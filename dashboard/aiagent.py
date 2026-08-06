#!/usr/bin/env python3
"""The dashboard-wide assistant: a tool-calling loop over OpenRouter.

One "Ask AI" entry point for the whole site, not a button per container.
The model is given tools to look at every host/container/OS-update the
dashboard already tracks, and -- after the user explicitly confirms -- to
start, stop, restart, pause, unpause, rename, remove or recreate a
container, or install OS updates.

The loop is stateless on the server: the browser holds the whole
conversation (including tool calls and their results) and resends it every
turn, the same way the rest of this project avoids server-side session
storage (SessionStore aside, which only ever holds a login token, never
content).

Safety mirrors the REST API's own buttons, because it *is* the REST API's
own buttons: every write tool calls the same collector functions
start/stop/rename/etc. already use, which re-validate capability and
inputs on the agent side regardless of what this model asked for. The one
thing this module adds is a pause -- a write tool is never executed inside
the model's own loop. It always stops and hands control back to the
browser, which shows the same confirm dialog the buttons use (or skips it,
if the user has turned that off in Settings) before a second call actually
runs it.
"""

import json
import re
import time

if __package__ in (None, ""):
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dashboard import aihelper, collector, config as config_mod
else:
    from . import aihelper, collector, config as config_mod

MAX_ROUNDS = 6
HEX_ID = re.compile(r"^[a-fA-F0-9]{12,64}$")

SYSTEM_PROMPT = (
    "You are the assistant embedded in a Docker container and OS-update "
    "dashboard, covering every host it tracks. Use the tools you're given "
    "to look up what's actually running, fetch logs, and check pending OS "
    "updates before answering -- call list_hosts and/or list_containers "
    "first rather than guessing a host or container. Container and package "
    "names are ambiguous across hosts; always pass the exact host name a "
    "tool gave you.\n\n"
    "Actions that change something -- starting, stopping, restarting, "
    "pausing, unpausing, renaming, removing or recreating a container, or "
    "installing OS updates -- require the user's explicit confirmation and "
    "only run after they approve it in a dialog the app shows them. Call "
    "the tool as soon as you have enough information to propose the "
    "action; don't ask 'are you sure' yourself first, the confirm step "
    "already does that. If the user declines, don't retry the same action "
    "without them asking again."
)

SIMPLE_ACTIONS = {
    "start_container": "start",
    "stop_container": "stop",
    "restart_container": "restart",
    "pause_container": "pause",
    "unpause_container": "unpause",
}
CONTAINER_ARG_TOOLS = set(SIMPLE_ACTIONS) | {
    "get_logs", "get_logs_history", "rename_container", "remove_container", "recreate_container",
}
WRITE_TOOLS = set(SIMPLE_ACTIONS) | {
    "rename_container", "remove_container", "recreate_container", "install_os_updates",
}
READ_TOOLS = {"list_hosts", "list_containers", "get_logs", "get_logs_history", "list_os_updates"}


