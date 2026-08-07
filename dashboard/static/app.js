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
    composeQuery: "", composeStatus: "", // compose tab
    eventsQuery: "", eventsData: null, // events tab
    imagesQuery: "", imagesFilter: "", imagesData: null,     // images tab
    volumesQuery: "", volumesFilter: "", volumesData: null,  // volumes tab
    networksQuery: "", networksData: null,                   // networks tab
    host: "", open: {}, picked: {}, osJobs: {}, canAddHosts: false,
    containerJobs: {}, logsOpen: {}, logs: {}, renaming: {}, settings: {}, logsRange: {},
    stackOpen: {}, stackTemplates: [], stackFileContext: null,
    configOpen: {}, config: {}, stats: {}, limitsJobs: {}, portMapOpen: false,
    registries: [],
    assistant: { open: false, wire: [], display: [], loading: false, error: null }
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

  function searchPlaceholder(tab) {
    if (tab === "os") return "Search package or version…";
    if (tab === "compose") return "Search stack or service…";
    if (tab === "events") return "Search events…";
    if (tab === "images") return "Search image tag…";
    if (tab === "volumes") return "Search volume name…";
    if (tab === "networks") return "Search network name…";
    return "Search name or image…";
  }

  function queryFor(tab) {
    if (tab === "os") return state.osQuery;
    if (tab === "compose") return state.composeQuery;
    if (tab === "events") return state.eventsQuery;
    if (tab === "images") return state.imagesQuery;
    if (tab === "volumes") return state.volumesQuery;
    if (tab === "networks") return state.networksQuery;
    return state.query;
  }

  var TABS = ["containers", "compose", "os", "events", "images", "volumes", "networks"];

  function renderTabs(data) {
    var summary = data.summary || {};
    el.tabContainersCount.textContent = summary.containers_total || 0;
    el.tabOsCount.textContent = summary.os_updates_total || 0;
    el.tabComposeCount.textContent = summary.stacks_total || 0;
    TABS.forEach(function (tab) {
      el["tab" + tab.charAt(0).toUpperCase() + tab.slice(1)]
        .setAttribute("aria-selected", state.tab === tab ? "true" : "false");
    });
    el.tabOs.hidden = !summary.os_hosts_reporting && !osHosts(data).length;
    el.tabCompose.hidden = !summary.stacks_total;
    el.search.placeholder = searchPlaceholder(state.tab);
    updateTabAction();
  }

  function selectTab(tab) {
    if (state.tab === tab) return;
    state.tab = tab;
    // Each tab keeps its own query, so switching never applies a search that
    // was meant for the other list.
    el.search.value = queryFor(tab);
    stopEventsPolling();
    if (tab === "events") {
      loadAllEvents();
      startEventsPolling();
    } else if (tab === "images" && !state.imagesData) {
      loadImages();
    } else if (tab === "volumes" && !state.volumesData) {
      loadVolumes();
    } else if (tab === "networks" && !state.networksData) {
      loadNetworks();
    }
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

  // ---- Compose stacks -------------------------------------------------------

  function stackKey(host, stack) { return host.name + "/" + stack.project; }

  function stackMatches(stack) {
    if (!state.composeQuery) return true;
    var q = state.composeQuery;
    if (stack.project.toLowerCase().indexOf(q) !== -1) return true;
    return stack.services.some(function (s) {
      return (s.service || "").toLowerCase().indexOf(q) !== -1 ||
        (s.container_name || "").toLowerCase().indexOf(q) !== -1;
    });
  }

  function stacksForHost(host) {
    return (host.stacks || []).filter(stackMatches);
  }

  function renderComposeFilters(data) {
    var stacks = [];
    (data.hosts || []).forEach(function (host) { stacks = stacks.concat(host.stacks || []); });
    var attention = stacks.filter(function (s) { return s.needs_attention; }).length;
    var options = [{ key: "", label: "All", count: stacks.length }];
    if (attention) options.push({ key: "attention", label: "Needs attention", count: attention });
    chipRow(el.statusFilters, options, state.composeStatus || "", function (key) {
      state.composeStatus = state.composeStatus === key ? "" : key;
      render();
    });
    fillHostFilter((data.hosts || []).filter(function (h) { return (h.stacks || []).length; }));
  }

  function renderComposeHosts(data) {
    el.hosts.textContent = "";
    var hosts = (data.hosts || []).filter(function (host) {
      return !state.host || host.name === state.host;
    });

    var shown = 0;
    hosts.forEach(function (host) {
      var stacks = stacksForHost(host).filter(function (s) {
        return !state.composeStatus || (state.composeStatus === "attention" && s.needs_attention);
      });
      shown += stacks.length;
      if (!stacks.length) return;
      el.hosts.appendChild(hostComposeCard(host, stacks));
    });

    if (!shown && !el.hosts.children.length) {
      el.empty.hidden = false;
      el.empty.textContent = state.composeQuery || state.composeStatus
        ? "No stacks match the current filter."
        : "No Compose stacks detected -- containers need the com.docker.compose.project label, " +
          "which Docker Compose sets on its own.";
    } else {
      el.empty.hidden = true;
    }
  }

  function hostComposeCard(host, stacks) {
    var card = text("section", "host");
    var head = text("div", "host-head");
    var title = text("div", "host-title");
    title.appendChild(dot(
      !host.online ? "error" : (stacks.some(function (s) { return s.needs_attention; })
        ? "update-available" : "up-to-date")
    ));
    title.appendChild(text("span", "name", host.label || host.name));
    head.appendChild(title);
    var meta = text("div", "host-meta");
    meta.appendChild(text("span", null, stacks.length + " " + plural(stacks.length, "stack", "stacks")));
    head.appendChild(meta);
    card.appendChild(head);

    var scroll = text("div", "table-scroll");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    [["Stack", ""], ["Services", ""], ["Status", ""], ["Actions", ""]].forEach(function (column) {
      headRow.appendChild(text("th", column[1], column[0]));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    stacks.forEach(function (stack) {
      stackRows(stack, host).forEach(function (node) { tbody.appendChild(node); });
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    card.appendChild(scroll);
    return card;
  }

  function stackRows(stack, host) {
    var key = stackKey(host, stack);
    var row = text("tr", "row");
    row.tabIndex = 0;

    var name = text("td", "c-name");
    name.appendChild(text("span", null, stack.project));
    if (stack.workdir) name.appendChild(text("span", "sub", stack.workdir));
    row.appendChild(name);

    row.appendChild(text("td", null,
      stack.services_running + "/" + stack.services_total + " running"));

    var status = text("td", "c-status");
    status.appendChild(badge(stack.update_status));
    row.appendChild(status);

    var actionsCell = text("td", "c-actions");
    actionsCell.addEventListener("click", function (event) { event.stopPropagation(); });
    if (stackConfigPath(stack)) {
      var redeployBtn = text("button", "button small");
      redeployBtn.type = "button";
      redeployBtn.textContent = "Redeploy";
      redeployBtn.disabled = !(host.stack_redeploy && host.stack_redeploy.can_redeploy);
      redeployBtn.title = redeployBtn.disabled
        ? ((host.stack_redeploy && host.stack_redeploy.reason) || "Not available on this host.")
        : "";
      redeployBtn.addEventListener("click", function () { runRedeployStack(host, stack); });
      actionsCell.appendChild(redeployBtn);

      var viewBtn = text("button", "button small");
      viewBtn.type = "button";
      viewBtn.textContent = "View/Edit";
      viewBtn.addEventListener("click", function () { openStackFile(host, stack); });
      actionsCell.appendChild(viewBtn);
    }
    row.appendChild(actionsCell);

    var detail = text("tr", "detail");
    var cell = document.createElement("td");
    cell.colSpan = 4;
    var body = text("div", "detail-body");
    var serviceTable = text("table", "stack-services");
    var serviceHead = document.createElement("tr");
    ["Service", "Container", "State", "Image", "Update"].forEach(function (label) {
      serviceHead.appendChild(text("th", null, label));
    });
    serviceTable.appendChild(serviceHead);
    stack.services.forEach(function (service) {
      var serviceRow = document.createElement("tr");
      serviceRow.appendChild(text("td", null, service.service));
      serviceRow.appendChild(text("td", null, service.container_name));
      serviceRow.appendChild(text("td", null, service.status || service.state || "—"));
      serviceRow.appendChild(text("td", "c-image", service.image || "—"));
      var updateCell = document.createElement("td");
      updateCell.appendChild(badge(service.update_status));
      serviceRow.appendChild(updateCell);
      serviceTable.appendChild(serviceRow);
    });
    body.appendChild(serviceTable);
    cell.appendChild(body);
    detail.appendChild(cell);
    detail.hidden = !state.stackOpen[key];

    function toggle() { state.stackOpen[key] = !state.stackOpen[key]; detail.hidden = !state.stackOpen[key]; }
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
    });

    return [row, detail];
  }

  // ---- Images / Volumes / Networks -----------------------------------------
  // Same shape for all three: fetch each online host's list on demand (not
  // part of the main poll -- these change far less often than containers),
  // group by host, cross-reference against the containers already in
  // state.data to show what's using what.

  function loadHostResource(endpoint) {
    var data = state.data;
    if (!data) return Promise.resolve({});
    var hosts = (data.hosts || []).filter(function (h) { return h.online; });
    return Promise.all(hosts.map(function (host) {
      return fetch("/api/hosts/" + encodeURIComponent(host.name) + "/" + endpoint)
        .then(function (r) { return r.json(); })
        .then(function (result) { return { host: host, result: result }; })
        .catch(function (err) {
          return { host: host, result: { error: err.message || "Could not load." } };
        });
    })).then(function (entries) {
      var byHost = {};
      entries.forEach(function (entry) { byHost[entry.host.name] = entry; });
      return byHost;
    });
  }

  function allContainers(data) {
    var out = [];
    (data.hosts || []).forEach(function (host) {
      (host.containers || []).forEach(function (c) { out.push(c); });
    });
    return out;
  }

  // -- Images --

  function loadImages(silent) {
    if (!silent) { state.imagesData = { loading: true }; render(); }
    loadHostResource("images").then(function (byHost) {
      state.imagesData = { byHost: byHost };
      render();
    });
  }

  function renderImagesFilters(data) {
    var options = [{ key: "", label: "All" }, { key: "dangling", label: "Dangling only" }];
    chipRow(el.statusFilters, options, state.imagesFilter || "", function (key) {
      state.imagesFilter = state.imagesFilter === key ? "" : key;
      render();
    });
    fillHostFilter(data.hosts || []);
  }

  function renderImagesTab(data) {
    el.hosts.textContent = "";
    var entry = state.imagesData;
    if (!entry || entry.loading) {
      el.hosts.appendChild(text("p", "os-running", "Loading images…"));
      el.empty.hidden = true;
      return;
    }
    var containers = allContainers(data);
    var shown = 0;
    (data.hosts || []).forEach(function (host) {
      if (state.host && host.name !== state.host) return;
      var hostEntry = (entry.byHost || {})[host.name];
      if (!hostEntry) return;
      if (hostEntry.result.error) {
        el.hosts.appendChild(errorCard(host, hostEntry.result.error));
        return;
      }
      var images = (hostEntry.result.images || []).filter(function (img) {
        if (state.imagesFilter === "dangling" && !img.dangling) return false;
        if (state.imagesQuery) {
          var haystack = (img.tags.join(" ") + " " + img.id).toLowerCase();
          if (haystack.indexOf(state.imagesQuery) === -1) return false;
        }
        return true;
      });
      shown += images.length;
      if (!images.length) return;
      el.hosts.appendChild(imagesCard(host, images, containers));
    });
    el.empty.hidden = !!shown;
    if (!shown) el.empty.textContent = "No images match the current filter.";
  }

  function imagesCard(host, images, containers) {
    var card = text("section", "host");
    var head = text("div", "host-head");
    var title = text("div", "host-title");
    title.appendChild(dot("unknown"));
    title.appendChild(text("span", "name", host.label || host.name));
    head.appendChild(title);
    head.appendChild(text("div", "host-meta", images.length + " " + plural(images.length, "image", "images")));
    card.appendChild(head);

    var scroll = text("div", "table-scroll");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    [["Tags", ""], ["Size", "optional"], ["Created", "optional"], ["Used by", ""]].forEach(function (c) {
      headRow.appendChild(text("th", c[1], c[0]));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    images.forEach(function (img) {
      var users = containers.filter(function (c) {
        return c.host === host.name && (c.image_id === img.full_id || c.current_image_id === img.full_id);
      });
      var row = text("tr", "row");
      var nameCell = text("td", "c-name");
      nameCell.appendChild(text("span", null, img.tags.length ? img.tags.join(", ") : "<dangling>"));
      nameCell.appendChild(text("span", "sub", img.id));
      row.appendChild(nameCell);
      row.appendChild(text("td", "c-age optional", bytes(img.size) || "—"));
      row.appendChild(text("td", "c-age optional", relativeTime(img.created) || "—"));
      row.appendChild(text("td", null, users.length
        ? users.map(function (c) { return c.name; }).join(", ")
        : "unused"));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    card.appendChild(scroll);
    return card;
  }

  // -- Volumes --

  function loadVolumes(silent) {
    if (!silent) { state.volumesData = { loading: true }; render(); }
    loadHostResource("volumes").then(function (byHost) {
      state.volumesData = { byHost: byHost };
      render();
    });
  }

  function renderVolumesFilters(data) {
    var options = [{ key: "", label: "All" }, { key: "orphaned", label: "Orphaned only" }];
    chipRow(el.statusFilters, options, state.volumesFilter || "", function (key) {
      state.volumesFilter = state.volumesFilter === key ? "" : key;
      render();
    });
    fillHostFilter(data.hosts || []);
  }

  function volumeUsers(containers, hostName, volumeName) {
    return containers.filter(function (c) {
      if (c.host !== hostName) return false;
      return (c.mounts || []).some(function (m) { return m.type === "volume" && m.name === volumeName; });
    });
  }

  function renderVolumesTab(data) {
    el.hosts.textContent = "";
    var entry = state.volumesData;
    if (!entry || entry.loading) {
      el.hosts.appendChild(text("p", "os-running", "Loading volumes…"));
      el.empty.hidden = true;
      return;
    }
    var containers = allContainers(data);
    var shown = 0;
    (data.hosts || []).forEach(function (host) {
      if (state.host && host.name !== state.host) return;
      var hostEntry = (entry.byHost || {})[host.name];
      if (!hostEntry) return;
      if (hostEntry.result.error) {
        el.hosts.appendChild(errorCard(host, hostEntry.result.error));
        return;
      }
      var volumes = (hostEntry.result.volumes || []).filter(function (vol) {
        var users = volumeUsers(containers, host.name, vol.name);
        if (state.volumesFilter === "orphaned" && users.length) return false;
        if (state.volumesQuery && vol.name.toLowerCase().indexOf(state.volumesQuery) === -1) return false;
        return true;
      });
      shown += volumes.length;
      if (!volumes.length) return;
      el.hosts.appendChild(volumesCard(host, volumes, containers));
    });
    el.empty.hidden = !!shown;
    if (!shown) el.empty.textContent = "No volumes match the current filter.";
  }

  function volumesCard(host, volumes, containers) {
    var card = text("section", "host");
    var head = text("div", "host-head");
    var title = text("div", "host-title");
    title.appendChild(dot("unknown"));
    title.appendChild(text("span", "name", host.label || host.name));
    head.appendChild(title);
    head.appendChild(text("div", "host-meta", volumes.length + " " + plural(volumes.length, "volume", "volumes")));
    card.appendChild(head);

    var scroll = text("div", "table-scroll");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    [["Name", ""], ["Driver", "optional"], ["Created", "optional"], ["Used by", ""]].forEach(function (c) {
      headRow.appendChild(text("th", c[1], c[0]));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    volumes.forEach(function (vol) {
      var users = volumeUsers(containers, host.name, vol.name);
      var row = text("tr", "row");
      var nameCell = text("td", "c-name");
      nameCell.appendChild(text("span", null, vol.name));
      nameCell.appendChild(text("span", "sub", vol.mountpoint));
      row.appendChild(nameCell);
      row.appendChild(text("td", "optional", vol.driver || "—"));
      row.appendChild(text("td", "c-age optional", relativeTime(vol.created) || "—"));
      row.appendChild(text("td", null, users.length
        ? users.map(function (c) { return c.name; }).join(", ")
        : "orphaned"));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    card.appendChild(scroll);
    return card;
  }

  // -- Networks --

  function loadNetworks(silent) {
    if (!silent) { state.networksData = { loading: true }; render(); }
    loadHostResource("networks").then(function (byHost) {
      state.networksData = { byHost: byHost };
      render();
    });
  }

  function renderNetworksFilters(data) {
    el.statusFilters.textContent = "";
    fillHostFilter(data.hosts || []);
  }

  function networkUsers(containers, hostName, networkName) {
    return containers.filter(function (c) {
      return c.host === hostName && (c.networks || []).indexOf(networkName) !== -1;
    });
  }

  function renderNetworksTab(data) {
    el.hosts.textContent = "";
    var entry = state.networksData;
    if (!entry || entry.loading) {
      el.hosts.appendChild(text("p", "os-running", "Loading networks…"));
      el.empty.hidden = true;
      return;
    }
    var containers = allContainers(data);
    var shown = 0;
    (data.hosts || []).forEach(function (host) {
      if (state.host && host.name !== state.host) return;
      var hostEntry = (entry.byHost || {})[host.name];
      if (!hostEntry) return;
      if (hostEntry.result.error) {
        el.hosts.appendChild(errorCard(host, hostEntry.result.error));
        return;
      }
      var networks = (hostEntry.result.networks || []).filter(function (net) {
        if (!state.networksQuery) return true;
        return net.name.toLowerCase().indexOf(state.networksQuery) !== -1;
      });
      shown += networks.length;
      if (!networks.length) return;
      el.hosts.appendChild(networksCard(host, networks, containers));
    });
    el.empty.hidden = !!shown;
    if (!shown) el.empty.textContent = "No networks match the current filter.";
  }

  function networksCard(host, networks, containers) {
    var card = text("section", "host");
    var head = text("div", "host-head");
    var title = text("div", "host-title");
    title.appendChild(dot("unknown"));
    title.appendChild(text("span", "name", host.label || host.name));
    head.appendChild(title);
    head.appendChild(text("div", "host-meta", networks.length + " " + plural(networks.length, "network", "networks")));
    card.appendChild(head);

    var scroll = text("div", "table-scroll");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    [["Name", ""], ["Driver", "optional"], ["Subnet", "optional"], ["Attached", ""]].forEach(function (c) {
      headRow.appendChild(text("th", c[1], c[0]));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    networks.forEach(function (net) {
      var users = networkUsers(containers, host.name, net.name);
      var row = text("tr", "row");
      var nameCell = text("td", "c-name");
      nameCell.appendChild(text("span", null, net.name));
      if (net.internal) nameCell.appendChild(text("span", "sub", "internal"));
      row.appendChild(nameCell);
      row.appendChild(text("td", "optional", net.driver || "—"));
      row.appendChild(text("td", "optional", net.subnets.join(", ") || "—"));
      row.appendChild(text("td", null, users.length
        ? users.map(function (c) { return c.name; }).join(", ")
        : "none"));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    card.appendChild(scroll);
    return card;
  }

  function errorCard(host, message) {
    var card = text("section", "host");
    var head = text("div", "host-head");
    var title = text("div", "host-title");
    title.appendChild(dot("error"));
    title.appendChild(text("span", "name", host.label || host.name));
    head.appendChild(title);
    card.appendChild(head);
    var error = text("div", "host-error");
    error.appendChild(dot("error"));
    error.appendChild(text("span", null, message));
    card.appendChild(error);
    return card;
  }

  // ---- creation flows: pull/build image, create volume/network/container,
  // clone a container, deploy a stack. Every host select below is filtered
  // to hosts that actually allow it -- capability re-checked server-side
  // regardless of what this list shows, same as every other write path.

  function eligibleHosts(data, capabilityOf) {
    return (data.hosts || []).filter(function (host) {
      if (!host.online) return false;
      var cap = capabilityOf(host);
      return !!(cap && (cap.can_manage || cap.can_deploy));
    });
  }

  function fillSelect(select, hosts, emptyLabel) {
    select.innerHTML = "";
    if (!hosts.length) {
      var opt = text("option", null, emptyLabel || "No eligible hosts");
      opt.value = "";
      opt.disabled = true;
      opt.selected = true;
      select.appendChild(opt);
      return;
    }
    hosts.forEach(function (host) {
      var option = text("option", null, host.label || host.name);
      option.value = host.name;
      select.appendChild(option);
    });
  }

  function watchJob(url, logEl, onDone) {
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (job) {
        if (job.error) throw new Error(job.error);
        if (logEl) {
          logEl.textContent = (job.lines || []).join("\n");
          logEl.scrollTop = logEl.scrollHeight;
        }
        if (job.status === "running") {
          setTimeout(function () { watchJob(url, logEl, onDone); }, 1000);
        } else if (onDone) {
          onDone(job);
        }
      })
      .catch(function () {
        setTimeout(function () { watchJob(url, logEl, onDone); }, 2000);
      });
  }

  function afterWrite() {
    fetch("/api/refresh", { method: "POST" }).then(function () { setTimeout(load, 1200); });
  }

  // -- Pull image --

  function openPullImage() {
    var hosts = eligibleHosts(state.data, function (h) { return h.container_actions; });
    fillSelect(el.pullImageHost, hosts);
    el.pullImageRepo.value = "";
    el.pullImageTag.value = "";
    el.pullImageLog.hidden = true;
    el.pullImageLog.textContent = "";
    hide(el.pullImageError);
    el.pullImageSubmit.disabled = false;
    if (typeof el.pullImageDialog.showModal === "function") el.pullImageDialog.showModal();
  }

  function submitPullImage(event) {
    event.preventDefault();
    var host = el.pullImageHost.value;
    var repo = el.pullImageRepo.value.trim();
    var tag = el.pullImageTag.value.trim() || "latest";
    if (!host || !repo) return;
    el.pullImageSubmit.disabled = true;
    hide(el.pullImageError);
    postJSON("/api/hosts/" + encodeURIComponent(host) + "/images/pull",
      { repository: repo, reference: tag })
      .then(function (job) {
        if (job.error) throw new Error(job.error);
        el.pullImageLog.hidden = false;
        watchJob(
          "/api/hosts/" + encodeURIComponent(host) + "/images/job/" + encodeURIComponent(job.id),
          el.pullImageLog,
          function (finalJob) {
            el.pullImageSubmit.disabled = false;
            if (finalJob.status === "ok" && state.tab === "images") loadImages(true);
          }
        );
      })
      .catch(function (err) {
        el.pullImageSubmit.disabled = false;
        showError(el.pullImageError, err.message || "Could not start the pull.");
      });
  }

  // -- Build image --

  function openBuildImage() {
    var hosts = eligibleHosts(state.data, function (h) { return h.container_actions; });
    fillSelect(el.buildImageHost, hosts);
    el.buildImageTag.value = "";
    el.buildImageDockerfile.value = "";
    el.buildImageLog.hidden = true;
    el.buildImageLog.textContent = "";
    hide(el.buildImageError);
    el.buildImageSubmit.disabled = false;
    if (typeof el.buildImageDialog.showModal === "function") el.buildImageDialog.showModal();
  }

  function submitBuildImage(event) {
    event.preventDefault();
    var host = el.buildImageHost.value;
    var dockerfile = el.buildImageDockerfile.value;
    if (!host || !dockerfile.trim()) return;
    el.buildImageSubmit.disabled = true;
    hide(el.buildImageError);
    postJSON("/api/hosts/" + encodeURIComponent(host) + "/images/build",
      { dockerfile: dockerfile, tag: el.buildImageTag.value.trim() })
      .then(function (job) {
        if (job.error) throw new Error(job.error);
        el.buildImageLog.hidden = false;
        watchJob(
          "/api/hosts/" + encodeURIComponent(host) + "/images/job/" + encodeURIComponent(job.id),
          el.buildImageLog,
          function (finalJob) {
            el.buildImageSubmit.disabled = false;
            if (finalJob.status === "ok" && state.tab === "images") loadImages(true);
          }
        );
      })
      .catch(function (err) {
        el.buildImageSubmit.disabled = false;
        showError(el.buildImageError, err.message || "Could not start the build.");
      });
  }

  // -- Create volume --

  function openCreateVolume() {
    var hosts = eligibleHosts(state.data, function (h) { return h.container_actions; });
    fillSelect(el.createVolumeHost, hosts);
    el.createVolumeName.value = "";
    el.createVolumeDriver.value = "";
    hide(el.createVolumeError);
    if (typeof el.createVolumeDialog.showModal === "function") el.createVolumeDialog.showModal();
  }

  function submitCreateVolume(event) {
    event.preventDefault();
    var host = el.createVolumeHost.value;
    if (!host) return;
    hide(el.createVolumeError);
    postJSON("/api/hosts/" + encodeURIComponent(host) + "/volumes", {
      name: el.createVolumeName.value.trim(),
      driver: el.createVolumeDriver.value.trim(),
    })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        el.createVolumeDialog.close();
        if (state.tab === "volumes") loadVolumes(true);
      })
      .catch(function (err) {
        showError(el.createVolumeError, err.message || "Could not create the volume.");
      });
  }

  // -- Create network --

  function openCreateNetwork() {
    var hosts = eligibleHosts(state.data, function (h) { return h.container_actions; });
    fillSelect(el.createNetworkHost, hosts);
    el.createNetworkName.value = "";
    el.createNetworkDriver.value = "";
    el.createNetworkInternal.checked = false;
    el.createNetworkSubnet.value = "";
    el.createNetworkGateway.value = "";
    hide(el.createNetworkError);
    if (typeof el.createNetworkDialog.showModal === "function") el.createNetworkDialog.showModal();
  }

  function submitCreateNetwork(event) {
    event.preventDefault();
    var host = el.createNetworkHost.value;
    var name = el.createNetworkName.value.trim();
    if (!host || !name) return;
    hide(el.createNetworkError);
    postJSON("/api/hosts/" + encodeURIComponent(host) + "/networks", {
      name: name,
      driver: el.createNetworkDriver.value.trim(),
      internal: el.createNetworkInternal.checked,
      subnet: el.createNetworkSubnet.value.trim(),
      gateway: el.createNetworkGateway.value.trim(),
    })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        el.createNetworkDialog.close();
        if (state.tab === "networks") loadNetworks(true);
      })
      .catch(function (err) {
        showError(el.createNetworkError, err.message || "Could not create the network.");
      });
  }

  // -- Create / clone container --

  function specRemoveButton(row) {
    var remove = text("button", "spec-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove");
    remove.addEventListener("click", function () { row.remove(); });
    return remove;
  }

  function addEnvRow(value) {
    var row = text("div", "spec-row");
    var input = document.createElement("input");
    input.placeholder = "KEY=value";
    input.value = value || "";
    row.appendChild(input);
    row.appendChild(specRemoveButton(row));
    el.createContainerEnvRows.appendChild(row);
  }

  function addPortRow(port) {
    port = port || {};
    var row = text("div", "spec-row");
    var containerPort = document.createElement("input");
    containerPort.placeholder = "container port";
    containerPort.value = port.container_port || "";
    var hostPort = document.createElement("input");
    hostPort.placeholder = "host port (blank = random)";
    hostPort.value = port.host_port || "";
    var proto = document.createElement("select");
    ["tcp", "udp"].forEach(function (p) {
      var opt = text("option", null, p.toUpperCase());
      opt.value = p;
      if ((port.protocol || "tcp") === p) opt.selected = true;
      proto.appendChild(opt);
    });
    row.appendChild(containerPort);
    row.appendChild(hostPort);
    row.appendChild(proto);
    row.appendChild(specRemoveButton(row));
    el.createContainerPortRows.appendChild(row);
  }

  function addVolumeRow(vol) {
    vol = vol || {};
    var row = text("div", "spec-row");
    var source = document.createElement("input");
    source.placeholder = "volume name or host path";
    source.value = vol.source || "";
    var destination = document.createElement("input");
    destination.placeholder = "/path/in/container";
    destination.value = vol.destination || "";
    var mode = document.createElement("select");
    ["rw", "ro"].forEach(function (m) {
      var opt = text("option", null, m);
      opt.value = m;
      if ((vol.mode || "rw") === m) opt.selected = true;
      mode.appendChild(opt);
    });
    row.appendChild(source);
    row.appendChild(destination);
    row.appendChild(mode);
    row.appendChild(specRemoveButton(row));
    el.createContainerVolumeRows.appendChild(row);
  }

  function rowInputs(container) {
    return Array.prototype.slice.call(container.children);
  }

  function collectEnvRows() {
    return rowInputs(el.createContainerEnvRows)
      .map(function (row) { return row.querySelector("input").value.trim(); })
      .filter(Boolean);
  }

  function collectPortRows() {
    return rowInputs(el.createContainerPortRows).map(function (row) {
      var inputs = row.querySelectorAll("input");
      return {
        container_port: inputs[0].value.trim(),
        host_port: inputs[1].value.trim(),
        protocol: row.querySelector("select").value,
      };
    }).filter(function (p) { return p.container_port; });
  }

  function collectVolumeRows() {
    return rowInputs(el.createContainerVolumeRows).map(function (row) {
      var inputs = row.querySelectorAll("input");
      return {
        source: inputs[0].value.trim(),
        destination: inputs[1].value.trim(),
        mode: row.querySelector("select").value,
      };
    }).filter(function (v) { return v.source && v.destination; });
  }

  function openCreateContainer(prefill, hostName) {
    var hosts = eligibleHosts(state.data, function (h) { return h.container_actions; });
    fillSelect(el.createContainerHost, hosts);
    if (hostName && hosts.some(function (h) { return h.name === hostName; })) {
      el.createContainerHost.value = hostName;
    }

    el.createContainerTitle.textContent = prefill ? "Clone container" : "Create a container";
    el.createContainerName.value = (prefill && prefill.name) ? prefill.name + "-copy" : "";
    el.createContainerImage.value = (prefill && prefill.image) || "";
    var command = prefill && prefill.command;
    el.createContainerCommand.value = Array.isArray(command) ? command.join(" ") : (command || "");

    el.createContainerEnvRows.innerHTML = "";
    ((prefill && prefill.env) || []).forEach(addEnvRow);
    el.createContainerPortRows.innerHTML = "";
    ((prefill && prefill.ports) || []).forEach(addPortRow);
    el.createContainerVolumeRows.innerHTML = "";
    ((prefill && prefill.volumes) || []).forEach(addVolumeRow);

    el.createContainerRestart.value = (prefill && prefill.restart_policy) || "no";
    el.createContainerNetwork.value = (prefill && prefill.network) || "";
    el.createContainerStart.checked = true;
    hide(el.createContainerError);
    el.createContainerSubmit.disabled = false;

    el.createContainerNetworkList.innerHTML = "";
    var selectedHost = el.createContainerHost.value;
    var netEntry = state.networksData && state.networksData.byHost &&
      state.networksData.byHost[selectedHost];
    ((netEntry && netEntry.result && netEntry.result.networks) || []).forEach(function (n) {
      var opt = document.createElement("option");
      opt.value = n.name;
      el.createContainerNetworkList.appendChild(opt);
    });

    if (typeof el.createContainerDialog.showModal === "function") el.createContainerDialog.showModal();
  }

  function openCloneContainer(host, container) {
    fetch("/api/hosts/" + encodeURIComponent(host.name) + "/containers/" +
      encodeURIComponent(container.id) + "/clone-spec")
      .then(function (r) { return r.json(); })
      .then(function (spec) {
        if (spec.error) throw new Error(spec.error);
        spec.name = container.name;
        openCreateContainer(spec, host.name);
      })
      .catch(function (err) {
        window.alert(err.message || "Could not read that container's config.");
      });
  }

  function downloadTextFile(filename, contents, mime) {
    var blob = new Blob([contents], { type: mime || "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function exportContainerConfig(host, container) {
    fetch("/api/hosts/" + encodeURIComponent(host.name) + "/containers/" +
      encodeURIComponent(container.id) + "/clone-spec")
      .then(function (r) { return r.json(); })
      .then(function (spec) {
        if (spec.error) throw new Error(spec.error);
        spec.name = container.name;
        downloadTextFile(container.name + ".config.json", JSON.stringify(spec, null, 2));
      })
      .catch(function (err) {
        window.alert(err.message || "Could not read that container's config.");
      });
  }

  function submitCreateContainer(event) {
    event.preventDefault();
    var host = el.createContainerHost.value;
    var image = el.createContainerImage.value.trim();
    if (!host || !image) return;
    el.createContainerSubmit.disabled = true;
    hide(el.createContainerError);
    postJSON("/api/hosts/" + encodeURIComponent(host) + "/containers/create", {
      name: el.createContainerName.value.trim(),
      image: image,
      command: el.createContainerCommand.value.trim(),
      env: collectEnvRows(),
      ports: collectPortRows(),
      volumes: collectVolumeRows(),
      restart_policy: el.createContainerRestart.value,
      network: el.createContainerNetwork.value.trim(),
      start: el.createContainerStart.checked,
    })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        el.createContainerDialog.close();
        afterWrite();
      })
      .catch(function (err) {
        el.createContainerSubmit.disabled = false;
        showError(el.createContainerError, err.message || "Could not create the container.");
      });
  }

  // -- Deploy stack --

  function loadStackTemplates() {
    return fetch("/api/stack-templates")
      .then(function (r) { return r.json(); })
      .then(function (result) {
        state.stackTemplates = (result && result.templates) || [];
        el.deployStackTemplatePicker.innerHTML = "";
        var blank = text("option", null, "Start from scratch");
        blank.value = "";
        el.deployStackTemplatePicker.appendChild(blank);
        state.stackTemplates.forEach(function (tpl) {
          var opt = text("option", null, tpl.name);
          opt.value = tpl.name;
          el.deployStackTemplatePicker.appendChild(opt);
        });
      })
      .catch(function () {});
  }

  function openDeployStack() {
    var hosts = eligibleHosts(state.data, function (h) { return h.stack_deploy; });
    fillSelect(el.deployStackHost, hosts,
      "No eligible hosts -- see README for CUD_STACKS_DIR");
    el.deployStackProject.value = "";
    el.deployStackCompose.value = "";
    el.deployStackTemplatePicker.value = "";
    el.deployStackLog.hidden = true;
    el.deployStackLog.textContent = "";
    el.deployStackValidation.hidden = true;
    el.deployStackValidation.textContent = "";
    hide(el.deployStackError);
    el.deployStackSubmit.disabled = false;
    loadStackTemplates();
    if (typeof el.deployStackDialog.showModal === "function") el.deployStackDialog.showModal();
  }

  function applyStackTemplate() {
    var name = el.deployStackTemplatePicker.value;
    if (!name) return;
    var tpl = state.stackTemplates.filter(function (t) { return t.name === name; })[0];
    if (tpl) el.deployStackCompose.value = tpl.compose;
  }

  function saveStackTemplate() {
    var compose = el.deployStackCompose.value;
    if (!compose.trim()) {
      showError(el.deployStackError, "Write a compose file before saving it as a template.");
      return;
    }
    var name = window.prompt("Template name:", el.deployStackProject.value.trim());
    if (!name) return;
    postJSON("/api/stack-templates", { name: name.trim(), compose: compose })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        return loadStackTemplates();
      })
      .then(function () { el.deployStackTemplatePicker.value = name.trim(); })
      .catch(function (err) {
        showError(el.deployStackError, err.message || "Could not save that template.");
      });
  }

  function boundHostPorts(hostName) {
    var host = (state.data.hosts || []).filter(function (h) { return h.name === hostName; })[0];
    var ports = {};
    ((host && host.containers) || []).forEach(function (c) {
      (c.ports || []).forEach(function (p) {
        var match = /:(\d+)->/.exec(p);
        if (match) ports[match[1]] = c.name;
      });
    });
    return ports;
  }

  function runValidateStack() {
    var host = el.deployStackHost.value;
    var project = el.deployStackProject.value.trim() || "validate";
    var compose = el.deployStackCompose.value;
    if (!host || !compose.trim()) return;
    el.deployStackValidate.disabled = true;
    el.deployStackValidation.hidden = false;
    el.deployStackValidation.textContent = "Validating…";
    hide(el.deployStackError);
    postJSON("/api/hosts/" + encodeURIComponent(host) + "/stacks/validate",
      { project: project, compose: compose })
      .then(function (result) {
        el.deployStackValidate.disabled = false;
        if (result.error) throw new Error(result.error);
        if (!result.valid) {
          el.deployStackValidation.className = "dialog-note dialog-error-note";
          el.deployStackValidation.textContent = "Not valid:\n" + (result.errors || []).join("\n");
          return;
        }
        var bound = boundHostPorts(host);
        var conflicts = (result.ports || []).filter(function (p) { return bound[p.host_port]; });
        var lines = ["Valid."];
        if (result.warning) lines.push(result.warning);
        if (conflicts.length) {
          lines.push("Port conflicts with running containers:");
          conflicts.forEach(function (p) {
            lines.push("  " + p.host_port + " -> " + p.service + " also used by " + bound[p.host_port]);
          });
        } else if (result.ports && result.ports.length) {
          lines.push("Ports: " + result.ports.map(function (p) {
            return p.host_port + "->" + p.container_port + "/" + p.protocol + " (" + p.service + ")";
          }).join(", "));
        }
        el.deployStackValidation.className = "dialog-note";
        el.deployStackValidation.textContent = lines.join("\n");
      })
      .catch(function (err) {
        el.deployStackValidate.disabled = false;
        el.deployStackValidation.hidden = true;
        showError(el.deployStackError, err.message || "Could not validate.");
      });
  }

  function submitDeployStack(event) {
    event.preventDefault();
    var host = el.deployStackHost.value;
    var project = el.deployStackProject.value.trim();
    var compose = el.deployStackCompose.value;
    if (!host || !project || !compose.trim()) return;
    confirmDialog(
      "Deploy this stack?",
      "Run docker compose up -d for \"" + project + "\" on " + host + "? This can create, " +
      "recreate or remove containers, networks and volumes to match the file.",
      { confirmLabel: "Deploy" }
    ).then(function (ok) {
      if (!ok) return;
      el.deployStackSubmit.disabled = true;
      hide(el.deployStackError);
      postJSON("/api/hosts/" + encodeURIComponent(host) + "/stacks",
        { project: project, compose: compose })
        .then(function (job) {
          if (job.error) throw new Error(job.error);
          el.deployStackLog.hidden = false;
          watchJob(
            "/api/hosts/" + encodeURIComponent(host) + "/stacks/job/" + encodeURIComponent(job.id),
            el.deployStackLog,
            function (finalJob) {
              el.deployStackSubmit.disabled = false;
              if (finalJob.status === "ok") afterWrite();
            }
          );
        })
        .catch(function (err) {
          el.deployStackSubmit.disabled = false;
          showError(el.deployStackError, err.message || "Could not start the deploy.");
        });
    });
  }

  // -- Redeploy / view / edit an existing stack's compose file --

  function stackConfigPath(stack) {
    var raw = (stack.config_files || "").split(",")[0];
    return raw ? raw.trim() : "";
  }

  function runRedeployStack(host, stack) {
    var path = stackConfigPath(stack);
    if (!path) {
      window.alert("This stack has no known compose file path to redeploy from.");
      return;
    }
    confirmDialog(
      "Redeploy this stack?",
      "Pull the latest images and run docker compose up -d for \"" + stack.project +
      "\" on " + (host.label || host.name) + "?",
      { confirmLabel: "Redeploy" }
    ).then(function (ok) {
      if (!ok) return;
      postJSON("/api/hosts/" + encodeURIComponent(host.name) + "/stacks/redeploy",
        { project: stack.project, path: path })
        .then(function (job) {
          if (job.error) throw new Error(job.error);
          watchJob(
            "/api/hosts/" + encodeURIComponent(host.name) + "/stacks/job/" +
              encodeURIComponent(job.id),
            null,
            function (finalJob) { if (finalJob.status === "ok") afterWrite(); }
          );
        })
        .catch(function (err) {
          window.alert(err.message || "Could not start the redeploy.");
        });
    });
  }

  function openStackFile(host, stack) {
    var path = stackConfigPath(stack);
    if (!path) {
      window.alert("This stack has no known compose file path.");
      return;
    }
    state.stackFileContext = { host: host, stack: stack, path: path };
    el.stackFilePath.textContent = path + " on " + (host.label || host.name);
    el.stackFileContent.value = "Loading…";
    el.stackFileLog.hidden = true;
    el.stackFileLog.textContent = "";
    hide(el.stackFileError);
    if (typeof el.stackFileDialog.showModal === "function") el.stackFileDialog.showModal();
    fetch("/api/hosts/" + encodeURIComponent(host.name) + "/stacks/file?path=" +
      encodeURIComponent(path))
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        el.stackFileContent.value = result.content || "";
      })
      .catch(function (err) {
        el.stackFileContent.value = "";
        showError(el.stackFileError, err.message || "Could not read that file.");
      });
  }

  function saveStackFile() {
    var ctx = state.stackFileContext;
    if (!ctx) return;
    hide(el.stackFileError);
    postJSON("/api/hosts/" + encodeURIComponent(ctx.host.name) + "/stacks/file",
      { path: ctx.path, content: el.stackFileContent.value })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        el.stackFileDialog.close();
      })
      .catch(function (err) {
        showError(el.stackFileError, err.message || "Could not save that file.");
      });
  }

  function redeployFromFileDialog() {
    var ctx = state.stackFileContext;
    if (!ctx) return;
    confirmDialog(
      "Redeploy this stack?",
      "Pull the latest images and run docker compose up -d for \"" + ctx.stack.project +
      "\" on " + (ctx.host.label || ctx.host.name) + "?",
      { confirmLabel: "Redeploy" }
    ).then(function (ok) {
      if (!ok) return;
      hide(el.stackFileError);
      postJSON("/api/hosts/" + encodeURIComponent(ctx.host.name) + "/stacks/redeploy",
        { project: ctx.stack.project, path: ctx.path })
        .then(function (job) {
          if (job.error) throw new Error(job.error);
          el.stackFileLog.hidden = false;
          watchJob(
            "/api/hosts/" + encodeURIComponent(ctx.host.name) + "/stacks/job/" +
              encodeURIComponent(job.id),
            el.stackFileLog,
            function (finalJob) { if (finalJob.status === "ok") afterWrite(); }
          );
        })
        .catch(function (err) {
          showError(el.stackFileError, err.message || "Could not start the redeploy.");
        });
    });
  }

  // -- per-tab action button --

  function updateTabAction() {
    el.tabAction.hidden = true;
    el.tabAction2.hidden = true;
    el.tabAction.onclick = null;
    el.tabAction2.onclick = null;
    if (state.tab === "images") {
      el.tabAction.hidden = false;
      el.tabAction.textContent = "Pull image";
      el.tabAction.onclick = openPullImage;
      el.tabAction2.hidden = false;
      el.tabAction2.textContent = "Build image";
      el.tabAction2.onclick = openBuildImage;
    } else if (state.tab === "volumes") {
      el.tabAction.hidden = false;
      el.tabAction.textContent = "Create volume";
      el.tabAction.onclick = openCreateVolume;
    } else if (state.tab === "networks") {
      el.tabAction.hidden = false;
      el.tabAction.textContent = "Create network";
      el.tabAction.onclick = openCreateNetwork;
    } else if (state.tab === "containers") {
      el.tabAction.hidden = false;
      el.tabAction.textContent = "Create container";
      el.tabAction.onclick = function () { openCreateContainer(null, state.host || null); };
    } else if (state.tab === "compose") {
      el.tabAction.hidden = false;
      el.tabAction.textContent = "Deploy stack";
      el.tabAction.onclick = openDeployStack;
    }
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
      busy.appendChild(text("span", null, job.kind === "refresh"
        ? "Refreshing package lists…"
        : "Installing " + job.packages.length + " " +
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

    var refreshBtn = text("button", "button small");
    refreshBtn.type = "button";
    refreshBtn.textContent = "Refresh package lists";
    var listsUpdated = (host.os || {}).lists_updated;
    refreshBtn.title = listsUpdated
      ? "Package lists last refreshed " + relativeTime(listsUpdated) + " ago"
      : "";
    refreshBtn.addEventListener("click", function () { runOsRefresh(host); });
    bar.appendChild(refreshBtn);

    if (job && job.status && job.status !== "running") {
      var doneText = job.kind === "refresh"
        ? (job.status === "ok" ? "Package lists refreshed" : "Refresh failed")
        : (job.status === "ok"
            ? "Installed " + job.packages.length + " " + plural(job.packages.length, "package", "packages")
            : "Update failed");
      var done = text("span", job.status === "ok" ? "os-done" : "os-failed", doneText);
      bar.appendChild(done);
      bar.appendChild(logBox(job));
    }
    return bar;
  }

  function runOsRefresh(host) {
    state.osJobs[host.name] = { status: "running", kind: "refresh", packages: [], lines: [] };
    render();

    postJSON("/api/hosts/" + encodeURIComponent(host.name) + "/os/refresh", {})
      .then(function (job) {
        if (job.error) throw new Error(job.error);
        state.osJobs[host.name] = job;
        render();
        watchOsJob(host, job.id);
      })
      .catch(function (err) {
        state.osJobs[host.name] = {
          status: "failed", kind: "refresh", packages: [],
          lines: [err.message || "The refresh could not be started."]
        };
        render();
      });
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
    var cloneBtn = text("button", "button small");
    cloneBtn.type = "button";
    cloneBtn.textContent = "Clone";
    cloneBtn.addEventListener("click", function () { openCloneContainer(host, container); });
    manage.appendChild(cloneBtn);

    var exportBtn = text("button", "button small");
    exportBtn.type = "button";
    exportBtn.textContent = "Export config";
    exportBtn.addEventListener("click", function () { exportContainerConfig(host, container); });
    manage.appendChild(exportBtn);
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

  function configToggleButton(host, container) {
    var key = containerKey(container);
    var btn = text("button", "button small");
    btn.type = "button";
    btn.textContent = state.configOpen[key] ? "Hide config" : "Config";
    btn.addEventListener("click", function () {
      var opening = !state.configOpen[key];
      state.configOpen[key] = opening;
      state.open[key] = true;
      if (opening && !state.config[key]) {
        loadContainerConfig(host, container);
        loadContainerStats(host, container);
      }
      render();
    });
    return btn;
  }

  function loadContainerConfig(host, container) {
    var key = containerKey(container);
    state.config[key] = { loading: true };
    render();

    fetch(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) + "/clone-spec"
    )
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.config[key] = result;
        render();
      })
      .catch(function (err) {
        state.config[key] = { error: err.message || "Could not load container config." };
        render();
      });
  }

  function loadContainerStats(host, container) {
    var key = containerKey(container);
    var previous = state.stats[key];
    state.stats[key] = { loading: true, last: previous && !previous.error ? previous : null };
    render();

    fetch(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) + "/stats"
    )
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.stats[key] = result;
        render();
      })
      .catch(function (err) {
        state.stats[key] = { error: err.message || "Could not load usage stats." };
        render();
      });
  }

  function runContainerLimits(host, container, memoryMb, cpuLimit) {
    var key = containerKey(container);
    state.limitsJobs[key] = { status: "running" };
    render();

    postJSON(
      "/api/hosts/" + encodeURIComponent(host.name) +
      "/containers/" + encodeURIComponent(container.id) + "/limits",
      { memory_mb: memoryMb, cpu_limit: cpuLimit }
    )
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.limitsJobs[key] = { status: "ok" };
        loadContainerConfig(host, container);
        render();
        fetch("/api/refresh", { method: "POST" }).then(function () {
          setTimeout(load, 1200);
        });
      })
      .catch(function (err) {
        state.limitsJobs[key] = {
          status: "failed", message: err.message || "Updating limits failed.",
        };
        render();
      });
  }

  function limitsForm(host, container, config) {
    var key = containerKey(container);
    var wrap = text("div", "limits-form");

    var memoryField = text("label", "limits-field");
    memoryField.appendChild(text("span", null, "Memory (MB, 0 = unlimited)"));
    var memoryInput = document.createElement("input");
    memoryInput.type = "number";
    memoryInput.min = "0";
    memoryInput.value = config.memory_mb || 0;
    memoryField.appendChild(memoryInput);
    wrap.appendChild(memoryField);

    var cpuField = text("label", "limits-field");
    cpuField.appendChild(text("span", null, "CPU limit (cores, 0 = unlimited)"));
    var cpuInput = document.createElement("input");
    cpuInput.type = "number";
    cpuInput.min = "0";
    cpuInput.step = "0.1";
    cpuInput.value = config.cpu_limit || 0;
    cpuField.appendChild(cpuInput);
    wrap.appendChild(cpuField);

    var job = state.limitsJobs[key];
    var save = text("button", "button small primary");
    save.type = "button";
    save.textContent = job && job.status === "running" ? "Saving…" : "Save limits";
    save.disabled = !!(job && job.status === "running");
    save.addEventListener("click", function () {
      var memoryMb = Number(memoryInput.value) || 0;
      var cpuLimit = Number(cpuInput.value) || 0;
      confirmDialog(
        "Update resource limits?",
        "Change " + container.name + "'s limits to " +
        (memoryMb ? memoryMb + " MB memory" : "unlimited memory") + " and " +
        (cpuLimit ? cpuLimit + " CPU" : "unlimited CPU") + "? Takes effect immediately.",
        { confirmLabel: "Update" }
      ).then(function (ok) {
        if (ok) runContainerLimits(host, container, memoryMb, cpuLimit);
      });
    });
    wrap.appendChild(save);

    if (job && job.status === "failed") {
      wrap.appendChild(text("span", "os-readonly", job.message));
    }

    return wrap;
  }

  function configSection(host, container) {
    var key = containerKey(container);
    var wrap = text("div", "config-section");
    var entry = state.config[key];

    if (!entry || entry.loading) {
      wrap.appendChild(text("span", "os-running", "Loading config…"));
      return wrap;
    }
    if (entry.error) {
      wrap.appendChild(text("span", "os-readonly", entry.error));
      return wrap;
    }

    var envBlock = text("div", "config-block");
    envBlock.appendChild(text("h4", null, "Environment"));
    if ((entry.env || []).length) {
      var envList = text("pre", "joblog", entry.env.join("\n"));
      envBlock.appendChild(envList);
    } else {
      envBlock.appendChild(text("span", "os-readonly", "No environment variables set."));
    }
    wrap.appendChild(envBlock);

    var mountsBlock = text("div", "config-block");
    mountsBlock.appendChild(text("h4", null, "Mounts"));
    var mounts = container.mounts || [];
    if (mounts.length) {
      var mountList = text("ul", "config-list");
      mounts.forEach(function (m) {
        mountList.appendChild(text("li", null,
          (m.name || m.source || "?") + " → " + m.destination +
          (m.rw === false ? " (ro)" : " (rw)") + " · " + (m.type || "bind")));
      });
      mountsBlock.appendChild(mountList);
    } else {
      mountsBlock.appendChild(text("span", "os-readonly", "No mounts."));
    }
    wrap.appendChild(mountsBlock);

    var netBlock = text("div", "config-block");
    netBlock.appendChild(text("h4", null, "Networks"));
    netBlock.appendChild(text("p", null, (container.networks || []).join(", ") || "—"));
    wrap.appendChild(netBlock);

    var limitsBlock = text("div", "config-block");
    limitsBlock.appendChild(text("h4", null, "Resource limits"));
    var statsEntry = state.stats[key];
    var current = statsEntry && !statsEntry.loading && !statsEntry.error ? statsEntry : statsEntry && statsEntry.last;
    if (current) {
      limitsBlock.appendChild(text("p", null,
        "CPU " + current.cpu_percent + "%  ·  Memory " + bytes(current.memory_used) +
        (current.memory_limit ? " / " + bytes(current.memory_limit) : "") +
        (current.memory_percent ? " (" + current.memory_percent + "%)" : "")));
    } else if (statsEntry && statsEntry.error) {
      limitsBlock.appendChild(text("span", "os-readonly", statsEntry.error));
    } else {
      limitsBlock.appendChild(text("span", "os-running", "Loading usage…"));
    }
    var refreshStats = text("button", "button small");
    refreshStats.type = "button";
    refreshStats.textContent = "Refresh usage";
    refreshStats.addEventListener("click", function () { loadContainerStats(host, container); });
    limitsBlock.appendChild(refreshStats);

    var capability = host.container_actions || {};
    if (capability.can_manage) {
      limitsBlock.appendChild(limitsForm(host, container, entry));
    }
    wrap.appendChild(limitsBlock);

    return wrap;
  }

  // A small, dependency-free markdown renderer -- just enough for what a
  // model actually sends back: paragraphs, headings, lists, fenced code,
  // inline code/bold/italic/links. Not a full spec implementation.

  function renderInlineMarkdown(parent, raw) {
    var pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*|_([^_]+)_|\[([^\]]+)\]\(([^)\s]+)\)/g;
    var lastIndex = 0;
    var match;
    while ((match = pattern.exec(raw)) !== null) {
      if (match.index > lastIndex) {
        parent.appendChild(document.createTextNode(raw.slice(lastIndex, match.index)));
      }
      if (match[1] !== undefined) {
        parent.appendChild(text("code", "md-inline-code", match[1]));
      } else if (match[2] !== undefined) {
        var strong = document.createElement("strong");
        strong.textContent = match[2];
        parent.appendChild(strong);
      } else if (match[3] !== undefined || match[4] !== undefined) {
        var em = document.createElement("em");
        em.textContent = match[3] !== undefined ? match[3] : match[4];
        parent.appendChild(em);
      } else if (match[5] !== undefined) {
        var a = document.createElement("a");
        a.href = match[6];
        a.textContent = match[5];
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        parent.appendChild(a);
      }
      lastIndex = pattern.lastIndex;
    }
    if (lastIndex < raw.length) {
      parent.appendChild(document.createTextNode(raw.slice(lastIndex)));
    }
  }

  function renderMarkdown(parent, raw) {
    var lines = (raw || "").replace(/\r\n/g, "\n").split("\n");
    var i = 0;
    var listEl = null;

    while (i < lines.length) {
      var line = lines[i];

      var fence = line.match(/^```(\w*)\s*$/);
      if (fence) {
        listEl = null;
        var codeLines = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          codeLines.push(lines[i]);
          i++;
        }
        i++;
        var pre = document.createElement("pre");
        pre.className = "md-code";
        pre.appendChild(text("code", null, codeLines.join("\n")));
        parent.appendChild(pre);
        continue;
      }

      if (!line.trim()) { listEl = null; i++; continue; }

      var heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        listEl = null;
        var h = document.createElement("h" + Math.min(6, heading[1].length + 2));
        h.className = "md-heading";
        renderInlineMarkdown(h, heading[2]);
        parent.appendChild(h);
        i++;
        continue;
      }

      var ordered = line.match(/^\s*\d+\.\s+(.*)$/);
      var unordered = !ordered && line.match(/^\s*[-*]\s+(.*)$/);
      if (ordered || unordered) {
        var tag = ordered ? "ol" : "ul";
        if (!listEl || listEl.tagName.toLowerCase() !== tag) {
          listEl = document.createElement(tag);
          listEl.className = "md-list";
          parent.appendChild(listEl);
        }
        var li = document.createElement("li");
        renderInlineMarkdown(li, (ordered || unordered)[1]);
        listEl.appendChild(li);
        i++;
        continue;
      }

      listEl = null;
      var paraLines = [];
      while (i < lines.length && lines[i].trim() &&
             !/^```/.test(lines[i]) && !/^#{1,4}\s/.test(lines[i]) &&
             !/^\s*\d+\.\s+/.test(lines[i]) && !/^\s*[-*]\s+/.test(lines[i])) {
        paraLines.push(lines[i]);
        i++;
      }
      var p = document.createElement("p");
      renderInlineMarkdown(p, paraLines.join(" "));
      parent.appendChild(p);
    }
  }

  function formatCost(usage) {
    if (!usage) return "";
    var bits = [];
    if (typeof usage.cost === "number") {
      bits.push("$" + usage.cost.toFixed(usage.cost < 0.01 ? 5 : 4));
    }
    var tokens = usage.total_tokens ||
      ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0));
    if (tokens) bits.push(tokens + " tokens");
    return bits.join(" · ");
  }

  // ---- the dashboard-wide assistant --------------------------------------
  // One entry point for the whole site, not a button per container: it can
  // look at any host/container/OS-update the dashboard tracks, and -- after
  // going through the same confirm dialog the action buttons use -- start,
  // stop, restart, pause, unpause, rename, remove or recreate a container,
  // or install OS updates.

  var TOOL_STATUS_LABELS = {
    list_hosts: "Checking hosts…",
    list_containers: "Looking at containers…",
    get_logs: "Reading logs…",
    get_logs_history: "Reading log history…",
    list_os_updates: "Checking OS updates…"
  };

  function assistantAvailable() {
    return !!(state.settings && state.settings.ai_assistant_available);
  }

  function updateAssistantVisibility() {
    if (el.aiFab) el.aiFab.hidden = !assistantAvailable();
  }

  function openAssistant() {
    state.assistant.open = true;
    renderAssistantPanel();
    if (typeof el.aiAssistantDialog.show === "function") {
      el.aiAssistantDialog.show();
    } else if (typeof el.aiAssistantDialog.showModal === "function") {
      el.aiAssistantDialog.showModal();
    }
    el.aiAssistantInput.focus();
  }

  function closeAssistant() {
    state.assistant.open = false;
    if (el.aiAssistantDialog.open) el.aiAssistantDialog.close();
  }

  function renderAssistantPanel() {
    var a = state.assistant;
    if (!el.aiAssistantMessages) return;
    el.aiAssistantMessages.innerHTML = "";

    if (!a.display.length) {
      el.aiAssistantMessages.appendChild(text("p", "ai-chat-hint",
        "Ask about any host or container -- I can look at containers, logs and OS updates, " +
        "and, with your approval, start, stop, restart, rename, remove or recreate a " +
        "container, or install OS updates."));
    }

    a.display.forEach(function (m) {
      if (m.role === "status") {
        el.aiAssistantMessages.appendChild(text("p", "ai-chat-status", m.text));
        return;
      }
      var bubble = text("div", "ai-chat-msg ai-chat-" + m.role);
      bubble.appendChild(text("span", "ai-chat-role", m.role === "user" ? "You" : "AI"));
      if (m.role === "assistant") {
        var body = document.createElement("div");
        body.className = "md-body";
        renderMarkdown(body, m.text || "");
        bubble.appendChild(body);
      } else {
        bubble.appendChild(text("p", null, m.text));
      }
      var cost = m.role === "assistant" ? formatCost(m.usage) : "";
      if (cost) bubble.appendChild(text("span", "ai-chat-usage", cost));
      el.aiAssistantMessages.appendChild(bubble);
    });

    if (a.loading) {
      var thinking = text("span", "os-running");
      thinking.appendChild(text("span", "spinner"));
      thinking.appendChild(text("span", null, "Working…"));
      el.aiAssistantMessages.appendChild(thinking);
    }
    if (a.error) el.aiAssistantMessages.appendChild(text("p", "ai-chat-error", a.error));

    el.aiAssistantMessages.scrollTop = el.aiAssistantMessages.scrollHeight;
    el.aiAssistantSend.disabled = !!a.loading;
  }

  function assistantSend(messageText) {
    var a = state.assistant;
    a.display.push({ role: "user", text: messageText });
    a.wire.push({ role: "user", content: messageText });
    a.loading = true;
    a.error = null;
    renderAssistantPanel();

    postJSON("/api/ai/chat", { messages: a.wire })
      .then(handleAssistantResult)
      .catch(function (err) {
        a.loading = false;
        a.error = err.message || "Could not reach the assistant.";
        renderAssistantPanel();
      });
  }

  function handleAssistantResult(result) {
    var a = state.assistant;
    if (result.error) {
      a.loading = false;
      a.error = result.error;
      renderAssistantPanel();
      return;
    }
    if (result.messages) a.wire = result.messages;

    if (result.status === "final") {
      a.loading = false;
      a.display.push({ role: "assistant", text: result.reply || "", usage: result.usage });
      renderAssistantPanel();
      return;
    }

    if (result.status === "needs_confirmation") {
      var pending = result.pending || {};
      var info = pending.confirm || {};
      a.display.push({ role: "status",
        text: TOOL_STATUS_LABELS[pending.name] || ("Proposing: " + pending.name) });
      renderAssistantPanel();

      confirmDialog(
        info.title || "Confirm this action?",
        info.message || "",
        { confirmLabel: info.confirm_label || "Confirm", danger: !!info.danger }
      ).then(function (approved) {
        a.display.push({
          role: "status",
          text: (approved ? "Approved: " : "Declined: ") + (info.message || pending.name)
        });
        renderAssistantPanel();

        postJSON("/api/ai/chat", {
          messages: a.wire, confirm: { approved: approved }, pending: pending
        })
          .then(function (nextResult) {
            if (approved) {
              // Something may have just changed -- catch the dashboard up
              // the same way the row action buttons do.
              fetch("/api/refresh", { method: "POST" }).then(function () {
                setTimeout(load, 1200);
              });
            }
            handleAssistantResult(nextResult);
          })
          .catch(function (err) {
            a.loading = false;
            a.error = err.message || "Could not reach the assistant.";
            renderAssistantPanel();
          });
      });
      return;
    }

    a.loading = false;
    a.error = "Unexpected response from the assistant.";
    renderAssistantPanel();
  }

  // ---- Docker events -------------------------------------------------------

  var EVENT_STATUS = {
    start: "up-to-date", create: "up-to-date", unpause: "up-to-date",
    connect: "up-to-date", mount: "up-to-date",
    die: "error", kill: "error", stop: "error", oom: "error", destroy: "error",
    remove: "error", disconnect: "error", unmount: "error",
    pause: "unknown", rename: "unknown",
    pull: "update-available", health_status: "update-available"
  };
  var EVENT_TYPE_LABELS = { container: "Container", image: "Image", network: "Network", volume: "Volume" };

  function eventStatus(event) {
    return EVENT_STATUS[event.action] || "unknown";
  }

  function eventLabel(event) {
    var who = event.name || event.actor_id || "";
    var bits = [event.action || event.type];
    if (who) bits.push(who);
    if (event.exit_code !== null && event.exit_code !== undefined && event.exit_code !== "") {
      bits.push("exit " + event.exit_code);
    }
    return bits.join(" · ");
  }

  function eventMatches(event) {
    if (!state.eventsQuery) return true;
    var haystack = [event.name, event.actor_id, event.action, event.type, event.image]
      .filter(Boolean).join(" ").toLowerCase();
    return haystack.indexOf(state.eventsQuery) !== -1;
  }

  var EVENTS_POLL_SECONDS = 10;
  var eventsPollTimer = null;

  function startEventsPolling() {
    stopEventsPolling();
    eventsPollTimer = setInterval(function () { loadAllEvents(true); }, EVENTS_POLL_SECONDS * 1000);
  }

  function stopEventsPolling() {
    if (eventsPollTimer) { clearInterval(eventsPollTimer); eventsPollTimer = null; }
  }

  function loadAllEvents(silent) {
    if (!silent) {
      state.eventsData = { loading: true };
      render();
    }
    var params = ["limit=300"];
    if (state.host) params.push("host=" + encodeURIComponent(state.host));
    fetch("/api/events?" + params.join("&"))
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.eventsData = { items: result.events || [] };
        render();
      })
      .catch(function (err) {
        state.eventsData = { error: err.message || "Could not load events." };
        render();
      });
  }

  function renderEventsFilters(data) {
    el.statusFilters.textContent = "";
    fillHostFilter(data.hosts || []);
  }

  function renderEventsTab(data) {
    el.hosts.textContent = "";
    var entry = state.eventsData;

    if (!entry || entry.loading) {
      el.hosts.appendChild(text("p", "os-running", "Loading events…"));
      el.empty.hidden = true;
      return;
    }
    if (entry.error) {
      el.empty.hidden = false;
      el.empty.textContent = entry.error;
      return;
    }

    var items = (entry.items || []).filter(eventMatches);
    if (!items.length) {
      el.empty.hidden = false;
      el.empty.textContent = (entry.items || []).length
        ? "Nothing matches the current filter."
        : "No events recorded yet -- either nothing has happened, or event history isn't enabled on these hosts.";
      return;
    }
    el.empty.hidden = true;

    var section = text("section", "host");
    var list = text("ul", "timeline");
    items.forEach(function (event) {
      var li = text("li", "timeline-item");
      li.appendChild(dot(eventStatus(event)));
      var body = text("div", "timeline-body");
      var head = text("div", "timeline-head");
      head.appendChild(text("span", "timeline-label", eventLabel(event)));
      head.appendChild(text("span", "tag", EVENT_TYPE_LABELS[event.type] || event.type));
      body.appendChild(head);
      var meta = text("div", "timeline-meta");
      meta.appendChild(text("span", null, event.host_label || event.host || ""));
      meta.appendChild(text("span", "sep", "·"));
      meta.appendChild(text("span", null, relativeTime(event.ts) + " ago"));
      body.appendChild(meta);
      li.appendChild(body);
      list.appendChild(li);
    });
    section.appendChild(list);
    el.hosts.appendChild(section);
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
    actionsCell.appendChild(configToggleButton(host, container));
    row.appendChild(actionsCell);

    var detail = text("tr", "detail");
    var cell = document.createElement("td");
    cell.colSpan = 6;
    var body = text("div", "detail-body");
    body.appendChild(text("p", "detail-note", container.detail || ""));
    body.appendChild(containerDetailsPanel(container));
    if (state.logsOpen[key]) body.appendChild(logsSection(host, container));
    if (state.configOpen[key]) body.appendChild(configSection(host, container));

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
    var headRight = text("div", "host-head-right");
    headRight.appendChild(meta);
    head.appendChild(headRight);
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
    } else if (state.tab === "compose") {
      renderComposeFilters(data);
      renderComposeHosts(data);
    } else if (state.tab === "events") {
      renderEventsFilters(data);
      renderEventsTab(data);
    } else if (state.tab === "images") {
      renderImagesFilters(data);
      renderImagesTab(data);
    } else if (state.tab === "volumes") {
      renderVolumesFilters(data);
      renderVolumesTab(data);
    } else if (state.tab === "networks") {
      renderNetworksFilters(data);
      renderNetworksTab(data);
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
    if (state.tab === "events") loadAllEvents(true);
    else if (state.tab === "images") loadImages(true);
    else if (state.tab === "volumes") loadVolumes(true);
    else if (state.tab === "networks") loadNetworks(true);
    fetch("/api/refresh", { method: "POST" })
      .then(function () { schedulePoll(600); })
      .catch(function () { schedulePoll(2000); });
  }

  // ---- port map -----------------------------------------------------------

  var PORT_MAPPING = /^(.+):(\d+)->(\d+)\/(\w+)$/;

  function collectPortMappings() {
    var rows = [];
    ((state.data && state.data.hosts) || []).forEach(function (host) {
      (host.containers || []).forEach(function (container) {
        (container.ports || []).forEach(function (portText) {
          var match = PORT_MAPPING.exec(portText);
          if (!match) return;
          rows.push({
            host: host.label || host.name,
            hostKey: host.name,
            bindIp: match[1],
            hostPort: match[2],
            containerPort: match[3],
            protocol: match[4],
            container: container.name,
          });
        });
      });
    });

    // Group by container too -- a container publishing on both 0.0.0.0 and ::
    // is one binding, not a conflict with itself.
    var containersByBinding = {};
    rows.forEach(function (row) {
      var conflictKey = row.hostKey + "/" + row.hostPort + "/" + row.protocol;
      var set = containersByBinding[conflictKey] || (containersByBinding[conflictKey] = {});
      set[row.container] = true;
    });
    rows.forEach(function (row) {
      var conflictKey = row.hostKey + "/" + row.hostPort + "/" + row.protocol;
      row.conflict = Object.keys(containersByBinding[conflictKey]).length > 1;
    });

    rows.sort(function (a, b) {
      return a.host.localeCompare(b.host) || Number(a.hostPort) - Number(b.hostPort);
    });
    return rows;
  }

  function renderPortMap() {
    el.portMapBody.textContent = "";
    var rows = collectPortMappings();
    if (!rows.length) {
      el.portMapBody.appendChild(text("p", "os-readonly", "No published ports found."));
      return;
    }

    var conflicts = rows.filter(function (r) { return r.conflict; });
    if (conflicts.length) {
      el.portMapBody.appendChild(text("p", "os-readonly",
        conflicts.length + " conflicting port " +
        plural(conflicts.length, "binding", "bindings") + " found."));
    }

    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["Host", "Host port", "Container port", "Protocol", "Container"].forEach(function (label) {
      headRow.appendChild(text("th", null, label));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    rows.forEach(function (row) {
      var tr = text("tr", row.conflict ? "port-conflict" : null);
      tr.appendChild(text("td", null, row.host));
      tr.appendChild(text("td", null, row.hostPort));
      tr.appendChild(text("td", null, row.containerPort));
      tr.appendChild(text("td", null, row.protocol));
      tr.appendChild(text("td", null, row.container));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    el.portMapBody.appendChild(table);
  }

  function openPortMap() {
    renderPortMap();
    if (typeof el.portMapDialog.showModal === "function") el.portMapDialog.showModal();
  }

  // ---- cleanup / prune -----------------------------------------------------

  function openCleanup() {
    var hosts = eligibleHosts(state.data, function (h) { return h.container_actions; });
    fillSelect(el.cleanupHost, hosts);
    el.cleanupLog.hidden = true;
    el.cleanupLog.textContent = "";
    hide(el.cleanupError);
    setCleanupBusy(false);
    if (typeof el.cleanupDialog.showModal === "function") el.cleanupDialog.showModal();
  }

  function setCleanupBusy(busy) {
    [el.cleanupContainers, el.cleanupImages, el.cleanupVolumes, el.cleanupNetworks]
      .forEach(function (btn) { btn.disabled = busy; });
  }

  var CLEANUP_KINDS = {
    containers: { label: "stopped containers", url: "prune/containers" },
    images: { label: "dangling images", url: "prune/images" },
    volumes: { label: "unused volumes -- including their data", url: "prune/volumes" },
    networks: { label: "unused networks", url: "prune/networks" },
  };

  function runCleanup(kind) {
    var host = el.cleanupHost.value;
    if (!host) {
      showError(el.cleanupError, "Pick a host first.");
      return;
    }
    var info = CLEANUP_KINDS[kind];
    confirmDialog(
      "Prune " + info.label + "?",
      "This permanently removes " + info.label + " on " + host + ". There is no undo.",
      { confirmLabel: "Prune", danger: true }
    ).then(function (ok) {
      if (!ok) return;
      hide(el.cleanupError);
      setCleanupBusy(true);
      postJSON(
        "/api/hosts/" + encodeURIComponent(host) + "/" + info.url,
        kind === "images" ? { dangling_only: true } : {}
      )
        .then(function (result) {
          setCleanupBusy(false);
          if (result.error) throw new Error(result.error);
          el.cleanupLog.hidden = false;
          var removed = result.removed || [];
          var line = "Pruned " + info.label + ": " +
            (removed.length ? removed.length + " removed" : "nothing to remove") +
            (result.space_reclaimed ? ", " + bytes(result.space_reclaimed) + " reclaimed" : "");
          el.cleanupLog.textContent = (el.cleanupLog.textContent ? el.cleanupLog.textContent + "\n" : "") + line;
          fetch("/api/refresh", { method: "POST" });
        })
        .catch(function (err) {
          setCleanupBusy(false);
          showError(el.cleanupError, err.message || "Prune failed.");
        });
    });
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
        updateAssistantVisibility();
        return settings;
      })
      .catch(function () { return null; });
  }

  function loadRegistries() {
    return fetch("/api/registries")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (result) {
        state.registries = (result && result.registries) || [];
        renderRegistryList();
      })
      .catch(function () {});
  }

  function renderRegistryList() {
    el.settingsRegistryList.textContent = "";
    var registries = state.registries || [];
    if (!registries.length) {
      el.settingsRegistryList.appendChild(text("p", "os-readonly", "No registries configured."));
      return;
    }
    registries.forEach(function (reg) {
      var row = text("div", "registry-row");
      var label = reg.host + (reg.username ? " (" + reg.username + ")" : "") +
        (reg.insecure ? " · insecure" : "");
      row.appendChild(text("span", null, label));
      var remove = text("button", "button small");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", function () {
        fetch("/api/registries/" + encodeURIComponent(reg.host), { method: "DELETE" })
          .then(function (r) { return r.json(); })
          .then(function () { loadRegistries(); });
      });
      row.appendChild(remove);
      el.settingsRegistryList.appendChild(row);
    });
  }

  function addRegistry() {
    var host = el.settingsRegistryHost.value.trim();
    if (!host) {
      showError(el.settingsRegistryError, "A registry host is required.");
      return;
    }
    hide(el.settingsRegistryError);
    postJSON("/api/registries", {
      host: host,
      username: el.settingsRegistryUsername.value.trim(),
      password: el.settingsRegistryPassword.value,
      insecure: el.settingsRegistryInsecure.checked,
    })
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        el.settingsRegistryHost.value = "";
        el.settingsRegistryUsername.value = "";
        el.settingsRegistryPassword.value = "";
        el.settingsRegistryInsecure.checked = false;
        loadRegistries();
      })
      .catch(function (err) {
        showError(el.settingsRegistryError, err.message || "Could not save registry.");
      });
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
    el.settingsAiKey.value = "";
    el.settingsAiKey.placeholder = s.openrouter_api_key_set
      ? "A key is set -- leave blank to keep it"
      : "sk-or-...";
    el.settingsAiKeyHint.textContent = s.openrouter_api_key_set
      ? "A key is already configured. Enter a new one to replace it, or leave this blank."
      : "Required for the Ask AI button to appear. Get one at openrouter.ai/keys.";
    el.settingsAiModel.value = s.openrouter_model || "";
    hide(el.settingsError);
    el.settingsRegistryHost.value = "";
    el.settingsRegistryUsername.value = "";
    el.settingsRegistryPassword.value = "";
    el.settingsRegistryInsecure.checked = false;
    hide(el.settingsRegistryError);
    loadRegistries();
    loadAiModels();
    if (typeof el.settingsDialog.showModal === "function") el.settingsDialog.showModal();
  }

  var aiModelsLoaded = false;
  function loadAiModels() {
    if (aiModelsLoaded) return;
    aiModelsLoaded = true;
    fetch("/api/ai/models")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (result) {
        if (!result || !result.models) return;
        el.settingsAiModelList.innerHTML = "";
        result.models.forEach(function (m) {
          var opt = document.createElement("option");
          opt.value = m.id;
          opt.label = m.name || m.id;
          el.settingsAiModelList.appendChild(opt);
        });
      })
      .catch(function () { aiModelsLoaded = false; });
  }

  function submitSettings(event) {
    event.preventDefault();
    var body = {
      skip_confirmations: el.settingsSkipConfirmations.checked,
      include_stopped: el.settingsIncludeStopped.checked,
      refresh_interval_minutes: Number(el.settingsRefreshInterval.value) || 30,
      log_tail_lines: Number(el.settingsLogTail.value) || 300,
      log_auto_refresh: el.settingsLogAutoRefresh.checked,
      log_refresh_seconds: Number(el.settingsLogRefresh.value) || 5,
      openrouter_api_key: el.settingsAiKey.value,
      openrouter_model: el.settingsAiModel.value.trim()
    };
    postJSON("/api/settings", body)
      .then(function (result) {
        if (result.error) throw new Error(result.error);
        state.settings = result;
        updateAssistantVisibility();
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
      portMapOpen: $("port-map-open"),
      portMapDialog: $("port-map-dialog"),
      portMapBody: $("port-map-body"),
      cleanupOpen: $("cleanup-open"),
      cleanupDialog: $("cleanup-dialog"),
      cleanupHost: $("cleanup-host"),
      cleanupActions: $("cleanup-actions"),
      cleanupContainers: $("cleanup-containers"),
      cleanupImages: $("cleanup-images"),
      cleanupVolumes: $("cleanup-volumes"),
      cleanupNetworks: $("cleanup-networks"),
      cleanupLog: $("cleanup-log"),
      cleanupError: $("cleanup-error"),
      tabContainers: $("tab-containers"),
      tabOs: $("tab-os"),
      tabCompose: $("tab-compose"),
      tabEvents: $("tab-events"),
      tabImages: $("tab-images"),
      tabVolumes: $("tab-volumes"),
      tabNetworks: $("tab-networks"),
      tabContainersCount: $("tab-containers-count"),
      tabOsCount: $("tab-os-count"),
      tabComposeCount: $("tab-compose-count"),
      tabAction: $("tab-action"),
      tabAction2: $("tab-action-2"),

      pullImageDialog: $("pull-image-dialog"),
      pullImageForm: $("pull-image-form"),
      pullImageHost: $("pull-image-host"),
      pullImageRepo: $("pull-image-repo"),
      pullImageTag: $("pull-image-tag"),
      pullImageLog: $("pull-image-log"),
      pullImageError: $("pull-image-error"),
      pullImageSubmit: $("pull-image-submit"),
      pullImageCancel: $("pull-image-cancel"),

      buildImageDialog: $("build-image-dialog"),
      buildImageForm: $("build-image-form"),
      buildImageHost: $("build-image-host"),
      buildImageTag: $("build-image-tag"),
      buildImageDockerfile: $("build-image-dockerfile"),
      buildImageLog: $("build-image-log"),
      buildImageError: $("build-image-error"),
      buildImageSubmit: $("build-image-submit"),
      buildImageCancel: $("build-image-cancel"),

      createVolumeDialog: $("create-volume-dialog"),
      createVolumeForm: $("create-volume-form"),
      createVolumeHost: $("create-volume-host"),
      createVolumeName: $("create-volume-name"),
      createVolumeDriver: $("create-volume-driver"),
      createVolumeError: $("create-volume-error"),
      createVolumeCancel: $("create-volume-cancel"),

      createNetworkDialog: $("create-network-dialog"),
      createNetworkForm: $("create-network-form"),
      createNetworkHost: $("create-network-host"),
      createNetworkName: $("create-network-name"),
      createNetworkDriver: $("create-network-driver"),
      createNetworkInternal: $("create-network-internal"),
      createNetworkSubnet: $("create-network-subnet"),
      createNetworkGateway: $("create-network-gateway"),
      createNetworkError: $("create-network-error"),
      createNetworkCancel: $("create-network-cancel"),

      createContainerDialog: $("create-container-dialog"),
      createContainerForm: $("create-container-form"),
      createContainerTitle: $("create-container-title"),
      createContainerHost: $("create-container-host"),
      createContainerName: $("create-container-name"),
      createContainerImage: $("create-container-image"),
      createContainerCommand: $("create-container-command"),
      createContainerEnvRows: $("create-container-env-rows"),
      createContainerEnvAdd: $("create-container-env-add"),
      createContainerPortRows: $("create-container-port-rows"),
      createContainerPortAdd: $("create-container-port-add"),
      createContainerVolumeRows: $("create-container-volume-rows"),
      createContainerVolumeAdd: $("create-container-volume-add"),
      createContainerRestart: $("create-container-restart"),
      createContainerNetwork: $("create-container-network"),
      createContainerNetworkList: $("create-container-network-list"),
      createContainerStart: $("create-container-start"),
      createContainerError: $("create-container-error"),
      createContainerSubmit: $("create-container-submit"),
      createContainerCancel: $("create-container-cancel"),

      deployStackDialog: $("deploy-stack-dialog"),
      deployStackForm: $("deploy-stack-form"),
      deployStackHost: $("deploy-stack-host"),
      deployStackProject: $("deploy-stack-project"),
      deployStackCompose: $("deploy-stack-compose"),
      deployStackTemplatePicker: $("deploy-stack-template-picker"),
      deployStackSaveTemplate: $("deploy-stack-save-template"),
      deployStackValidation: $("deploy-stack-validation"),
      deployStackValidate: $("deploy-stack-validate"),
      deployStackLog: $("deploy-stack-log"),
      deployStackError: $("deploy-stack-error"),
      deployStackSubmit: $("deploy-stack-submit"),
      deployStackCancel: $("deploy-stack-cancel"),

      stackFileDialog: $("stack-file-dialog"),
      stackFileForm: $("stack-file-form"),
      stackFilePath: $("stack-file-path"),
      stackFileContent: $("stack-file-content"),
      stackFileLog: $("stack-file-log"),
      stackFileError: $("stack-file-error"),
      stackFileCancel: $("stack-file-cancel"),
      stackFileDownload: $("stack-file-download"),
      stackFileRedeploy: $("stack-file-redeploy"),
      stackFileSave: $("stack-file-save"),

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
      settingsLogRefreshWrap: $("settings-log-refresh-wrap"),
      settingsRegistryList: $("settings-registry-list"),
      settingsRegistryHost: $("settings-registry-host"),
      settingsRegistryUsername: $("settings-registry-username"),
      settingsRegistryPassword: $("settings-registry-password"),
      settingsRegistryInsecure: $("settings-registry-insecure"),
      settingsRegistryAdd: $("settings-registry-add"),
      settingsRegistryError: $("settings-registry-error"),
      settingsAiKey: $("settings-ai-key"),
      settingsAiKeyHint: $("settings-ai-key-hint"),
      settingsAiModel: $("settings-ai-model"),
      settingsAiModelList: $("settings-ai-model-list"),
      aiFab: $("ai-assistant-open"),
      aiAssistantDialog: $("ai-assistant-dialog"),
      aiAssistantClose: $("ai-assistant-close"),
      aiAssistantMessages: $("ai-assistant-messages"),
      aiAssistantForm: $("ai-assistant-form"),
      aiAssistantInput: $("ai-assistant-input"),
      aiAssistantSend: $("ai-assistant-send")
    };

    initTheme();
    renderLegend();
    checkSetup();
    loadSettings();

    el.aiFab.addEventListener("click", openAssistant);
    el.aiAssistantClose.addEventListener("click", closeAssistant);
    el.aiAssistantForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var value = el.aiAssistantInput.value.trim();
      if (!value || state.assistant.loading) return;
      el.aiAssistantInput.value = "";
      assistantSend(value);
    });
    el.aiAssistantInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        el.aiAssistantForm.requestSubmit();
      }
    });

    el.refresh.addEventListener("click", refresh);
    el.addHost.addEventListener("click", openHostDialog);
    el.portMapOpen.addEventListener("click", openPortMap);
    el.cleanupOpen.addEventListener("click", openCleanup);
    el.cleanupContainers.addEventListener("click", function () { runCleanup("containers"); });
    el.cleanupImages.addEventListener("click", function () { runCleanup("images"); });
    el.cleanupVolumes.addEventListener("click", function () { runCleanup("volumes"); });
    el.cleanupNetworks.addEventListener("click", function () { runCleanup("networks"); });
    el.tabContainers.addEventListener("click", function () { selectTab("containers"); });
    el.tabOs.addEventListener("click", function () { selectTab("os"); });
    el.tabCompose.addEventListener("click", function () { selectTab("compose"); });
    el.tabEvents.addEventListener("click", function () { selectTab("events"); });
    el.tabImages.addEventListener("click", function () { selectTab("images"); });
    el.tabVolumes.addEventListener("click", function () { selectTab("volumes"); });
    el.tabNetworks.addEventListener("click", function () { selectTab("networks"); });

    el.pullImageForm.addEventListener("submit", submitPullImage);
    el.pullImageCancel.addEventListener("click", function () { el.pullImageDialog.close(); });
    el.buildImageForm.addEventListener("submit", submitBuildImage);
    el.buildImageCancel.addEventListener("click", function () { el.buildImageDialog.close(); });
    el.createVolumeForm.addEventListener("submit", submitCreateVolume);
    el.createVolumeCancel.addEventListener("click", function () { el.createVolumeDialog.close(); });
    el.createNetworkForm.addEventListener("submit", submitCreateNetwork);
    el.createNetworkCancel.addEventListener("click", function () { el.createNetworkDialog.close(); });
    el.createContainerForm.addEventListener("submit", submitCreateContainer);
    el.createContainerCancel.addEventListener("click", function () { el.createContainerDialog.close(); });
    el.createContainerEnvAdd.addEventListener("click", function () { addEnvRow(); });
    el.createContainerPortAdd.addEventListener("click", function () { addPortRow(); });
    el.createContainerVolumeAdd.addEventListener("click", function () { addVolumeRow(); });
    el.deployStackForm.addEventListener("submit", submitDeployStack);
    el.deployStackCancel.addEventListener("click", function () { el.deployStackDialog.close(); });
    el.deployStackTemplatePicker.addEventListener("change", applyStackTemplate);
    el.deployStackSaveTemplate.addEventListener("click", saveStackTemplate);
    el.deployStackValidate.addEventListener("click", runValidateStack);
    el.stackFileCancel.addEventListener("click", function () { el.stackFileDialog.close(); });
    el.stackFileDownload.addEventListener("click", function () {
      var ctx = state.stackFileContext;
      var name = (ctx && ctx.stack && ctx.stack.project) || "docker-compose";
      downloadTextFile(name + ".yml", el.stackFileContent.value, "text/yaml");
    });
    el.stackFileSave.addEventListener("click", saveStackFile);
    el.stackFileRedeploy.addEventListener("click", redeployFromFileDialog);

    el.setupForm.addEventListener("submit", submitSetup);
    el.hostForm.addEventListener("submit", submitHost);
    el.hostCancel.addEventListener("click", function () { el.hostDialog.close(); });
    el.hostCopy.addEventListener("click", copyCommand);
    el.confirmCancel.addEventListener("click", function () { el.confirmDialog.close(""); });
    el.settingsOpen.addEventListener("click", openSettings);
    el.settingsRegistryAdd.addEventListener("click", addRegistry);
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
      if (state.tab === "events") { loadAllEvents(); return; }
      render();
    });

    var searchTimer = null;
    el.search.addEventListener("input", function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        var value = el.search.value.trim().toLowerCase();
        if (state.tab === "os") state.osQuery = value;
        else if (state.tab === "compose") state.composeQuery = value;
        else if (state.tab === "events") state.eventsQuery = value;
        else if (state.tab === "images") state.imagesQuery = value;
        else if (state.tab === "volumes") state.volumesQuery = value;
        else if (state.tab === "networks") state.networksQuery = value;
        else state.query = value;
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
