#!/usr/bin/env python3
"""The browser-facing side of exec/terminal.

Runs a small WebSocket server on the dashboard's own websocket port (main
port + 1, same convention as the agent's), separate from the stdlib
http.server everything else runs on. The browser only ever talks to this --
never directly to an agent -- so a host's bearer token never has to reach
the browser. Session auth is the same cookie the main HTTP server already
issues; the browser sends it automatically on the WS handshake since
cookies aren't port-scoped.

For a local host this does the Docker exec/hijack itself, via the same
agent.DockerClient + execctl code the agent uses. For a remote host it opens
its own outbound WebSocket to that agent's exec endpoint (carrying the
host's stored token) and relays frames both ways -- a proxy, the same
local/remote split every other feature in this project has, just applied to
a raw byte stream instead of a JSON request.
"""

import http.cookies
import json
import sys
import threading
import urllib.parse

try:
    from websockets.sync.server import serve as ws_serve
    from websockets.sync.client import connect as ws_connect
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False


def _session_from_cookie(headers, sessions, cookie_name):
    raw = headers.get("Cookie")
    if not raw:
        return None
    try:
        jar = http.cookies.SimpleCookie(raw)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(cookie_name)
    if not morsel:
        return None
    return sessions.get(morsel.value)


def _relay(source, sink, stop):
    try:
        for message in source:
            sink.send(message)
    except Exception:
        pass
    finally:
        stop.set()


def _handle_remote(ws, host):
    scheme = "wss" if host.get("tls") else "ws"
    ws_port = int(host.get("port", 9713)) + 1
    url = "%s://%s:%s/v1/containers/%s/exec" % (
        scheme, host.get("address"), ws_port,
        urllib.parse.quote(ws.container_id, safe=""))
    if host.get("token"):
        url += "?token=" + urllib.parse.quote(host["token"], safe="")

    try:
        upstream = ws_connect(url, open_timeout=10)
    except Exception as exc:
        ws.send('{"error": "could not reach agent: %s"}' % str(exc).replace('"', "'"))
        ws.close(code=1011)
        return

    stop = threading.Event()
    t1 = threading.Thread(target=_relay, args=(ws, upstream, stop), daemon=True)
    t2 = threading.Thread(target=_relay, args=(upstream, ws, stop), daemon=True)
    t1.start()
    t2.start()
    stop.wait()
    try:
        upstream.close()
    except Exception:
        pass


def _handle_local(ws, host):
    import agent as agent_module
    import containerctl
    import execctl

    client = agent_module.DockerClient(host.get("docker_socket"))
    try:
        sock, leftover, exec_id = execctl.open_session(client, ws.container_id)
    except containerctl.ActionError as exc:
        ws.send('{"error": "%s"}' % str(exc).replace('"', "'"))
        ws.close(code=1011)
        return
    except Exception as exc:
        ws.send('{"error": "%s: %s"}' % (type(exc).__name__, exc))
        ws.close(code=1011)
        return

    stop = threading.Event()

    def pump_docker_to_browser():
        try:
            if leftover:
                ws.send(leftover)
            while not stop.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break
                ws.send(chunk)
        except Exception:
            pass
        finally:
            stop.set()

    reader = threading.Thread(target=pump_docker_to_browser, daemon=True)
    reader.start()

    try:
        for message in ws:
            if isinstance(message, str):
                try:
                    control = json.loads(message)
                except ValueError:
                    continue
                resize = control.get("resize") or {}
                if resize:
                    execctl.resize(client, exec_id, resize.get("cols"), resize.get("rows"))
            else:
                try:
                    sock.sendall(message)
                except OSError:
                    break
    except Exception:
        pass
    finally:
        stop.set()
        try:
            sock.close()
        except Exception:
            pass


def _make_handler(load_config, sessions, password, session_cookie_name):
    def handler(ws):
        parsed = urllib.parse.urlparse(ws.request.path)
        parts = [p for p in parsed.path.split("/") if p]
        # expect: api hosts <name> containers <id> exec
        if (len(parts) != 6 or parts[0] != "api" or parts[1] != "hosts"
                or parts[3] != "containers" or parts[5] != "exec"):
            ws.close(code=1008, reason="not found")
            return

        if password and not _session_from_cookie(ws.request.headers, sessions, session_cookie_name):
            ws.close(code=1008, reason="not signed in")
            return

        host_name = urllib.parse.unquote(parts[2])
        ws.container_id = urllib.parse.unquote(parts[4])

        config, _ = load_config()
        host = None
        for candidate in config.get("hosts", []):
            if candidate.get("name") == host_name:
                host = candidate
                break
        if not host:
            ws.close(code=1008, reason="no such host")
            return

        if host.get("mode") == "local":
            _handle_local(ws, host)
        else:
            _handle_remote(ws, host)

    return handler


def serve(load_config, sessions, password, session_cookie_name, bind="0.0.0.0", port=8501):
    if not WEBSOCKETS_AVAILABLE:
        sys.stderr.write(
            "exec/terminal disabled: the 'websockets' package is not installed\n")
        return None
    server = ws_serve(_make_handler(load_config, sessions, password, session_cookie_name),
                       bind, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sys.stderr.write("exec/terminal websocket listening on ws://%s:%s\n" % (bind, port))
    return server
