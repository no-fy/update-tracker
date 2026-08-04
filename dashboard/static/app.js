/* Container update dashboard -- view layer.
   Reads /api/state and renders it. No dependencies, no build step. */

(function () {
  "use strict";

  var STATUS = {
    "update-available": { label: "Update available", glyph: "↑", order: 0 },
    "restart-pending":  { label: "Restart pending",  glyph: "↻", order: 1 },
    "error":            { label: "Check failed",     glyph: "✕", order: 2 },
    "unknown":          { label: "Unknown",          glyph: "?",      order: 3 },
    "pinned":           { label: "Pinned",           glyph: "≡", order: 4 },
    "ignored":          { label: "Ignored",          glyph: "–", order: 5 },
    "up-to-date":       { label: "Up to date",       glyph: "✓", order: 6 }
  };
  var ATTENTION = ["update-available", "restart-pending"];
  var OS_LEVELS = [
    { key: "security",  label: "security",  status: "error" },
    { key: "important", label: "important", status: "update-available" },
    { key: "routine",   label: "routine",   status: "unknown" }
  ];

  var state = {
    data: null, tab: "containers",
    status: "", query: "",            // containers tab
    severity: "", osQuery: "",        // OS tab
    host: "", open: {}, picked: {}, osJobs: {}, canAddHosts: false,
    containerJobs: {}, logsOpen: {}, logs: {}, renaming: {}, settings: {}, logsRange: {}
  };
  var el = {};
  var pollTimer = null;

  function $(id) { return document.getElementById(id); }

  function text(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
  }

  function dot(status) {
    var node = text("span", "dot s-" + status);
    node.setAttribute("aria-hidden", "true");
    return node;
  }

  function badge(status) {
    var meta = STATUS[status] || { label: status };
    var node = text("span", "badge");
    node.appendChild(dot(status));
    node.appendChild(text("span", null, meta.label));
    return node;
  }

  // ---- formatting --------------------------------------------------------

  function relativeTime(value) {
    if (!value && value !== 0) return "";
    var seconds;
    if (typeof value === "number") {
      seconds = (Date.now() / 1000) - value;
    } else {
      var parsed = Date.parse(value);
      if (isNaN(parsed)) return "";
      seconds = (Date.now() - parsed) / 1000;
    }
    var future = seconds < 0;
    seconds = Math.abs(seconds);
    var units = [
      [31536000, "y"], [2592000, "mo"], [604800, "w"],
      [86400, "d"], [3600, "h"], [60, "m"]
    ];
    for (var i = 0; i < units.length; i++) {
      if (seconds >= units[i][0]) {
        var n = Math.floor(seconds / units[i][0]);
        return future ? "in " + n + units[i][1] : n + units[i][1] + " ago";
      }
    }
    return future ? "in a moment" : "just now";
  }

  function shortDigest(digest) {
    if (!digest) return "—";
    var hex = String(digest).replace(/^sha256:/, "");
    return hex.slice(0, 12);
  }

  function plural(n, one, many) { return n === 1 ? one : many; }

  // ---- data helpers ------------------------------------------------------

  function allContainers(data) {
    var out = [];
    (data.hosts || []).forEach(function (host) {
      (host.containers || []).forEach(function (c) { out.push(c); });
    });
    return out;
  }

  function matches(container) {
    if (state.status) {
      if (state.status === "attention") {
        if (ATTENTION.indexOf(container.update_status) === -1) return false;
      } else if (container.update_status !== state.status) {
        return false;
      }
    }
    if (state.query) {
      var haystack = [
        container.name, container.image_display, container.image_ref,
        container.compose_project, container.compose_service, container.host
      ].join(" ").toLowerCase();
      if (haystack.indexOf(state.query) === -1) return false;
    }
    return true;
  }

  function matchesUpdate(update) {
    if (state.severity && update.severity !== state.severity) return false;
    if (state.osQuery) {
      var haystack = [update.name, update.installed, update.candidate, update.source]
        .join(" ").toLowerCase();
      if (haystack.indexOf(state.osQuery) === -1) return false;
    }
    return true;
  }

  function osHosts(data) {
    return (data.hosts || []).filter(function (host) {
      return host.os && (host.os.available || host.os.error);
    });
  }

  function statusRank(status) {
    var meta = STATUS[status];
    return meta ? meta.order : 99;
  }

  // ---- rendering ---------------------------------------------------------

  function renderHero(data) {
    var summary = data.summary || {};
    if (state.tab === "os") {
      renderOsHero(data, summary);
      return;
    }
    var needs = summary.needs_attention || 0;
    el.heroValue.textContent = data.generated_at ? needs : "—";
    el.heroLabel.textContent = needs === 1
      ? "container needs an update"
      : "containers need an update";

    var counts = summary.counts || {};
    var notes = [];
    if (counts["update-available"]) {
      notes.push(counts["update-available"] + " with a newer image in the registry");
    }
    if (counts["restart-pending"]) {
      notes.push(counts["restart-pending"] + " already pulled, awaiting a recreate");
    }
    if (!notes.length && data.generated_at) {
      notes.push("Everything checked is running its tag's current image.");
    }
    el.heroNote.textContent = notes.join(" · ");
  }

  function renderOsHero(data, summary) {
    var counts = summary.os_counts || {};
    var security = counts.security || 0;
    el.heroValue.textContent = data.generated_at ? security : "—";
    el.heroLabel.textContent = security === 1
      ? "security update waiting"
      : "security updates waiting";

    var notes = [];
    if (counts.important) notes.push(counts.important + " important");
    if (counts.routine) notes.push(counts.routine + " routine");
    if (summary.os_reboots_required) {
      notes.push(summary.os_reboots_required + " " +
        plural(summary.os_reboots_required, "host needs", "hosts need") + " a reboot");
    }
    if (!notes.length && data.generated_at) {
      notes.push(summary.os_hosts_reporting
        ? "Every host reporting packages is up to date."
        : "No host is reporting OS packages yet.");
    }
    el.heroNote.textContent = notes.join(" · ");
  }

  function kpi(label, status, value, note) {
    var card = text("div", "kpi");
    var head = text("div", "kpi-label");
    if (status) head.appendChild(dot(status));
    head.appendChild(text("span", null, label));
    card.appendChild(head);
    card.appendChild(text("div", "kpi-value", value));
    if (note) card.appendChild(text("div", "kpi-note", note));
    return card;
  }

  function renderKpis(data) {
    var summary = data.summary || {};
    var counts = summary.counts || {};
    el.kpis.textContent = "";
    el.kpis.appendChild(kpi(
      "Up to date", "up-to-date", counts["up-to-date"] || 0,
      summary.containers_total
        ? Math.round(100 * (counts["up-to-date"] || 0) / summary.containers_total) + "% of all containers"
        : null
    ));
    el.kpis.appendChild(kpi(
      "Not checked", "unknown",
      (counts["unknown"] || 0) + (counts["pinned"] || 0) + (counts["ignored"] || 0),
      "locally built, pinned or ignored"
    ));
    el.kpis.appendChild(kpi(
      "Check failed", "error", counts["error"] || 0,
      (counts["error"] || 0) ? "registry unreachable or private" : "no registry errors"
    ));
    if (summary.os_hosts_reporting) {
      var osCounts = summary.os_counts || {};
      el.kpis.appendChild(kpi(
        "OS updates", osCounts.security ? "error" : "unknown",
        summary.os_updates_total || 0,
        osCounts.security
          ? osCounts.security + " security" + (summary.os_reboots_required
              ? ", " + summary.os_reboots_required + " need a reboot" : "")
          : "no security updates pending"
      ));
    }
    el.kpis.appendChild(kpi(
      "Hosts online", null,
      (summary.hosts_online || 0) + "/" + (summary.hosts_total || 0),
      summary.containers_total + " " + plural(summary.containers_total, "container", "containers") +
        ", " + (summary.containers_running || 0) + " running"
    ));
  }

  function renderTabs(data) {
    var summary = data.summary || {};
    el.tabContainersCount.textContent = summary.containers_total || 0;
    el.tabOsCount.textContent = summary.os_updates_total || 0;
    el.tabContainers.setAttribute("aria-selected", state.tab === "containers" ? "true" : "false");
    el.tabOs.setAttribute("aria-selected", state.tab === "os" ? "true" : "false");
    el.tabOs.hidden = !summary.os_hosts_reporting && !osHosts(data).length;
    el.search.placeholder = state.tab === "os"
      ? "Search package or version…"
      : "Search name or image…";
  }

  function selectTab(tab) {
    if (state.tab === tab) return;
    state.tab = tab;
    // Each tab keeps its own query, so switching never applies a search that
    // was meant for the other list.
    el.search.value = tab === "os" ? state.osQuery : state.query;
    render();
  }

  function chipRow(container, options, current, onPick) {
    container.textContent = "";
    options.forEach(function (option) {
      var chip = text("button", "chip");
      chip.type = "button";
      chip.setAttribute("aria-pressed", current === option.key ? "true" : "false");
      if (option.status) chip.appendChild(dot(option.status));
      chip.appendChild(text("span", null, option.label));
      chip.appendChild(text("span", "count", option.count));
      chip.addEventListener("click", function () { onPick(option.key); });
      container.appendChild(chip);
    });
  }

  function renderOsFilters(data) {
    var counts = (data.summary || {}).os_counts || {};
    var total = (data.summary || {}).os_updates_total || 0;
    var options = [{ key: "", label: "All", count: total }];
    OS_LEVELS.forEach(function (level) {
      if (counts[level.key]) {
        options.push({
          key: level.key, label: level.label,
          count: counts[level.key], status: level.status
        });
      }
    });
    chipRow(el.statusFilters, options, state.severity, function (key) {
      state.severity = state.severity === key ? "" : key;
      render();
    });
    fillHostFilter(osHosts(data));
  }

  function fillHostFilter(hosts) {
    var previous = state.host;
    el.hostFilter.textContent = "";
    var all = text("option", null, "All hosts");
    all.value = "";
    el.hostFilter.appendChild(all);
    var known = false;
    hosts.forEach(function (host) {
      var option = text("option", null, host.label || host.name);
      option.value = host.name;
      if (host.name === previous) known = true;
      el.hostFilter.appendChild(option);
    });
    el.hostFilter.value = known ? previous : "";
    if (!known) state.host = "";
  }

  function renderOsHosts(data) {
    el.hosts.textContent = "";
    var hosts = osHosts(data).filter(function (host) {
      return !state.host || host.name === state.host;
    });

    var shown = 0;
    hosts.forEach(function (host) {
      var os = host.os || {};
      var updates = (os.updates || []).filter(matchesUpdate);
      shown += updates.length;
      if (!updates.length && os.available && (state.severity || state.osQuery)) return;
      el.hosts.appendChild(osCard(host, updates));
    });

    if (!hosts.length) {
      el.empty.hidden = false;
      el.empty.textContent = "";
      el.empty.appendChild(text("span", null, "No host is reporting OS packages. Give the agent a read-only view of the host with "));
      el.empty.appendChild(text("code", null, "-v /:/host:ro -e CUD_HOST_ROOT=/host"));
      el.empty.appendChild(text("span", null, "."));
    } else if (!shown && !el.hosts.children.length) {
      el.empty.hidden = false;
      el.empty.textContent = "No packages match the current filter.";
    } else {
      el.empty.hidden = true;
    }
  }

  function osCard(host, updates) {
    var card = text("section", "host");
    var head = text("div", "host-head");
    var title = text("div", "host-title");
    var os = host.os || {};

    title.appendChild(dot(
      !os.available ? "error" : ((os.counts || {}).security ? "error" : "up-to-date")
    ));
    title.appendChild(text("span", "name", host.label || host.name));
    title.appendChild(text("span", "tag", os.manager || "unknown"));
    head.appendChild(title);

    var meta = text("div", "host-meta");
    var bits = [];
    if (os.available) {
      bits.push(updates.length + " " + plural(updates.length, "package", "packages") + " shown");
      if (os.packages_installed) bits.push(os.packages_installed + " installed");
      if (os.lists_updated) bits.push("lists " + relativeTime(os.lists_updated));
      if (os.reboot_required) bits.push("reboot required");
    } else {
      bits.push(os.error || "not reported");
    }
    bits.forEach(function (bit, index) {
      if (index) meta.appendChild(text("span", "sep", "·"));
      meta.appendChild(text("span", null, bit));
    });
    head.appendChild(meta);
    card.appendChild(head);

    if (updates.length) {
      var body = text("div", "os-panel");
      body.appendChild(updateBar(host, updates));
      body.appendChild(osTable(host, updates));
      card.appendChild(body);
    }
    return card;
  }

  function pickedFor(host) {
    return Object.keys(state.picked).filter(function (key) {
      return state.picked[key] && key.indexOf(host.name + "/") === 0;
    }).map(function (key) { return key.slice(host.name.length + 1); });
  }

  function updateBar(host, updates) {
    var bar = text("div", "os-actions");
    bar.id = "os-actions-" + host.name;
    var updating = (host.os && host.os.updating) || {};

    if (!updating.can_update) {
      bar.appendChild(text("span", "os-readonly", updating.reason ||
        "This agent is read-only, so updates can only be installed on the host itself."));
      return bar;
    }

    var job = state.osJobs[host.name];
    if (job && job.status === "running") {
      var busy = text("span", "os-running");
      busy.appendChild(text("span", "spinner"));
      busy.appendChild(text("span", null, "Installing " + job.packages.length + " " +
        plural(job.packages.length, "package", "packages") + "…"));
      bar.appendChild(busy);
      bar.appendChild(logBox(job));
      return bar;
    }

    var picked = pickedFor(host);
    var security = updates.filter(function (u) { return u.severity === "security"; });

    var selected = text("button", "button small");
    selected.type = "button";
    selected.textContent = picked.length
      ? "Install " + picked.length + " selected"
      : "Select packages to install";
    selected.disabled = !picked.length;
    selected.addEventListener("click", function () {
      confirmAndRun(host, picked, null,
        "Install " + picked.length + " " + plural(picked.length, "package", "packages") +
        " on " + (host.label || host.name) + "?");
    });
    bar.appendChild(selected);

    if (security.length) {
      var sec = text("button", "button small primary");
      sec.type = "button";
      sec.textContent = "Install " + security.length + " security " +
        plural(security.length, "update", "updates");
      sec.addEventListener("click", function () {
        confirmAndRun(host, null, "security",
          "Install all " + security.length + " security " +
          plural(security.length, "update", "updates") + " on " +
          (host.label || host.name) + "?");
      });
      bar.appendChild(sec);
    }

    if (job && job.status && job.status !== "running") {
      var done = text("span", job.status === "ok" ? "os-done" : "os-failed",
        job.status === "ok"
          ? "Installed " + job.packages.length + " " + plural(job.packages.length, "package", "packages")
          : "Update failed");
      bar.appendChild(done);
      bar.appendChild(logBox(job));
    }
    return bar;
  }

  function logBox(job) {
    var pre = text("pre", "joblog", (job.lines || []).join("\n"));
    return pre;
  }

  function confirmAndRun(host, packages, severity, question) {
    confirmDialog("Install updates?", question + " This runs the package manager on that machine.",
      { confirmLabel: "Install" }).then(function (ok) {
      if (!ok) return;
      var body = {};
      if (packages) body.packages = packages;
      if (severity) body.severity = severity;

      state.osJobs[host.name] = { status: "running", packages: packages || [], lines: [] };
      render();

      postJSON("/api/hosts/" + encodeURIComponent(host.name) + "/os/update", body)
        .then(function (job) {
          if (job.error) throw new Error(job.error);
          state.osJobs[host.name] = job;
          pickedFor(host).forEach(function (name) {
            delete state.picked[host.name + "/" + name];
          });
          render();
          watchOsJob(host, job.id);
        })
        .catch(function (err) {
          state.osJobs[host.name] = {
            status: "failed", packages: packages || [],
            lines: [err.message || "The update could not be started."]
          };
          render();
        });
    });
  }

  function watchOsJob(host, jobId) {
    fetch("/api/hosts/" + encodeURIComponent(host.name) + "/os/job/" + encodeURIComponent(jobId))
      .then(function (r) { return r.json(); })
      .then(function (job) {
        state.osJobs[host.name] = job;
        render();
        if (job.status === "running") {
          setTimeout(function () { watchOsJob(host, jobId); }, 1500);
        } else {
          // The package list is stale now; pull a fresh one.
          fetch("/api/refresh", { method: "POST" }).then(function () {
            setTimeout(load, 1500);
          });
        }
      })
      .catch(function () {
        setTimeout(function () { watchOsJob(host, jobId); }, 3000);
      });
  }

  function renderUpdateBar(host) {
    var existing = document.getElementById("os-actions-" + host.name);
    if (!existing || !existing.parentNode) return;
    var updates = ((host.os || {}).updates || []).filter(matchesUpdate);
    existing.parentNode.replaceChild(updateBar(host, updates), existing);
  }

  // ---- container actions --------------------------------------------------

  function capitalize(word) { return word.charAt(0).toUpperCase() + word.slice(1); }

  var ACTION_PRESENT = {
    start: "Starting", stop: "Stopping", restart: "Restarting", pause: "Pausing",
    unpause: "Unpausing", rename: "Renaming", remove: "Removing", recreate: "Recreating"
  };
  var ACTION_PAST = {
    start: "started", stop: "stopped", restart: "restarted", pause: "paused",
    unpause: "unpaused", rename: "renamed", remove: "removed", recreate: "recreated"
  };
  function presentTense(action) { return ACTION_PRESENT[action] || (capitalize(action) + "ing"); }
  function pastTense(action) { return ACTION_PAST[action] || (action + "ed"); }

  function containerKey(container) { return container.host + "/" + container.id; }

  function containerActionBar(host, container) {
    var bar = text("div", "container-actions");
    var capability = host.container_actions || {};
    if (!capability.can_manage) {
      bar.appendChild(text("span", "os-readonly", capability.reason ||
        "This agent is read-only, so containers can only be managed on the host itself."));
      return bar;
    }

    var key = containerKey(container);
    var job = state.containerJobs[key];
    if (job && job.status === "running") {
      var busy = text("span", "os-running");
      busy.appendChild(text("span", "spinner"));
      busy.appendChild(text("span", null, presentTense(job.action) + " " + container.name + "…"));
      bar.appendChild(busy);
      if (job.action === "recreate" && job.lines && job.lines.length) {
        bar.appendChild(logBox(job));
      }
      return bar;
    }

    var running = container.state === "running";
    var paused = container.state === "paused";

    function actionButton(parent, label, action, needsConfirm, style) {
      var btn = text("button", "button small" + (style ? " " + style : ""));
      btn.type = "button";
      btn.textContent = label;
      btn.addEventListener("click", function () {
        if (!needsConfirm) {
          runContainerAction(host, container, action);
          return;
        }
        confirmDialog(
          label + " this container?",
          label + " " + container.name + " on " + (host.label || host.name) + "?",
          { confirmLabel: label, danger: style === "danger" }
        ).then(function (ok) { if (ok) runContainerAction(host, container, action); });
      });
      parent.appendChild(btn);
    }

    var lifecycle = text("div", "action-group");
    if (!running && !paused) actionButton(lifecycle, "Start", "start", false, "primary");
    if (paused) actionButton(lifecycle, "Unpause", "unpause", false, "primary");
    if (running) actionButton(lifecycle, "Pause", "pause", false, "");
    if (running) actionButton(lifecycle, "Restart", "restart", true, "");
    if (running || paused) actionButton(lifecycle, "Stop", "stop", true, "danger");
    bar.appendChild(lifecycle);

    if (container.update_status === "update-available" || container.update_status === "restart-pending") {
      var recreateBtn = text("button", "button small primary");
      recreateBtn.type = "button";
      recreateBtn.textContent = "Recreate with latest image";
      recreateBtn.addEventListener("click", function () {
        confirmDialog(
          "Recreate this container?",
          "Pull the latest image for " + container.name + " and swap it in, keeping its " +
          "config (env, ports, networks, volumes). Brief downtime while it restarts.",
          { confirmLabel: "Recreate" }
        ).then(function (ok) { if (ok) runContainerRecreate(host, container); });
      });
      bar.appendChild(recreateBtn);
    }

    var manage = text("div", "action-group");
    if (state.renaming[key]) {
      manage.appendChild(renameForm(host, container, key));
    } else {
      var renameBtn = text("button", "button small");
      renameBtn.type = "button";
      renameBtn.textContent = "Rename";
      renameBtn.addEventListener("click", function () {
        state.renaming[key] = true;
        render();
      });
      manage.appendChild(renameBtn);
    }
    if (!running && !paused) {
      var removeBtn = text("button", "button small danger");
      removeBtn.type = "button";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", function () {
        confirmDialog(
          "Remove this container?",
          "This permanently deletes " + container.name + " on " + (host.label || host.name) +
          ". This cannot be undone.",
          { confirmLabel: "Remove", danger: true }
        ).then(function (ok) { if (ok) runContainerRemove(host, container); });
      });
      manage.appendChild(removeBtn);
    }
    bar.appendChild(manage);

    if (job && job.status && job.status !== "running") {
      var done = text("span", job.status === "ok" ? "os-done" : "os-failed",
        job.status === "ok"
          ? capitalize(pastTense(job.action)) + " " + container.name
          : (job.message || (capitalize(pastTense(job.action)) + " failed")));
      bar.appendChild(done);
      if (job.action === "recreate" && job.lines && job.lines.length) {
        bar.appendChild(logBox(job));
      }
    }

    return bar;
  }

  function renameForm(host, container, key) {
    var wrap = text("span", "rename-form");
    var input = document.createElement("input");
    input.type = "text";
    input.value = container.name;
    input.className = "rename-input";
    wrap.appendChild(input);

    var save = text("button", "button small primary");
    save.type = "button";
    save.textContent = "Save";
    save.addEventListener("click", function () {
      var newName = input.value.trim();
      if (!newName || newName === container.name) {
        state.renaming[key] = false;
        render();
        return;
      }
      runContainerRename(host, container, newName);
    });
    wrap.appendChild(save);

    var cancel = text("button", "button small");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", function () {
      state.renaming[key] = false;
      render();
    });
    wrap.appendChild(cancel);

    return wrap;
  }

  function runContainerAction(host, container, action) {
    var key = containerKey(container);
    state.containerJobs[key] = { status: "running", action: action };
    render();

    postJSON(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) + "/" + action,
      {}
    )
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.containerJobs[key] = { status: "ok", action: action };
        render();
        fetch("/api/refresh", { method: "POST" }).then(function () {
          setTimeout(load, 1200);
        });
      })
      .catch(function (err) {
        state.containerJobs[key] = {
          status: "failed", action: action,
          message: err.message || (capitalize(pastTense(action)) + " failed."),
        };
        render();
      });
  }

  function runContainerRename(host, container, newName) {
    var key = containerKey(container);
    state.containerJobs[key] = { status: "running", action: "rename" };
    render();

    postJSON(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) + "/rename",
      { name: newName }
    )
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.containerJobs[key] = { status: "ok", action: "rename" };
        state.renaming[key] = false;
        render();
        fetch("/api/refresh", { method: "POST" }).then(function () {
          setTimeout(load, 1200);
        });
      })
      .catch(function (err) {
        state.containerJobs[key] = {
          status: "failed", action: "rename",
          message: err.message || "Rename failed.",
        };
        render();
      });
  }

  function runContainerRemove(host, container) {
    var key = containerKey(container);
    state.containerJobs[key] = { status: "running", action: "remove" };
    render();

    fetch(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) +
      "?expected_name=" + encodeURIComponent(container.name),
      { method: "DELETE" }
    )
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        render();
        fetch("/api/refresh", { method: "POST" }).then(function () {
          setTimeout(load, 1200);
        });
      })
      .catch(function (err) {
        state.containerJobs[key] = {
          status: "failed", action: "remove",
          message: err.message || "Remove failed.",
        };
        render();
      });
  }

  function runContainerRecreate(host, container) {
    var key = containerKey(container);
    state.containerJobs[key] = { status: "running", action: "recreate", lines: [] };
    render();

    postJSON(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) + "/recreate",
      {}
    )
      .then(function (job) {
        if (job.error) throw new Error(job.error);
        state.containerJobs[key] = recreateJobState(job);
        render();
        watchRecreateJob(host, container, job.id);
      })
      .catch(function (err) {
        state.containerJobs[key] = {
          status: "failed", action: "recreate",
          message: err.message || "Recreate could not be started.",
        };
        render();
      });
  }

  function recreateJobState(job) {
    return { status: job.status, action: "recreate", lines: job.lines || [], jobId: job.id };
  }

  function watchRecreateJob(host, container, jobId) {
    var key = containerKey(container);
    fetch(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) +
      "/recreate/job/" + encodeURIComponent(jobId)
    )
      .then(function (r) { return r.json(); })
      .then(function (job) {
        state.containerJobs[key] = recreateJobState(job);
        render();
        if (job.status === "running") {
          setTimeout(function () { watchRecreateJob(host, container, jobId); }, 1200);
        } else {
          fetch("/api/refresh", { method: "POST" }).then(function () {
            setTimeout(load, 1200);
          });
        }
      })
      .catch(function () {
        setTimeout(function () { watchRecreateJob(host, container, jobId); }, 2500);
      });
  }

  // Auto-refresh timers live outside `state` -- they are not render-derived
  // data, just live handles, and must survive individual render() calls.
  var logTimers = {};

  function logTailLines() { return (state.settings && state.settings.log_tail_lines) || 300; }
  function logRefreshSeconds() { return (state.settings && state.settings.log_refresh_seconds) || 5; }
  function logAutoRefreshEnabled() {
    return !state.settings || state.settings.log_auto_refresh !== false;
  }

  function startLogsAutoRefresh(host, container) {
    var key = containerKey(container);
    if (logTimers[key] || !logAutoRefreshEnabled()) return;
    logTimers[key] = setInterval(function () {
      if (!state.logsOpen[key]) { stopLogsAutoRefresh(key); return; }
      loadLogs(host, container, true);
    }, logRefreshSeconds() * 1000);
  }

  function stopLogsAutoRefresh(key) {
    if (logTimers[key]) { clearInterval(logTimers[key]); delete logTimers[key]; }
  }

  function logsToggleButton(host, container) {
    var key = containerKey(container);
    var btn = text("button", "button small");
    btn.type = "button";
    btn.textContent = state.logsOpen[key] ? "Hide logs" : "Logs";
    btn.addEventListener("click", function () {
      var opening = !state.logsOpen[key];
      state.logsOpen[key] = opening;
      state.open[key] = true;
      if (opening) {
        loadLogs(host, container);
        startLogsAutoRefresh(host, container);
      } else {
        stopLogsAutoRefresh(key);
      }
      render();
    });
    return btn;
  }

  var LOG_RANGES = [
    { id: "live", label: "Live" },
    { id: "1h", label: "1h", seconds: 3600 },
    { id: "24h", label: "24h", seconds: 86400 },
    { id: "7d", label: "7d", seconds: 604800 }
  ];

  function formatLogLine(item) {
    if (typeof item === "string") return item;
    var when = item.ts ? new Date(item.ts * 1000).toLocaleString() : "";
    return (when ? "[" + when + "] " : "") + item.line;
  }

  function logsSection(host, container) {
    var key = containerKey(container);
    var wrap = text("div", "logs-section");
    var range = state.logsRange[key] || "live";
    var entry = state.logs[key];

    var rangeRow = text("div", "logs-range");
    LOG_RANGES.forEach(function (r) {
      var btn = text("button", "chip");
      btn.type = "button";
      btn.setAttribute("aria-pressed", range === r.id ? "true" : "false");
      btn.textContent = r.label;
      btn.addEventListener("click", function () {
        if (state.logsRange[key] === r.id) return;
        state.logsRange[key] = r.id;
        if (r.id === "live") {
          loadLogs(host, container);
          startLogsAutoRefresh(host, container);
        } else {
          stopLogsAutoRefresh(key);
          loadLogHistory(host, container, r.seconds);
        }
        render();
      });
      rangeRow.appendChild(btn);
    });
    wrap.appendChild(rangeRow);

    if (entry && entry.loading) {
      wrap.appendChild(text("span", "os-running", "Loading logs…"));
      return wrap;
    }

    var refreshBtn = text("button", "button small");
    refreshBtn.type = "button";
    refreshBtn.textContent = "Refresh";
    refreshBtn.addEventListener("click", function () {
      if (range === "live") {
        loadLogs(host, container);
      } else {
        var r = LOG_RANGES.filter(function (item) { return item.id === range; })[0];
        loadLogHistory(host, container, r ? r.seconds : null);
      }
    });
    wrap.appendChild(refreshBtn);

    if (range === "live" && logAutoRefreshEnabled()) {
      wrap.appendChild(text("span", "logs-auto-hint",
        "Auto-refreshing every " + logRefreshSeconds() + "s"));
    }

    var pre = text("pre", "joblog", entry
      ? (entry.error || (entry.lines || []).map(formatLogLine).join("\n") || "No log output.")
      : "");
    wrap.appendChild(pre);
    return wrap;
  }

  function loadLogs(host, container, silent) {
    var key = containerKey(container);
    if (!silent) {
      state.logs[key] = { loading: true };
      render();
    }

    fetch(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) + "/logs?tail=" + logTailLines()
    )
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.logs[key] = { lines: result.lines || [] };
        render();
      })
      .catch(function (err) {
        // A silent background refresh that fails keeps showing the last good
        // fetch rather than replacing it with an error -- one flaky poll
        // should not blank out logs someone is actively reading.
        if (!silent) {
          state.logs[key] = { error: err.message || "Could not load logs." };
          render();
        }
      });
  }

  function loadLogHistory(host, container, seconds) {
    var key = containerKey(container);
    state.logs[key] = { loading: true };
    render();

    var since = seconds ? (Date.now() / 1000 - seconds) : null;
    var qs = "?limit=2000" + (since ? "&since=" + since : "");

    fetch(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) + "/logs/history" + qs
    )
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        if (result.enabled === false) {
          state.logs[key] = { error: "Log history is not enabled on this host." };
        } else {
          state.logs[key] = { lines: result.lines || [] };
        }
        render();
      })
      .catch(function (err) {
        state.logs[key] = { error: err.message || "Could not load log history." };
        render();
      });
  }

  function renderFilters(data) {
    var counts = (data.summary || {}).counts || {};
    var total = (data.summary || {}).containers_total || 0;
    var options = [{ key: "", label: "All", count: total }];
    var attention = (data.summary || {}).needs_attention || 0;
    if (attention) options.push({ key: "attention", label: "Needs attention", count: attention });
    Object.keys(STATUS)
      .sort(function (a, b) { return STATUS[a].order - STATUS[b].order; })
      .forEach(function (key) {
        if (counts[key]) options.push({ key: key, label: STATUS[key].label, count: counts[key] });
      });

    el.statusFilters.textContent = "";
    options.forEach(function (option) {
      var chip = text("button", "chip");
      chip.type = "button";
      chip.setAttribute("aria-pressed", state.status === option.key ? "true" : "false");
      if (option.key && option.key !== "attention") chip.appendChild(dot(option.key));
      chip.appendChild(text("span", null, option.label));
      chip.appendChild(text("span", "count", option.count));
      chip.addEventListener("click", function () {
        state.status = state.status === option.key ? "" : option.key;
        render();
      });
      el.statusFilters.appendChild(chip);
    });

    var previous = state.host;
    el.hostFilter.textContent = "";
    var all = text("option", null, "All hosts");
    all.value = "";
    el.hostFilter.appendChild(all);
    var known = false;
    (data.hosts || []).forEach(function (host) {
      var option = text("option", null, host.label || host.name);
      option.value = host.name;
      if (host.name === previous) known = true;
      el.hostFilter.appendChild(option);
    });
    // The selected host can disappear (removed, or renamed) -- fall back to all.
    if (previous && !known) state.host = "";
    el.hostFilter.value = state.host;
  }

  function containerDetailsPanel(container) {
    var grid = text("div", "detail-grid");

    function item(label, value) {
      if (!value) return;
      var wrapper = text("dl", "detail-item");
      wrapper.appendChild(text("dt", null, label));
      wrapper.appendChild(text("dd", null, value));
      grid.appendChild(wrapper);
    }

    item("Image", container.image_ref);
    item("Running", shortDigest(container.local_digest));
    item("Registry", shortDigest(container.remote_digest));
    item("Built", container.image_created ? relativeTime(container.image_created) : null);
    item("Published", container.remote_created ? relativeTime(container.remote_created) : null);
    item("Container", container.id);
    item("Created", relativeTime(container.created));
    item("Ports", (container.ports || []).join(", "));
    if (container.compose_workdir) item("Compose", container.compose_workdir);

    return grid;
  }

  function containerRow(container, host) {
    var key = containerKey(container);
    var row = text("tr", "row");
    row.tabIndex = 0;

    var name = text("td", "c-name");
    name.appendChild(text("span", null, container.name));
    var subtitle = container.compose_project
      ? container.compose_project + " · " + (container.compose_service || "")
      : (container.ports || []).slice(0, 2).join("  ");
    if (subtitle) name.appendChild(text("span", "sub", subtitle));
    row.appendChild(name);

    row.appendChild(text("td", "c-image", container.image_display || container.image_ref || "—"));

    var stateCell = text("td", "c-state optional");
    stateCell.textContent = container.status || container.state || "—";
    row.appendChild(stateCell);

    row.appendChild(text("td", "c-age optional", relativeTime(container.image_created) || "—"));

    var status = text("td", "c-status");
    status.appendChild(badge(container.update_status));
    row.appendChild(status);

    var actionsCell = text("td", "c-actions");
    actionsCell.addEventListener("click", function (event) { event.stopPropagation(); });
    actionsCell.appendChild(containerActionBar(host, container));
    actionsCell.appendChild(logsToggleButton(host, container));
    row.appendChild(actionsCell);

    var detail = text("tr", "detail");
    var cell = document.createElement("td");
    cell.colSpan = 6;
    var body = text("div", "detail-body");
    body.appendChild(text("p", "detail-note", container.detail || ""));
    body.appendChild(containerDetailsPanel(container));
    if (state.logsOpen[key]) body.appendChild(logsSection(host, container));

    cell.appendChild(body);
    detail.appendChild(cell);
    detail.hidden = !state.open[key];

    function toggle() {
      state.open[key] = !state.open[key];
      detail.hidden = !state.open[key];
      if (!state.open[key]) {
        stopLogsAutoRefresh(key);
      } else if (state.logsOpen[key]) {
        startLogsAutoRefresh(host, container);
        loadLogs(host, container, true);
      }
    }
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
    });

    return [row, detail];
  }

  function hostCard(host, containers) {
    var card = text("section", "host");

    var head = text("div", "host-head");
    var title = text("div", "host-title");
    title.appendChild(dot(
      !host.online ? "error" : (host.needs_attention ? "update-available" : "up-to-date")
    ));
    title.appendChild(text("span", "name", host.label || host.name));
    // Two hosts can share a hostname label -- keep the configured name visible.
    var tagBits = [];
    if (host.label && host.label !== host.name) tagBits.push(host.name);
    tagBits.push(host.mode === "local" ? "local socket" : (host.address || "agent"));
    title.appendChild(text("span", "tag", tagBits.join(" · ")));
    head.appendChild(title);

    var meta = text("div", "host-meta");
    var bits = [];
    if (host.online) {
      var info = host.info || {};
      if (info.docker_version) bits.push("Docker " + info.docker_version);
      if (info.os) bits.push(info.os);
      bits.push(containers.length + " " + plural(containers.length, "container", "containers") + " shown");
      if (host.needs_attention) bits.push(host.needs_attention + " need attention");
    } else {
      bits.push("offline");
    }
    bits.forEach(function (bit, index) {
      if (index) meta.appendChild(text("span", "sep", "·"));
      meta.appendChild(text("span", null, bit));
    });
    head.appendChild(meta);
    card.appendChild(head);

    if (!host.online) {
      var error = text("div", "host-error");
      error.appendChild(dot("error"));
      error.appendChild(text("span", null, host.error || "unreachable"));
      card.appendChild(error);
      return card;
    }

    if (!containers.length) {
      card.appendChild(text("div", "host-error", "No containers match the current filter."));
      return card;
    }

    var scroll = text("div", "table-scroll");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    [["Container", ""], ["Image", ""], ["State", "optional"], ["Image age", "optional"],
     ["Update", ""], ["Actions", ""]]
      .forEach(function (column) {
        headRow.appendChild(text("th", column[1], column[0]));
      });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    containers
      .slice()
      .sort(function (a, b) {
        var delta = statusRank(a.update_status) - statusRank(b.update_status);
        return delta || a.name.localeCompare(b.name);
      })
      .forEach(function (container) {
        containerRow(container, host).forEach(function (node) { tbody.appendChild(node); });
      });
    table.appendChild(tbody);
    scroll.appendChild(table);
    card.appendChild(scroll);
    return card;
  }

  function bytes(value) {
    if (!value) return null;
    var units = ["B", "kB", "MB", "GB"];
    var n = value, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + units[i];
  }

  function osTable(host, updates) {
    var canUpdate = host.os && host.os.updating && host.os.updating.can_update;
    var table = text("table", "os-table");

    var head = text("tr");
    if (canUpdate) {
      var pickAll = text("th", "os-pick");
      var allBox = document.createElement("input");
      allBox.type = "checkbox";
      allBox.setAttribute("aria-label", "Select all packages");
      var pickedCount = updates.filter(function (u) {
        return state.picked[host.name + "/" + u.name];
      }).length;
      allBox.checked = updates.length > 0 && pickedCount === updates.length;
      allBox.indeterminate = pickedCount > 0 && pickedCount < updates.length;
      allBox.addEventListener("change", function () {
        updates.forEach(function (u) {
          state.picked[host.name + "/" + u.name] = allBox.checked;
        });
        render();
      });
      pickAll.appendChild(allBox);
      head.appendChild(pickAll);
    }
    ["Package", "Installed", "Available", "From"].forEach(function (label) {
      head.appendChild(text("th", null, label));
    });
    table.appendChild(head);

    updates.forEach(function (update) {
      var key = host.name + "/" + update.name;
      var row = text("tr", "os-row");

      if (canUpdate) {
        var pick = text("td", "os-pick");
        var box = document.createElement("input");
        box.type = "checkbox";
        box.checked = !!state.picked[key];
        box.setAttribute("aria-label", "Select " + update.name);
        box.addEventListener("click", function (event) { event.stopPropagation(); });
        box.addEventListener("change", function () {
          state.picked[key] = box.checked;
          renderUpdateBar(host);
        });
        pick.appendChild(box);
        row.appendChild(pick);
      }

      var first = text("td", "os-pkg");
      var level = OS_LEVELS.filter(function (l) { return l.key === update.severity; })[0];
      first.appendChild(dot(level ? level.status : "unknown"));
      first.appendChild(text("span", null, update.name));
      row.appendChild(first);
      row.appendChild(text("td", "mono", update.installed));
      row.appendChild(text("td", "mono", update.candidate));
      row.appendChild(text("td", "os-src", update.source || ""));

      row.tabIndex = 0;
      row.addEventListener("click", function () {
        state.open[key] = !state.open[key];
        render();
      });
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          state.open[key] = !state.open[key];
          render();
        }
      });
      table.appendChild(row);

      if (state.open[key]) table.appendChild(osDetailRow(update, canUpdate));
    });
    return table;
  }

  function osDetailRow(update, canUpdate) {
    var row = text("tr", "os-detail-row");
    var cell = text("td");
    cell.colSpan = canUpdate ? 5 : 4;

    var box = text("div", "os-detail");
    if (update.description) box.appendChild(text("p", "os-desc", update.description));

    var facts = text("dl", "os-facts");
    function fact(label, value) {
      if (!value) return;
      facts.appendChild(text("dt", null, label));
      facts.appendChild(text("dd", null, value));
    }
    fact("Severity", update.severity);
    fact("Section", update.section);
    fact("Priority", update.priority);
    fact("Architecture", update.architecture);
    if (update.source_package && update.source_package !== update.name) {
      fact("Source package", update.source_package);
    }
    fact("Download", bytes(update.download_bytes));
    fact("Installed size", bytes(update.installed_bytes));
    fact("Repository", update.source);
    box.appendChild(facts);

    if (update.homepage) {
      var link = document.createElement("a");
      link.href = update.homepage;
      link.textContent = update.homepage;
      link.rel = "noreferrer noopener";
      link.target = "_blank";
      link.className = "os-link";
      box.appendChild(link);
    }
    cell.appendChild(box);
    row.appendChild(cell);
    return row;
  }

  function renderHosts(data) {
    el.hosts.textContent = "";
    var hosts = (data.hosts || []).filter(function (host) {
      return !state.host || host.name === state.host;
    });

    var shown = 0;
    hosts.forEach(function (host) {
      var containers = (host.containers || []).filter(matches);
      shown += containers.length;
      if (!containers.length && host.online && (state.status || state.query)) return;
      el.hosts.appendChild(hostCard(host, containers));
    });

    if (!data.hosts || !data.hosts.length) {
      el.empty.hidden = false;
      el.empty.textContent = "";
      el.empty.appendChild(text("span", null, "No hosts configured yet. Use "));
      el.empty.appendChild(text("strong", null, "Add host"));
      el.empty.appendChild(text("span", null, " above, or register this machine with "));
      el.empty.appendChild(text("code", null, "./cud add --local"));
      el.empty.appendChild(text("span", null, "."));
    } else if (!shown && !el.hosts.children.length) {
      el.empty.hidden = false;
      el.empty.textContent = "Nothing matches the current filter.";
    } else {
      el.empty.hidden = true;
    }
  }

  function renderLegend() {
    el.legend.textContent = "";
    ["update-available", "restart-pending", "error", "up-to-date", "unknown"].forEach(function (key) {
      var item = text("span");
      item.appendChild(dot(key));
      item.appendChild(text("span", null, STATUS[key].label));
      el.legend.appendChild(item);
    });
  }

  function render() {
    var data = state.data;
    if (!data) return;
    renderHero(data);
    renderKpis(data);
    renderTabs(data);
    if (state.tab === "os") {
      renderOsFilters(data);
      renderOsHosts(data);
    } else {
      renderFilters(data);
      renderHosts(data);
    }

    if (data.refreshing) {
      el.subtitle.textContent = "Checking hosts and registries…";
    } else if (data.generated_at) {
      el.subtitle.textContent = "Checked " + relativeTime(data.generated_at) +
        " · took " + (data.duration_seconds || 0) + "s";
    } else {
      el.subtitle.textContent = "Waiting for the first check…";
    }
    el.refresh.classList.toggle("busy", !!data.refreshing);
    el.refresh.disabled = !!data.refreshing;

    var footer = [];
    if (data.generated_at) {
      footer.push("Last check " + new Date(data.generated_at * 1000).toLocaleString());
    }
    footer.push("Digests are compared against the registry; nothing is pulled.");
    el.footerMeta.textContent = footer.join(" · ");
  }

  // ---- data loading ------------------------------------------------------

  function load() {
    return fetch("/api/state", { headers: { Accept: "application/json" } })
      .then(function (response) {
        // The session ran out, or the server restarted. Go and sign in again
        // rather than sitting here showing a stale snapshot.
        if (response.status === 401) {
          window.location.replace("/login");
          throw new Error("signed out");
        }
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        state.data = data;
        render();
        schedulePoll(data.refreshing ? 2000 : 30000);
      })
      .catch(function (error) {
        el.subtitle.textContent = "Cannot reach the dashboard API (" + error.message + ")";
        el.refresh.classList.remove("busy");
        el.refresh.disabled = false;
        schedulePoll(10000);
      });
  }

  function schedulePoll(delay) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(load, delay);
  }

  function refresh() {
    el.refresh.classList.add("busy");
    el.refresh.disabled = true;
    fetch("/api/refresh", { method: "POST" })
      .then(function () { schedulePoll(600); })
      .catch(function () { schedulePoll(2000); });
  }

  // ---- first-run setup ---------------------------------------------------

  function checkSetup() {
    return fetch("/api/setup")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (info) {
        if (!info) return null;
        state.canAddHosts = info.can_add_hosts;
        el.signout.hidden = !info.can_add_hosts;
        if (info.needs_setup && !info.env_password) openSetup();
        return info;
      })
      .catch(function () { return null; });
  }

  function openSetup() {
    if (el.setupDialog.open) return;
    if (typeof el.setupDialog.showModal === "function") el.setupDialog.showModal();
  }

  // ---- settings ------------------------------------------------------------

  function loadSettings() {
    return fetch("/api/settings")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (settings) {
        if (settings) state.settings = settings;
        return settings;
      })
      .catch(function () { return null; });
  }

  function openSettings() {
    var s = state.settings || {};
    el.settingsSkipConfirmations.checked = !!s.skip_confirmations;
    el.settingsIncludeStopped.checked = s.include_stopped !== false;
    el.settingsRefreshInterval.value = s.refresh_interval_minutes || 30;
    el.settingsLogTail.value = s.log_tail_lines || 300;
    el.settingsLogAutoRefresh.checked = s.log_auto_refresh !== false;
    el.settingsLogRefresh.value = s.log_refresh_seconds || 5;
    el.settingsLogRefreshWrap.hidden = !el.settingsLogAutoRefresh.checked;
    hide(el.settingsError);
    if (typeof el.settingsDialog.showModal === "function") el.settingsDialog.showModal();
  }

  function submitSettings(event) {
    event.preventDefault();
    var body = {
      skip_confirmations: el.settingsSkipConfirmations.checked,
      include_stopped: el.settingsIncludeStopped.checked,
      refresh_interval_minutes: Number(el.settingsRefreshInterval.value) || 30,
      log_tail_lines: Number(el.settingsLogTail.value) || 300,
      log_auto_refresh: el.settingsLogAutoRefresh.checked,
      log_refresh_seconds: Number(el.settingsLogRefresh.value) || 5
    };
    postJSON("/api/settings", body)
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.settings = result;
        el.settingsDialog.close();
        render();
      })
      .catch(function (err) {
        showError(el.settingsError, err.message || "Could not save settings.");
      });
  }

  function submitSetup(event) {
    event.preventDefault();
    var username = el.setupUsername.value.trim();
    var password = el.setupPassword.value;
    if (password !== el.setupConfirm.value) {
      showError(el.setupError, "The two passwords do not match.");
      return;
    }
    el.setupSubmit.disabled = true;
    postJSON("/api/setup", { username: username, password: password })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        // The browser has no credentials for the realm yet, so the next request
        // would 401. A reload lets it prompt once, cleanly.
        // The server signed this browser in as part of saving, so there is
        // nothing to log into -- just show the dashboard.
        el.setupDialog.close();
        window.location.reload();
      })
      .catch(function (err) {
        showError(el.setupError, err.message || "Could not save those credentials.");
        el.setupSubmit.disabled = false;
      });
  }

  // ---- adding a host -----------------------------------------------------

  var enrollTimer = null;

  function openHostDialog() {
    if (!state.canAddHosts) {
      openSetup();
      return;
    }
    resetHostDialog();
    if (typeof el.hostDialog.showModal === "function") el.hostDialog.showModal();
  }

  function resetHostDialog() {
    if (enrollTimer) { clearTimeout(enrollTimer); enrollTimer = null; }
    hide(el.hostError);
    el.hostStep1.hidden = false;
    el.hostStep2.hidden = true;
    el.hostCopy.hidden = true;
    el.hostSubmit.hidden = false;
    el.hostSubmit.disabled = false;
    el.hostCommand.textContent = "";
  }

  function submitHost(event) {
    event.preventDefault();
    hide(el.hostError);
    el.hostSubmit.disabled = true;
    postJSON("/api/enrollments", {
      name: el.hostName.value.trim(),
      port: el.hostPort.value
    })
      .then(function (enrollment) {
        if (enrollment.error) throw new Error(enrollment.error);
        showCommand(enrollment);
        watchEnrollment(enrollment.id);
      })
      .catch(function (err) {
        showError(el.hostError, err.message || "Could not create an enrolment.");
        el.hostSubmit.disabled = false;
      });
  }

  function showCommand(enrollment) {
    el.hostStep1.hidden = true;
    el.hostStep2.hidden = false;
    el.hostSubmit.hidden = true;
    el.hostCopy.hidden = false;
    el.hostCommand.textContent = enrollment.command;
    el.hostExpiry.textContent =
      "Single use. Expires in " + Math.round(enrollment.expires_in / 60) + " minutes.";
  }

  function watchEnrollment(id) {
    fetch("/api/enrollments/" + encodeURIComponent(id))
      .then(function (r) { return r.json(); })
      .then(function (item) {
        if (item.status === "registered") {
          el.hostStatus.innerHTML = "";
          el.hostStatus.textContent =
            "Registered " + ((item.host && item.host.name) || "the host") + ".";
          el.hostStatus.classList.add("done");
          load();
          return;
        }
        if (item.status === "expired" || item.status === "failed") {
          hide(el.hostStatus);
          showError(el.hostError, item.error || "That command expired. Close and try again.");
          return;
        }
        enrollTimer = setTimeout(function () { watchEnrollment(id); }, 2000);
      })
      .catch(function () {
        enrollTimer = setTimeout(function () { watchEnrollment(id); }, 4000);
      });
  }

  function copyCommand() {
    var text = el.hostCommand.textContent;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(function () {
        el.hostCopy.textContent = "Copied";
        setTimeout(function () { el.hostCopy.textContent = "Copy command"; }, 1500);
      });
    }
  }

  // ---- confirm dialog ------------------------------------------------------

  function confirmDialog(title, message, options) {
    options = options || {};
    if (state.settings && state.settings.skip_confirmations) {
      return Promise.resolve(true);
    }

    el.confirmTitle.textContent = title || "Are you sure?";
    el.confirmMessage.textContent = message || "";
    el.confirmOk.textContent = options.confirmLabel || "Confirm";
    el.confirmOk.className = "button" + (options.danger ? " danger" : " primary");

    return new Promise(function (resolve) {
      function onClose() {
        el.confirmDialog.removeEventListener("close", onClose);
        resolve(el.confirmDialog.returnValue === "ok");
      }
      el.confirmDialog.addEventListener("close", onClose);
      if (typeof el.confirmDialog.showModal === "function") {
        el.confirmDialog.showModal();
      } else {
        resolve(window.confirm([title, message].filter(Boolean).join("\n\n")));
      }
    });
  }

  // ---- small helpers -----------------------------------------------------

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  function showError(node, message) {
    node.textContent = message;
    node.hidden = false;
  }

  function hide(node) {
    node.hidden = true;
  }

  // ---- theme -------------------------------------------------------------

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem("cud-theme"); } catch (e) { /* private mode */ }
    if (saved) document.documentElement.setAttribute("data-theme", saved);
    el.theme.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      if (!current) {
        current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      var next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("cud-theme", next); } catch (e) { /* ignore */ }
    });
  }

  // ---- boot --------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    el = {
      subtitle: $("subtitle"),
      refresh: $("refresh"),
      theme: $("theme"),
      signout: $("signout"),
      heroValue: $("hero-value"),
      heroLabel: $("hero-label"),
      heroNote: $("hero-note"),
      kpis: $("kpis"),
      statusFilters: $("status-filters"),
      hostFilter: $("host-filter"),
      search: $("search"),
      hosts: $("hosts"),
      empty: $("empty"),
      footerMeta: $("footer-meta"),
      legend: $("legend"),
      addHost: $("add-host"),
      tabContainers: $("tab-containers"),
      tabOs: $("tab-os"),
      tabContainersCount: $("tab-containers-count"),
      tabOsCount: $("tab-os-count"),
      setupDialog: $("setup-dialog"),
      setupForm: $("setup-form"),
      setupUsername: $("setup-username"),
      setupPassword: $("setup-password"),
      setupConfirm: $("setup-confirm"),
      setupSubmit: $("setup-submit"),
      setupError: $("setup-error"),
      hostDialog: $("host-dialog"),
      hostForm: $("host-form"),
      hostStep1: $("host-step-1"),
      hostStep2: $("host-step-2"),
      hostCommand: $("host-command"),
      hostExpiry: $("host-expiry"),
      hostStatus: $("host-status"),
      hostCopy: $("host-copy"),
      hostName: $("host-name"),
      hostPort: $("host-port"),
      hostSubmit: $("host-submit"),
      hostCancel: $("host-cancel"),
      hostError: $("host-error"),
      confirmDialog: $("confirm-dialog"),
      confirmTitle: $("confirm-title"),
      confirmMessage: $("confirm-message"),
      confirmOk: $("confirm-ok"),
      confirmCancel: $("confirm-cancel"),
      settingsOpen: $("settings-open"),
      settingsDialog: $("settings-dialog"),
      settingsForm: $("settings-form"),
      settingsCancel: $("settings-cancel"),
      settingsError: $("settings-error"),
      settingsSkipConfirmations: $("settings-skip-confirmations"),
      settingsIncludeStopped: $("settings-include-stopped"),
      settingsRefreshInterval: $("settings-refresh-interval"),
      settingsLogTail: $("settings-log-tail"),
      settingsLogAutoRefresh: $("settings-log-auto-refresh"),
      settingsLogRefresh: $("settings-log-refresh"),
      settingsLogRefreshWrap: $("settings-log-refresh-wrap")
    };

    initTheme();
    renderLegend();
    checkSetup();
    loadSettings();

    el.refresh.addEventListener("click", refresh);
    el.addHost.addEventListener("click", openHostDialog);
    el.tabContainers.addEventListener("click", function () { selectTab("containers"); });
    el.tabOs.addEventListener("click", function () { selectTab("os"); });
    el.setupForm.addEventListener("submit", submitSetup);
    el.hostForm.addEventListener("submit", submitHost);
    el.hostCancel.addEventListener("click", function () { el.hostDialog.close(); });
    el.hostCopy.addEventListener("click", copyCommand);
    el.confirmCancel.addEventListener("click", function () { el.confirmDialog.close(""); });
    el.settingsOpen.addEventListener("click", openSettings);
    el.settingsForm.addEventListener("submit", submitSettings);
    el.settingsCancel.addEventListener("click", function () { el.settingsDialog.close(); });
    el.settingsLogAutoRefresh.addEventListener("change", function () {
      el.settingsLogRefreshWrap.hidden = !el.settingsLogAutoRefresh.checked;
    });
    el.signout.addEventListener("click", function () {
      fetch("/api/logout", { method: "POST" })
        .then(function () { window.location.replace("/login"); })
        .catch(function () { window.location.replace("/login"); });
    });
    el.hostFilter.addEventListener("change", function () {
      state.host = el.hostFilter.value;
      render();
    });

    var searchTimer = null;
    el.search.addEventListener("input", function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        var value = el.search.value.trim().toLowerCase();
        if (state.tab === "os") state.osQuery = value; else state.query = value;
        render();
      }, 120);
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "/" && document.activeElement !== el.search) {
        event.preventDefault();
        el.search.focus();
      }
    });

    load();
  });
})();
