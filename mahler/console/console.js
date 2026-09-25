

/* The operator console's browser side (DESIGN D27).
   The server renders everything; this script only keeps browser-side state
   (view, tab, theme, expanded groups, an open overlay) as attributes on
   <html>, sends writes, and swaps #app for a fresh render every 30s and after
   every write. No transitions: every change is instant. */
(function () {
  "use strict";
  var root = document.documentElement;
  var app = document.getElementById("app");
  var loadedRevision = root.getAttribute("data-console-revision");
  var reloading = false;
  var REFRESH_MS = 30000;
  var THEMES = ["auto", "light", "dark"];
  var openRun = null;
  var openRevert = null;
  var openBug = null;           // the ref whose bug sheet is open (mahler#250)
  var openRelease = null;       // the project whose release preview is open (mahler#359)
  var openCapture = false;
  var suppressKeep = [];        // data-keep keys to drop on the next restore (mahler#251)
  var errorToastTimer = null;   // timer for auto-dismissing error toast
  var settingsDirty = false;    // never poll-refresh an unsaved settings form

  function e(value) {
    var span = document.createElement("span");
    span.textContent = value == null ? "" : String(value);
    return span.innerHTML;
  }
  function store(kind, key, value) {
    try { (kind === "local" ? localStorage : sessionStorage).setItem(key, value); } catch (e) {}
  }
  function load(kind, key) {
    try { return (kind === "local" ? localStorage : sessionStorage).getItem(key); } catch (e) { return null; }
  }
  function openGroups() {
    try { return JSON.parse(load("local", "mahler.open") || "{}"); } catch (e) { return {}; }
  }
  function counts() {
    var el = document.getElementById("counts");
    try { return JSON.parse(el ? el.textContent : "{}"); } catch (e) { return {}; }
  }

  function openNeedDetails() {
    try { return JSON.parse(load("session", "mahler.needDetails") || "[]"); }
    catch (e) { return []; }
  }
  function syncNeedDetails() {
    var open = openNeedDetails();
    var details = app.querySelectorAll("details[data-need-details]");
    for (var i = 0; i < details.length; i++) {
      details[i].open = open.indexOf(details[i].getAttribute("data-need-details")) !== -1;
    }
  }

  // expanded hold-reason rows (mahler#269): the open state lives on <html> as
  // data-why-<i>, which survives the 30s refresh; mirror it onto the row so
  // CSS can show its item list
  function syncWhy() {
    var whys = app.querySelectorAll(".why[data-why]");
    for (var w = 0; w < whys.length; w++) {
      var key = whys[w].getAttribute("data-why");
      whys[w].classList.toggle("open", root.getAttribute("data-" + key) === "open");
    }
  }

  // re-apply browser-side state to a freshly rendered #app
  var runLogTimer = null;
  function pollRunLog() {
    if (!openRun) {
      if (runLogTimer) { clearInterval(runLogTimer); runLogTimer = null; }
      return;
    }
    var panel = app.querySelector('[data-run-log="' + openRun + '"]');
    if (!panel) return;
    fetch("/api/run/" + openRun + "/log")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.lines) return;
        var html = [];
        for (var i = 0; i < data.lines.length; i++) {
          var ln = data.lines[i];
          var tone = ln.tone || "mut";
          html.push('<div class="log-line">' +
                    '<span class="mono t-mut" style="flex-shrink:0">' + e(ln.t || "") + '</span>' +
                    '<span class="mono t-' + tone + '">' + e(ln.text) + '</span>' +
                    '</div>');
        }
        panel.innerHTML = html.join("");
      }).catch(function () {});
  }
  function apply() {
    var theme = root.getAttribute("data-theme") || "auto";
    var labels = app.querySelectorAll("[data-tl]");
    for (var i = 0; i < labels.length; i++) {
      labels[i].hidden = labels[i].getAttribute("data-tl") !== theme;
    }
    var groups = openGroups();
    var gs = app.querySelectorAll("[data-group]");
    for (var j = 0; j < gs.length; j++) {
      gs[j].classList.toggle("open", !!groups[gs[j].getAttribute("data-group")]);
    }
    syncWhy();
    syncNeedDetails();
    setCapacityMode(capacityMode());
    var ovs = app.querySelectorAll("[data-run-detail]");
    var shown = false;
    for (var k = 0; k < ovs.length; k++) {
      var on = ovs[k].getAttribute("data-run-detail") === openRun;
      ovs[k].classList.toggle("show", on);
      shown = shown || on;
    }
    if (!shown) { openRun = null; }
    if (openRun) {
      if (!runLogTimer) {
        pollRunLog();
        runLogTimer = setInterval(pollRunLog, 10000);
      }
    } else {
      if (runLogTimer) { clearInterval(runLogTimer); runLogTimer = null; }
    }
    var reverts = app.querySelectorAll("[data-revert-detail]");
    var revertShown = false;
    for (var r = 0; r < reverts.length; r++) {
      var visible = reverts[r].getAttribute("data-revert-detail") === openRevert;
      reverts[r].classList.toggle("show", visible);
      revertShown = revertShown || visible;
    }
    if (!revertShown) { openRevert = null; }
    var bugs = app.querySelectorAll("[data-bug-detail]");
    var bugShown = false;
    for (var b = 0; b < bugs.length; b++) {
      var bugOn = bugs[b].getAttribute("data-bug-detail") === openBug;
      bugs[b].classList.toggle("show", bugOn);
      bugShown = bugShown || bugOn;
    }
    if (!bugShown) { openBug = null; }
    var caps = app.querySelectorAll("[data-capture-detail]");
    for (var c = 0; c < caps.length; c++) {
      caps[c].classList.toggle("show", openCapture);
    }
    var rels = app.querySelectorAll("[data-release-detail]");
    var relShown = false;
    for (var rl = 0; rl < rels.length; rl++) {
      var relOn = rels[rl].getAttribute("data-release-detail") === openRelease;
      rels[rl].classList.toggle("show", relOn);
      relShown = relShown || relOn;
      if (relOn) {
        var inp = rels[rl].querySelector(".ver-input");
        if (inp && inp.value) {
          var disp = rels[rl].querySelector(".sel-ver-display");
          if (disp) { disp.textContent = "v" + inp.value.trim(); }
          var btnTxt = rels[rl].querySelector(".sel-ver-btn-txt");
          if (btnTxt) { btnTxt.textContent = inp.value.trim(); }
        }
      }
    }
    if (!relShown) { openRelease = null; }
    
    var wraps = app.querySelectorAll(".attach-wrap");
    for (var w = 0; w < wraps.length; w++) {
      var idIn = wraps[w].querySelector(".attach-id");
      var nameIn = wraps[w].querySelector(".attach-name");
      var btn = wraps[w].querySelector(".attach-btn");
      if (idIn && idIn.value) {
        btn.textContent = (nameIn ? nameIn.value : "Attached") + " ✓";
        btn.classList.add("attached");
      } else if (btn) {
        btn.textContent = "Attach photo or screenshot";
        btn.classList.remove("attached");
      }
    }
    
    applyCaptureProject();
  }

  // the capture select remembers the last project you saved to, in
  // localStorage rather than data-keep, so it survives a real page load too
  function updateCaptureSave(sel) {
    var cap = sel.closest(".cap");
    var save = cap && cap.querySelector("[data-capture-save]");
    if (save) { save.disabled = !sel.value; }
  }
  function applyCaptureProject() {
    var saved = load("local", "mahler.capture.project");
    var sels = app.querySelectorAll("[data-capture-select]");
    for (var i = 0; i < sels.length; i++) {
      if (saved) {
        for (var j = 0; j < sels[i].options.length; j++) {
          if (sels[i].options[j].value === saved) { sels[i].value = saved; break; }
        }
      }
      updateCaptureSave(sels[i]);
    }
  }

  function setView(view) {
    root.setAttribute("data-view", view);
    store("session", "mahler.view", view);
    if (view === "history") { markSeen(); }
  }
  function setTab(tab) {
    root.setAttribute("data-tab", tab);
    store("session", "mahler.tab", tab);
    window.scrollTo(0, 0);
  }

  function statsRange() {
    return load("local", "mahler.stats.range") || root.getAttribute("data-stats-range") || "week";
  }

  function capacityMode() {
    var value = load("local", "mahler.capacity.mode");
    return value === "capability" ? value : "quota";
  }
  function setCapacityMode(value) {
    value = value === "capability" ? value : "quota";
    root.setAttribute("data-capacity-mode", value);
    store("local", "mahler.capacity.mode", value);
  }
  function statsUrl() {
    var value = statsRange();
    if (value.indexOf("custom:") === 0) {
      var bits = value.split(":");
      return "/fragment?range=custom&start=" + encodeURIComponent(bits[1] || "") +
             "&end=" + encodeURIComponent(bits[2] || "");
    }
    return "/fragment?range=" + encodeURIComponent(value);
  }
  function setRange(value) {
    root.setAttribute("data-stats-range", value);
    store("local", "mahler.stats.range", value);
    refresh(true);
  }

  // A full reload cannot use the fragment swap's draft restoration. Defer it
  // while any form has edits, including drafts whose input has lost focus.
  function reloadHasDraft() {
    if (settingsDirty) { return true; }
    var fields = app.querySelectorAll("input, textarea, select");
    for (var i = 0; i < fields.length; i++) {
      var field = fields[i];
      if (field.tagName === "SELECT") {
        var defaultIndex = 0;
        for (var j = 0; j < field.options.length; j++) {
          if (field.options[j].defaultSelected) { defaultIndex = j; }
        }
        if (field.selectedIndex !== defaultIndex) { return true; }
      } else if (field.value !== field.defaultValue || field.checked !== field.defaultChecked) {
        return true;
      }
    }
    return false;
  }

  function acceptRevision(html) {
    var fragment = document.createElement("template");
    fragment.innerHTML = html;
    var marker = fragment.content.querySelector("[data-console-revision]");
    var revision = marker && marker.getAttribute("data-console-revision");
    if (revision === loadedRevision) { return !reloading; }
    // Missing markers can mean an older server during rollback. Never install
    // incompatible controls, and never repeatedly reload an unversioned response.
    if (!revision || reloading || reloadHasDraft()) { return false; }
    store("session", "mahler.view", root.getAttribute("data-view"));
    store("session", "mahler.tab", root.getAttribute("data-tab"));
    store("local", "mahler.theme", root.getAttribute("data-theme"));
    reloading = true;
    window.location.reload();
    return false;
  }

  function refresh(force) {
    if (document.hidden) { return Promise.resolve(); }
    if (settingsDirty) { return Promise.resolve(); }
    var active = document.activeElement;
    if (!force && active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA") && active.value) {
      return Promise.resolve();          // never swap the page out from under typing
    }
    // Clear any error toast on successful refresh
    if (errorToastTimer) { clearTimeout(errorToastTimer); errorToastTimer = null; }
    var toast = document.getElementById("error-toast");
    if (toast) { toast.remove(); }
    var skip = suppressKeep;
    suppressKeep = [];
    return fetch(statsUrl(), { cache: "no-store" }).then(function (r) {
      if (!r.ok) { throw new Error("refresh " + r.status); }
      return r.text();
    }).then(function (html) {
      if (settingsDirty || !acceptRevision(html)) { return; }
      var keep = {};
      var inputs = app.querySelectorAll("[data-keep]");
      for (var i = 0; i < inputs.length; i++) {
        var k = inputs[i].getAttribute("data-keep");
        if (inputs[i].value && skip.indexOf(k) === -1) { keep[k] = inputs[i].value; }
      }
      // Both layouts carry the same key; the active draft wins over its hidden twin.
      var focused = document.activeElement;
      if (focused && focused.hasAttribute("data-keep") &&
          skip.indexOf(focused.getAttribute("data-keep")) === -1) {
        keep[focused.getAttribute("data-keep")] = focused.value;
      }
      app.innerHTML = html;
      var again = app.querySelectorAll("[data-keep]");
      for (var j = 0; j < again.length; j++) {
        var v = keep[again[j].getAttribute("data-keep")];
        if (v !== undefined) { again[j].value = v; }
      }
      apply();
    }).catch(function (err) { if (window.console) { console.warn(err); } });
  }

  function showErrorToast(message) {
    if (errorToastTimer) { clearTimeout(errorToastTimer); }
    var toast = document.getElementById("error-toast");
    if (toast) { toast.remove(); }
    toast = document.createElement("div");
    toast.id = "error-toast";
    toast.className = "bn bn-bad";
    toast.style.position = "fixed";
    toast.style.top = "12px";
    toast.style.right = "12px";
    toast.style.left = "12px";
    toast.style.zIndex = "30";
    toast.style.maxWidth = "560px";
    toast.style.margin = "0 auto";
    toast.innerHTML = '<span class="kind mono">Action failed</span>' +
                      '<span class="txt">' + message + '</span>';
    document.body.appendChild(toast);
    errorToastTimer = setTimeout(function () {
      if (toast.parentNode) { toast.remove(); }
      errorToastTimer = null;
    }, 5000);
  }

  function showSavedToast(message) {
    var toast = document.getElementById("saved-toast");
    if (toast) { toast.remove(); }
    toast = document.createElement("div");
    toast.id = "saved-toast";
    toast.className = "bn";
    toast.style.position = "fixed";
    toast.style.top = "12px";
    toast.style.right = "12px";
    toast.style.left = "12px";
    toast.style.zIndex = "30";
    toast.style.maxWidth = "560px";
    toast.style.margin = "0 auto";
    toast.style.background = "var(--bg)";
    toast.style.borderColor = "var(--good)";
    toast.innerHTML = '<span class="kind mono t-good">Saved</span><span class="txt">' + e(message) + '</span>';
    document.body.appendChild(toast);
    setTimeout(function () { if (toast.parentNode) { toast.remove(); } }, 4000);
  }

  function post(action, payload) {
    return fetch("/api/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Mahler-Console": "1" },
      body: JSON.stringify(payload || {}),
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false }; });
    }).then(function (res) {
      if (!res.ok) {
        if (window.console) { console.warn(action, res.error || "failed"); }
        showErrorToast(res.error || "The action was refused or failed.");
        if (action === "settings") { return; }
      } else if (action === "end_session") {
        showSavedToast("Session ended — hold lifted.");
      } else if (action === "capture") {
        // Clear every part of the composer on the next restore, but keep the project.
        suppressKeep = ["capture", "capture_att_id", "capture_att_name"];
        if (payload && payload.project) { store("local", "mahler.capture.project", payload.project); }
        openCapture = false;
      } else if (action === "cut_release") {
        openRelease = null;
      } else if (action === "settings") {
        settingsDirty = false;
        showSavedToast("Settings will apply on the next scheduler tick.");
      } else if (action === "answer" && res.resuming) {
        showSavedToast("Answer posted — resuming.");
      }
      return refresh(true);
    });
  }

  function numberValue(form, name) {
    var el = form.querySelector('[data-setting="' + name + '"]');
    return el && el.value !== "" ? Number(el.value) : "";
  }

  function settingsPayload(form) {
    var payload = { platforms: [], routing: [], concurrency: { total: 0, by_tier: {} },
      projects: [], scheduler: {} };
    var platforms = form.querySelectorAll("[data-setting-platform]");
    for (var i = 0; i < platforms.length; i++) {
      var row = platforms[i];
      function field(name) { return row.querySelector('[data-platform-field="' + name + '"]'); }
      payload.platforms.push({
        name: row.getAttribute("data-setting-platform"),
        enabled: field("enabled").checked,
        provider: field("provider").value,
        model: field("model").value,
        sort_model: field("sort_model").value,
        build_model: field("build_model").value
      });
    }
    var scopes = form.querySelectorAll("[data-route-scope]");
    for (var s = 0; s < scopes.length; s++) {
      var route = { key: scopes[s].getAttribute("data-route-scope") };
      var roles = scopes[s].querySelectorAll("[data-route-role]");
      for (var r = 0; r < roles.length; r++) {
        var names = [];
        var entries = roles[r].querySelectorAll("[data-route-platform]");
        for (var n = 0; n < entries.length; n++) { names.push(entries[n].getAttribute("data-route-platform")); }
        route[roles[r].getAttribute("data-route-role")] = names;
      }
      payload.routing.push(route);
    }
    payload.concurrency.total = numberValue(form, "concurrency.total");
    for (var tier = 1; tier <= 4; tier++) {
      var cap = numberValue(form, "concurrency.tier." + tier);
      if (cap !== "") { payload.concurrency.by_tier[String(tier)] = cap; }
    }
    var projectInputs = form.querySelectorAll('[data-setting^="project."]');
    for (var p = 0; p < projectInputs.length; p++) {
      payload.projects.push({ name: projectInputs[p].getAttribute("data-setting").slice(8),
        max_parallel: Number(projectInputs[p].value) });
    }
    var schedulerInputs = form.querySelectorAll('[data-setting^="scheduler."]');
    for (var t = 0; t < schedulerInputs.length; t++) {
      payload.scheduler[schedulerInputs[t].getAttribute("data-setting").slice(10)] = Number(schedulerInputs[t].value);
    }
    return payload;
  }

  function routeItem(name) {
    var li = document.createElement("li");
    li.setAttribute("data-route-platform", name);
    var label = document.createElement("span");
    label.className = "mono";
    label.textContent = name;
    li.appendChild(label);
    var buttons = document.createElement("span");
    buttons.className = "route-buttons";
    [["↑", "up", "Move up"], ["↓", "down", "Move down"]].forEach(function (spec) {
      var button = document.createElement("button");
      button.type = "button"; button.className = "btn"; button.textContent = spec[0];
      button.setAttribute("data-route-move", spec[1]); button.setAttribute("aria-label", spec[2]);
      buttons.appendChild(button);
    });
    var remove = document.createElement("button");
    remove.type = "button"; remove.className = "btn btn-bad"; remove.textContent = "Remove";
    remove.setAttribute("data-route-remove", ""); buttons.appendChild(remove);
    li.appendChild(buttons);
    return li;
  }

  function markSeen() {
    var c = counts();
    if (c.digest) { post("digest_seen", { upto: c.digest_upto }); }
  }

  function payloadFor(el, act) {
    if (act === "end_session") { return { project: el.getAttribute("data-project") }; }
    if (act === "clear_backoff") {
      return { platforms: (el.getAttribute("data-platforms") || "").split(",").filter(Boolean) };
    }
    if (act === "revert") { return { event: Number(el.getAttribute("data-event")) }; }
    if (act === "uat_pass") {
      return { project: el.getAttribute("data-project"),
        number: Number(el.getAttribute("data-number")) };
    }
    if (act === "uat_fail") {
      var bug = el.closest(".bugov");
      var ta = bug && bug.querySelector("textarea");
      var attachId = bug && bug.querySelector(".attach-id");
      return { project: el.getAttribute("data-project"),
        number: Number(el.getAttribute("data-number")),
        note: ta ? ta.value : "",
        attachment: (attachId && attachId.value) ? attachId.value : null };
    }
    if (act === "cut_release") {
      var ov = el.closest(".releaseov");
      var proj = el.getAttribute("data-project") || (ov ? ov.getAttribute("data-release-detail") : "");
      var verInp = ov ? ov.querySelector(".ver-input") : null;
      var shaInp = ov ? ov.querySelector(".rel-sha") : null;
      var itemsInp = ov ? ov.querySelector(".rel-items") : null;
      var notesPre = ov ? ov.querySelector(".notes-pre") : null;
      var itemNums = (itemsInp && itemsInp.value) ? itemsInp.value.split(",").map(Number).filter(Boolean) : [];
      return {
        project: proj,
        version: verInp ? verInp.value.trim() : "",
        checkpoint_sha: shaInp ? shaInp.value.trim() : "",
        item_numbers: itemNums,
        notes: notesPre ? notesPre.textContent : null
      };
    }
    if (act === "answer_undo") { return { id: Number(el.getAttribute("data-id")) }; }
    if (act === "answer") {
      var input = el.parentElement.querySelector("input");
      return { project: el.getAttribute("data-project"), number: Number(el.getAttribute("data-number")),
        text: el.hasAttribute("data-text") ? el.getAttribute("data-text") : (input ? input.value : "") };
    }
    if (act === "digest_seen") { return { upto: Number(el.getAttribute("data-upto")) || 0 }; }
    if (act === "brief_seen") {
      return { project: el.getAttribute("data-project"),
        upto: Number(el.getAttribute("data-upto")) || 0 };
    }
    if (act === "stop_run") { return { run: Number(el.getAttribute("data-run")) }; }
    if (act === "capture") {
      var cap = el.closest(".cap");
      var ta = cap && cap.querySelector(".cap-ta");
      var sel = cap && cap.querySelector("[data-capture-select]");
      var attachId = cap && cap.querySelector(".attach-id");
      return { text: ta ? ta.value : "", project: sel ? sel.value : "", attachment: (attachId && attachId.value) ? attachId.value : null };
    }
    var payload = {};
    for (var i = 0; i < el.attributes.length; i++) {
      var attr = el.attributes[i];
      if (attr.name.indexOf("data-") === 0 && attr.name !== "data-act") {
        payload[attr.name.slice(5)] = attr.value;
      }
    }
    return payload;
  }

  // a needs-you ping deep-links here: #needs/<project>/<n> lands the console on
  // that item in Triage (phone) / Needs you (desktop), no animation (mahler#257)
  function applyHash() {
    var m = /^#needs\/([^/]+)\/(\d+)$/.exec(location.hash || "");
    if (!m) { return; }
    var ref = m[1] + "#" + m[2];
    setView("needs");
    root.setAttribute("data-tab", "triage");
    store("session", "mahler.tab", "triage");
    apply();
    var el = app.querySelector('[data-need="' + ref.replace(/"/g, "") + '"]');
    if (el) { el.scrollIntoView({ behavior: "auto", block: "nearest" }); }
    history.replaceState(null, "", location.pathname + location.search);
  }

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("button, a");
    if (!el || !app.contains(el)) { return; }
    if (el.hasAttribute("data-theme-cycle")) {
      var cur = root.getAttribute("data-theme") || "auto";
      var next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
      root.setAttribute("data-theme", next);
      store("local", "mahler.theme", next);
      apply();
      return;
    }
    if (el.hasAttribute("data-go")) {
      var group = el.getAttribute("data-open-group");
      if (group) {
        var g = openGroups(); g[group] = true;
        store("local", "mahler.open", JSON.stringify(g));
      }
      setView(el.getAttribute("data-go"));
      apply();
      if (group) {
        var target = app.querySelector('.dk [data-group="' + group.replace(/"/g, "") + '"]');
        if (target) { target.scrollIntoView(); }
      }
      return;
    }
    if (el.hasAttribute("data-tab-go")) { setTab(el.getAttribute("data-tab-go")); return; }
    if (el.hasAttribute("data-capacity-mode")) {
      setCapacityMode(el.getAttribute("data-capacity-mode"));
      return;
    }
    if (el.hasAttribute("data-route-move")) {
      var item = el.closest("[data-route-platform]");
      if (item && el.getAttribute("data-route-move") === "up" && item.previousElementSibling) {
        item.parentNode.insertBefore(item, item.previousElementSibling);
      } else if (item && el.getAttribute("data-route-move") === "down" && item.nextElementSibling) {
        item.parentNode.insertBefore(item.nextElementSibling, item);
      }
      settingsDirty = true;
      return;
    }
    if (el.hasAttribute("data-route-remove")) {
      var removeItem = el.closest("[data-route-platform]");
      if (removeItem) { removeItem.remove(); settingsDirty = true; }
      return;
    }
    if (el.hasAttribute("data-route-add")) {
      var role = el.closest("[data-route-role]");
      var select = role && role.querySelector(".route-add select");
      var list = role && role.querySelector(".route-list");
      var exists = false;
      var current = list ? list.querySelectorAll("[data-route-platform]") : [];
      for (var ci = 0; ci < current.length; ci++) {
        if (current[ci].getAttribute("data-route-platform") === select.value) { exists = true; }
      }
      if (select && list && !exists) {
        list.appendChild(routeItem(select.value)); settingsDirty = true;
      }
      return;
    }
    if (el.hasAttribute("data-stats-range")) {
      setRange(el.getAttribute("data-stats-range"));
      return;
    }
    if (el.hasAttribute("data-stats-custom")) {
      var controls = el.closest(".stats-controls");
      var start = controls && controls.querySelector("[data-stats-start]");
      var end = controls && controls.querySelector("[data-stats-end]");
      if (start && end && start.value && end.value && start.value <= end.value) {
        setRange("custom:" + start.value + ":" + end.value);
      }
      return;
    }
    if (el.hasAttribute("data-toggle")) {
      var name = "data-" + el.getAttribute("data-toggle");
      if (root.getAttribute(name) === "open") { root.removeAttribute(name); }
      else { root.setAttribute(name, "open"); }
      syncWhy();
      return;
    }
    if (el.hasAttribute("data-toggle-group")) {
      var key = el.getAttribute("data-toggle-group");
      var gs = openGroups(); gs[key] = !gs[key];
      store("local", "mahler.open", JSON.stringify(gs));
      apply();
      return;
    }
    if (el.hasAttribute("data-open-revert")) {
      openRevert = el.getAttribute("data-open-revert"); apply();
      var keep = app.querySelector('.revertov.show [data-close-revert]');
      if (keep) { keep.focus(); }
      return;
    }
    if (el.hasAttribute("data-close-revert")) { openRevert = null; apply(); return; }
    if (el.hasAttribute("data-open-bug")) {
      openBug = el.getAttribute("data-open-bug"); apply();
      var sheet = app.querySelector('.bugov.show textarea');
      if (sheet) { sheet.focus(); }
      return;
    }
    if (el.hasAttribute("data-close-bug")) { openBug = null; apply(); return; }
    if (el.hasAttribute("data-open-run")) { openRun = el.getAttribute("data-open-run"); apply(); return; }
    if (el.hasAttribute("data-close-run")) { openRun = null; apply(); return; }
    if (el.hasAttribute("data-open-capture")) {
      openCapture = true; apply();
      var ta = app.querySelector('.captureov.show textarea');
      if (ta) { ta.focus(); }
      return;
    }
    if (el.hasAttribute("data-close-capture")) { openCapture = false; apply(); return; }
    if (el.hasAttribute("data-open-release")) {
      openRelease = el.getAttribute("data-open-release"); apply();
      var verInp = app.querySelector('.releaseov.show .ver-input');
      if (verInp) { verInp.focus(); }
      return;
    }
    if (el.hasAttribute("data-close-release")) { openRelease = null; apply(); return; }
    if (el.hasAttribute("data-set-ver")) {
      var v = el.getAttribute("data-set-ver");
      var overlay = el.closest(".releaseov");
      if (overlay && v) {
        var inp = overlay.querySelector(".ver-input");
        if (inp) { inp.value = v; }
        var disp = overlay.querySelector(".sel-ver-display");
        if (disp) { disp.textContent = "v" + v; }
        var btnTxt = overlay.querySelector(".sel-ver-btn-txt");
        if (btnTxt) { btnTxt.textContent = v; }
      }
      return;
    }
    if (el.hasAttribute("data-attach")) {
      var wrap = el.closest(".attach-wrap");
      if (wrap) {
        var input = wrap.querySelector(".attach-in");
        if (input) { input.click(); }
      }
      return;
    }
    if (el.hasAttribute("data-act")) {
      var act = el.getAttribute("data-act");
      el.disabled = true;
      if (act === "digest_seen") { root.removeAttribute("data-digest"); }
      post(act, payloadFor(el, act)).catch(function (err) { if (window.console) { console.warn(err); } })
        .finally(function () { el.disabled = false; });
    }
  });

  function uploadAttachment(file, wrap) {
    var btn = wrap.querySelector(".attach-btn");
    var idIn = wrap.querySelector(".attach-id");
    var nameIn = wrap.querySelector(".attach-name");

    btn.textContent = "Uploading...";
    btn.disabled = true;

    var reader = new FileReader();
    reader.onload = function(e) {
      var dataUrl = e.target.result;
      var b64 = dataUrl.split(",")[1];

      fetch("/api/attach", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Mahler-Console": "1" },
        body: JSON.stringify({ name: file.name, type: file.type || "application/octet-stream", data: b64 }),
      }).then(function(r) { return r.json(); }).then(function(res) {
        if (!res.ok) {
          showErrorToast(res.error || "Failed to attach file.");
          btn.textContent = "Attach photo or screenshot";
          btn.classList.remove("attached");
          idIn.value = "";
          nameIn.value = "";
        } else {
          idIn.value = res.id;
          nameIn.value = res.name;
          btn.textContent = res.name + " ✓";
          btn.classList.add("attached");
        }
      }).catch(function(err) {
        showErrorToast("Failed to attach file.");
        btn.textContent = "Attach photo or screenshot";
        btn.classList.remove("attached");
        idIn.value = "";
        nameIn.value = "";
      }).finally(function() {
        btn.disabled = false;
      });
    };
    reader.readAsDataURL(file);
  }

  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (el && el.closest && el.closest("[data-settings-form]")) { settingsDirty = true; }
    if (el && el.hasAttribute && el.hasAttribute("data-capture-select")) { updateCaptureSave(el); }
    if (el && el.classList && el.classList.contains("attach-in") && el.files && el.files.length > 0) {
      var file = el.files[0];
      var wrap = el.closest(".attach-wrap");
      uploadAttachment(file, wrap);
      el.value = "";
    }
  });

  document.addEventListener("paste", function (ev) {
    if (!openCapture) { return; }
    var capture = app.querySelector(".captureov.show");
    if (!capture) { return; }
    var items = ev.clipboardData && ev.clipboardData.items;
    if (!items || !items.length) { return; }
    var lastImage = null;
    for (var i = 0; i < items.length; i++) {
      if (items[i].type.indexOf("image/") === 0) { lastImage = items[i]; }
    }
    if (!lastImage) { return; }
    ev.preventDefault();
    var blob = lastImage.getAsFile();
    if (!blob) { return; }
    var mime = blob.type || lastImage.type;
    var ext = mime.replace(/^image\//, "");
    if (ext === "jpeg") { ext = "jpg"; }
    var file = new File([blob], "paste." + ext, { type: mime });
    var wrap = capture.querySelector(".attach-wrap");
    if (wrap) { uploadAttachment(file, wrap); }
  });

  document.addEventListener("input", function (ev) {
    var el = ev.target;
    if (el && el.closest && el.closest("[data-settings-form]")) { settingsDirty = true; }
    if (el && el.classList && el.classList.contains("ver-input")) {
      var ov = el.closest(".releaseov");
      if (ov) {
        var val = el.value.trim();
        var disp = ov.querySelector(".sel-ver-display");
        if (disp) { disp.textContent = "v" + val; }
        var btnTxt = ov.querySelector(".sel-ver-btn-txt");
        if (btnTxt) { btnTxt.textContent = val; }
      }
    }
  });

  // Native details are duplicated across the phone and desktop layouts. Keep
  // both copies in sync, and restore the expanded item after a fragment swap.
  document.addEventListener("toggle", function (ev) {
    var detail = ev.target;
    if (!detail || !detail.hasAttribute || !detail.hasAttribute("data-need-details")) { return; }
    var key = detail.getAttribute("data-need-details");
    var open = openNeedDetails();
    var at = open.indexOf(key);
    if (detail.open && at === -1) { open.push(key); }
    if (!detail.open && at !== -1) { open.splice(at, 1); }
    store("session", "mahler.needDetails", JSON.stringify(open));
    var twins = app.querySelectorAll('details[data-need-details="' + key.replace(/"/g, "") + '"]');
    for (var i = 0; i < twins.length; i++) {
      if (twins[i] !== detail) { twins[i].open = detail.open; }
    }
  }, true);

  document.addEventListener("submit", function (ev) {
    var form = ev.target.closest && ev.target.closest("[data-settings-form]");
    if (!form) { return; }
    ev.preventDefault();
    if (!form.reportValidity()) { return; }
    var button = form.querySelector('[type="submit"]');
    if (button) { button.disabled = true; }
    post("settings", settingsPayload(form)).catch(function (err) {
      if (window.console) { console.warn(err); }
      showErrorToast("Settings could not be saved.");
    }).finally(function () { if (button) { button.disabled = false; } });
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && openRevert) { openRevert = null; apply(); }
    if (ev.key === "Escape" && openBug) { openBug = null; apply(); }
    if (ev.key === "Escape" && openRun) { openRun = null; apply(); }
    if (ev.key === "Escape" && openCapture) { openCapture = false; apply(); }
    if (ev.key === "Escape" && openRelease) { openRelease = null; apply(); }
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { refresh(); } });

  // a stored view or tab this page no longer has falls back to the landing one
  if (!app.querySelector(".view-" + root.getAttribute("data-view"))) { root.setAttribute("data-view", "now"); }
  if (!app.querySelector(".tabv-" + root.getAttribute("data-tab"))) { root.setAttribute("data-tab", "now"); }
  if (root.getAttribute("data-view") === "history") { markSeen(); }
  apply();
  applyHash();
  if (load("local", "mahler.stats.range")) { refresh(true); }
  setInterval(function () { refresh(); }, REFRESH_MS);
  window.addEventListener("hashchange", applyHash);
})();
