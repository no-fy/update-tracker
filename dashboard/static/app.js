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

  var state = { data: null, status: "", host: "", query: "", open: {}, canAddHosts: false };
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

  function statusRank(status) {
    var meta = STATUS[status];
    return meta ? meta.order : 99;
  }

  // ---- rendering ---------------------------------------------------------

  function renderHero(data) {
    var summary = data.summary || {};
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
    el.kpis.appendChild(kpi(
      "Hosts online", null,
      (summary.hosts_online || 0) + "/" + (summary.hosts_total || 0),
      summary.containers_total + " " + plural(summary.containers_total, "container", "containers") +
        ", " + (summary.containers_running || 0) + " running"
    ));
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

  function containerRow(container) {
    var key = container.host + "/" + container.id;
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

    var detail = text("tr", "detail");
    var cell = document.createElement("td");
    cell.colSpan = 5;
    var body = text("div", "detail-body");
    body.appendChild(text("p", "detail-note", container.detail || ""));

    function item(label, value) {
      if (!value) return;
      var wrapper = text("dl", "detail-item");
      wrapper.appendChild(text("dt", null, label));
      wrapper.appendChild(text("dd", null, value));
      body.appendChild(wrapper);
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

    cell.appendChild(body);
    detail.appendChild(cell);
    detail.hidden = !state.open[key];

    function toggle() {
      state.open[key] = !state.open[key];
      detail.hidden = !state.open[key];
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
    [["Container", ""], ["Image", ""], ["State", "optional"], ["Image age", "optional"], ["Update", ""]]
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
        containerRow(container).forEach(function (node) { tbody.appendChild(node); });
      });
    table.appendChild(tbody);
    scroll.appendChild(table);
    card.appendChild(scroll);
    return card;
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
      el.empty.appendChild(text("span", null, "No hosts configured yet. Add one with "));
      el.empty.appendChild(text("code", null, "./cud add --local"));
      el.empty.appendChild(text("span", null, " or "));
      el.empty.appendChild(text("code", null, "./cud add root@your-server"));
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
    renderFilters(data);
    renderHosts(data);

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
        if (info.needs_setup && !info.env_password) openSetup();
        return info;
      })
      .catch(function () { return null; });
  }

  function openSetup() {
    if (el.setupDialog.open) return;
    if (typeof el.setupDialog.showModal === "function") el.setupDialog.showModal();
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
      hostError: $("host-error")
    };

    initTheme();
    renderLegend();
    checkSetup();

    el.refresh.addEventListener("click", refresh);
    el.addHost.addEventListener("click", openHostDialog);
    el.setupForm.addEventListener("submit", submitSetup);
    el.hostForm.addEventListener("submit", submitHost);
    el.hostCancel.addEventListener("click", function () { el.hostDialog.close(); });
    el.hostCopy.addEventListener("click", copyCommand);
    el.hostFilter.addEventListener("change", function () {
      state.host = el.hostFilter.value;
      render();
    });

    var searchTimer = null;
    el.search.addEventListener("input", function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        state.query = el.search.value.trim().toLowerCase();
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