def _tool(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


_HOST_PROP = {"type": "string", "description": "A host's `name`, from list_hosts."}
_CONTAINER_PROP = {"type": "string", "description": "A container's id or name, from list_containers."}

TOOLS = [
    _tool("list_hosts", "List every host the dashboard tracks, with online status and "
          "container counts.", {}),
    _tool("list_containers",
          "List containers, optionally filtered by host, state, or a name search.",
          {
              "host": _HOST_PROP,
              "state": {"type": "string", "enum": ["running", "stopped", "all"],
                        "description": "Defaults to all."},
              "query": {"type": "string", "description": "Case-insensitive substring match "
                        "on the container name."},
          }),
    _tool("get_logs", "Fetch recent stdout/stderr lines for one container -- the live tail "
          "Docker itself still has.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP,
           "tail": {"type": "integer", "description": "How many lines. Defaults to 200."}},
          required=["host", "container"]),
    _tool("get_logs_history",
          "Fetch stored historical log lines for one container, if the host has log history "
          "enabled -- useful for output from before a restart, or that has since rotated out "
          "of the live tail.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP,
           "since_seconds": {"type": "integer", "description": "Only lines from this many "
                              "seconds ago onward. Omit for as far back as retention allows."},
           "limit": {"type": "integer", "description": "Max lines. Defaults to 500."}},
          required=["host", "container"]),
    _tool("list_os_updates", "List pending OS package updates, for one host or all of them.",
          {"host": _HOST_PROP,
           "severity": {"type": "string", "enum": ["security", "important", "routine"]}}),
    _tool("start_container", "Start a stopped container. Requires user confirmation.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP}, required=["host", "container"]),
    _tool("stop_container", "Stop a running container. Requires user confirmation.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP}, required=["host", "container"]),
    _tool("restart_container", "Restart a container. Requires user confirmation.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP}, required=["host", "container"]),
    _tool("pause_container", "Freeze a running container's processes in place, without "
          "stopping it. Requires user confirmation.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP}, required=["host", "container"]),
    _tool("unpause_container", "Resume a paused container. Requires user confirmation.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP}, required=["host", "container"]),
    _tool("rename_container", "Rename a container. Requires user confirmation.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP,
           "new_name": {"type": "string"}}, required=["host", "container", "new_name"]),
    _tool("remove_container", "Permanently remove a stopped container. Refused if it's still "
          "running. Requires user confirmation.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP}, required=["host", "container"]),
    _tool("recreate_container", "Pull the current image for a container and recreate it in "
          "place, keeping its config (env, ports, networks, volumes). Only meaningful when "
          "the container has an update available. Requires user confirmation.",
          {"host": _HOST_PROP, "container": _CONTAINER_PROP}, required=["host", "container"]),
    _tool("install_os_updates", "Install pending OS package updates on a host: either "
          "specific packages, or an entire severity tier. Requires user confirmation.",
          {"host": _HOST_PROP,
           "packages": {"type": "array", "items": {"type": "string"},
                        "description": "Specific package names. Omit to use severity instead."},
           "severity": {"type": "string", "enum": ["security", "important", "routine"],
                        "description": "Install every pending package at this severity. "
                        "Omit if using packages."}},
          required=["host"]),
]


# ---- reading the current snapshot --------------------------------------

def _list_hosts(poller):
    hosts = (poller.get() or {}).get("hosts") or []
    return {"hosts": [
        {
            "name": h.get("name"), "label": h.get("label"), "mode": h.get("mode"),
            "online": h.get("online"), "error": h.get("error"),
            "containers": len(h.get("containers") or []),
            "needs_attention": h.get("needs_attention"),
            "os_updates_available": bool((h.get("os") or {}).get("available")),
        }
        for h in hosts
    ]}


def _list_containers(poller, args):
    hosts = (poller.get() or {}).get("hosts") or []
    host_filter = (args.get("host") or "").strip() or None
    state_filter = (args.get("state") or "").strip().lower() or None
    query = (args.get("query") or "").strip().lower() or None
    out = []
    for h in hosts:
        if host_filter and h.get("name") != host_filter:
            continue
        for c in h.get("containers") or []:
            if state_filter and state_filter != "all" and c.get("state") != state_filter:
                continue
            if query and query not in (c.get("name") or "").lower():
                continue
            out.append({
                "host": h.get("name"), "id": c.get("id"), "name": c.get("name"),
                "state": c.get("state"), "status": c.get("status"),
                "image": c.get("image_display") or c.get("image_ref"),
                "update_status": c.get("update_status"), "detail": c.get("detail"),
            })
    return {"containers": out[:300]}


def _list_os_updates(poller, args):
    hosts = (poller.get() or {}).get("hosts") or []
    host_filter = (args.get("host") or "").strip() or None
    severity = (args.get("severity") or "").strip().lower() or None
    out = []
    for h in hosts:
        if host_filter and h.get("name") != host_filter:
            continue
        os_data = h.get("os") or {}
        for pkg in os_data.get("updates") or []:
            if severity and pkg.get("severity") != severity:
                continue
            out.append({
                "host": h.get("name"), "name": pkg.get("name"),
                "candidate": pkg.get("candidate"), "severity": pkg.get("severity"),
                "description": (pkg.get("description") or "")[:200],
            })
    return {"updates": out[:300]}


def _find_container_record(poller, host_name, container_id):
    for h in (poller.get() or {}).get("hosts") or []:
        if h.get("name") != host_name:
            continue
        for c in h.get("containers") or []:
            if c.get("id") == container_id:
                return c
    return None


def _resolve_container(poller, host_name, ref):
    """Tools only know an id or a name; the agent-side write calls only
    accept a real (hex) id -- resolve here, once, for every tool."""
    ref = (ref or "").strip()
    if not ref:
        return None, "container is required"
    for h in (poller.get() or {}).get("hosts") or []:
        if h.get("name") != host_name:
            continue
        containers = h.get("containers") or []
        if HEX_ID.match(ref):
            for c in containers:
                if (c.get("id") or "").startswith(ref.lower()):
                    return c.get("id"), None
        for c in containers:
            if (c.get("name") or "") == ref:
                return c.get("id"), None
        return None, "no container named or matching id %r on host %r -- call list_containers first" % (ref, host_name)
    return None, "no such host: %r" % host_name


def _prepare_target(poller, config, args):
    host_name = (args.get("host") or "").strip()
    host = config_mod.find_host(config, host_name) if host_name else None
    if not host:
        return None, None, {"error": "no such host: %r" % host_name}
    container_id, err = _resolve_container(poller, host_name, args.get("container"))
    if err:
        return host, None, {"error": err}
    return host, container_id, None


def execute_tool(name, args, config, poller):
    """Runs one tool, read or write alike -- callers decide *when* it's
    safe to call this; this function does not gate on WRITE_TOOLS itself."""
    args = args or {}
    try:
        if name == "list_hosts":
            return _list_hosts(poller)
        if name == "list_containers":
            return _list_containers(poller, args)
        if name == "list_os_updates":
            return _list_os_updates(poller, args)

        if name == "install_os_updates":
            host = config_mod.find_host(config, (args.get("host") or "").strip())
            if not host:
                return {"error": "no such host: %r" % args.get("host")}
            try:
                return collector.start_os_update(
                    host, packages=args.get("packages"), severity=args.get("severity"))
            except collector.OsUpdateRefused as exc:
                return {"error": str(exc)}

        if name in CONTAINER_ARG_TOOLS:
            host, container_id, err = _prepare_target(poller, config, args)
            if err:
                return err
            if name == "get_logs":
                return collector.container_logs(host, container_id, tail=int(args.get("tail") or 200))
            if name == "get_logs_history":
                since = time.time() - float(args["since_seconds"]) if args.get("since_seconds") else None
                return collector.container_logs_history(
                    host, container_id, since=since, limit=args.get("limit") or 500)
            if name in SIMPLE_ACTIONS:
                return collector.container_action(host, container_id, SIMPLE_ACTIONS[name])
            if name == "rename_container":
                new_name = (args.get("new_name") or "").strip()
                return collector.container_rename(host, container_id, new_name)
            if name == "remove_container":
                # The confirm dialog already showed the user what's being
                # removed; the expected-name re-check is for a typed prompt,
                # which this path doesn't have, so supply it ourselves from
                # what the dashboard already knows the container is called.
                current = _find_container_record(poller, args.get("host"), container_id)
                return collector.container_remove(
                    host, container_id, expected_name=(current or {}).get("name"))
            if name == "recreate_container":
                return collector.container_recreate(host, container_id)

        return {"error": "unknown tool: %s" % name}
    except collector.ContainerActionRefused as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


# ---- describing a pending write for the confirm dialog -----------------

def describe_pending(name, args):
    host = args.get("host") or "?"
    container = args.get("container") or "?"
    if name in SIMPLE_ACTIONS:
        verb = SIMPLE_ACTIONS[name]
        return {
            "title": "%s this container?" % verb.capitalize(),
            "message": "%s %s on %s?" % (verb.capitalize(), container, host),
            "danger": verb == "stop",
            "confirm_label": verb.capitalize(),
        }
    if name == "rename_container":
        return {
            "title": "Rename this container?",
            "message": "Rename %s on %s to %r?" % (container, host, args.get("new_name")),
            "danger": False, "confirm_label": "Rename",
        }
    if name == "remove_container":
        return {
            "title": "Remove this container?",
            "message": "Permanently remove %s on %s? This cannot be undone." % (container, host),
            "danger": True, "confirm_label": "Remove",
        }
    if name == "recreate_container":
        return {
            "title": "Recreate this container?",
            "message": "Pull the latest image and recreate %s on %s?" % (container, host),
            "danger": False, "confirm_label": "Recreate",
        }
    if name == "install_os_updates":
        if args.get("severity"):
            target = "every pending %s package" % args["severity"]
        elif args.get("packages"):
            target = ", ".join(args["packages"])
        else:
            target = "all pending packages"
        return {
            "title": "Install OS updates?",
            "message": "Install %s on %s?" % (target, host),
            "danger": False, "confirm_label": "Install",
        }
    return {
        "title": "Confirm this action?",
        "message": "%s(%s)" % (name, json.dumps(args)),
        "danger": True, "confirm_label": "Confirm",
    }


# ---- the loop itself -----------------------------------------------------

def _run(messages, config, poller, api_key, model):
    for _ in range(MAX_ROUNDS):
        try:
            response = aihelper.chat_completion(
                [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                tools=TOOLS, api_key_override=api_key, model_override=model,
            )
        except aihelper.ChatError as exc:
            return {"status": "error", "error": str(exc)}

        choices = response.get("choices") or []
        if not choices:
            error = response.get("error")
            if error:
                return {"status": "error", "error": "OpenRouter error: %s" % str(
                    error.get("message", error))[:300]}
            return {"status": "error", "error": "The model returned no response."}

        assistant_message = choices[0].get("message") or {}
        usage = response.get("usage")

        stored_assistant = {"role": "assistant"}
        if assistant_message.get("content"):
            stored_assistant["content"] = assistant_message["content"]
        tool_calls = assistant_message.get("tool_calls") or []
        if tool_calls:
            stored_assistant["tool_calls"] = tool_calls
        messages = messages + [stored_assistant]

        if not tool_calls:
            return {"status": "final", "messages": messages,
                    "reply": assistant_message.get("content") or "", "usage": usage}

        primary = tool_calls[0]
        extra = tool_calls[1:]
        fn = primary.get("function") or {}
        name = fn.get("name")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}

        skipped = [
            {"role": "tool", "tool_call_id": tc.get("id"),
             "content": json.dumps({"skipped": "only one tool call runs per turn -- ask again"})}
            for tc in extra
        ]

        if name in WRITE_TOOLS:
            return {
                "status": "needs_confirmation",
                "messages": messages + skipped,
                "pending": {"id": primary.get("id"), "name": name, "arguments": args,
                            "confirm": describe_pending(name, args)},
                "usage": usage,
            }

        result = execute_tool(name, args, config, poller)
        messages = messages + [
            {"role": "tool", "tool_call_id": primary.get("id"),
             "content": json.dumps(result, default=str)[:8000]}
        ] + skipped

    return {"status": "error", "error": "Stopped after too many tool calls in a row."}


def run_turn(messages, config, poller, api_key, model):
    return _run(list(messages or []), config, poller, api_key, model)


def resume_turn(messages, pending, approved, config, poller, api_key, model):
    messages = list(messages or [])
    pending = pending or {}
    if approved:
        result = execute_tool(pending.get("name"), pending.get("arguments") or {}, config, poller)
    else:
        result = {"cancelled": True, "reason": "The user declined this action."}
    messages.append({
        "role": "tool", "tool_call_id": pending.get("id"),
        "content": json.dumps(result, default=str)[:8000],
    })
    return _run(messages, config, poller, api_key, model)
