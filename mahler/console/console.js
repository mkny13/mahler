/* The operator console's browser side (DESIGN D27).
   The server renders everything; this script only keeps browser-side state
   (view, tab, theme, expanded groups, an open overlay) as attributes on
   <html>, sends writes, and swaps #app for a fresh render every 30s and after
   every write. No transitions: every change is instant. */
(function () {
  "use strict";
  var root = document.documentElement;
  var app = document.getElementById("app");
  var REFRESH_MS = 30000;
  var THEMES = ["auto", "light", "dark"];
  var openRun = null;

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

  // re-apply browser-side state to a freshly rendered #app
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
    var ovs = app.querySelectorAll("[data-run-detail]");
    var shown = false;
    for (var k = 0; k < ovs.length; k++) {
      var on = ovs[k].getAttribute("data-run-detail") === openRun;
      ovs[k].classList.toggle("show", on);
      shown = shown || on;
    }
    if (!shown) { openRun = null; }
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

  function refresh(force) {
    if (document.hidden) { return Promise.resolve(); }
    var active = document.activeElement;
    if (!force && active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA") && active.value) {
      return Promise.resolve();          // never swap the page out from under typing
    }
    return fetch("/fragment", { cache: "no-store" }).then(function (r) {
      if (!r.ok) { throw new Error("refresh " + r.status); }
      return r.text();
    }).then(function (html) {
      var keep = {};
      var inputs = app.querySelectorAll("[data-keep]");
      for (var i = 0; i < inputs.length; i++) { if (inputs[i].value) { keep[inputs[i].getAttribute("data-keep")] = inputs[i].value; } }
      // Both layouts carry the same key; the active draft wins over its hidden twin.
      var focused = document.activeElement;
      if (focused && focused.hasAttribute("data-keep")) {
        keep[focused.getAttribute("data-keep")] = focused.value;
      }
      app.innerHTML = html;
      var again = app.querySelectorAll("[data-keep]");
      for (var j = 0; j < again.length; j++) {
        var v = keep[again[j].getAttribute("data-keep")];
        if (v !== undefined) { again[j].value = v; }
      }
      apply();
      applyHash();
    }).catch(function (err) { if (window.console) { console.warn(err); } });
  }

  function post(action, payload) {
    return fetch("/api/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Mahler-Console": "1" },
      body: JSON.stringify(payload || {}),
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false }; });
    }).then(function (res) {
      if (!res.ok && window.console) { console.warn(action, res.error || "failed"); }
      return refresh(true);
    });
  }

  function markSeen() {
    var c = counts();
    if (c.digest) { post("digest_seen", { upto: c.digest_upto }); }
  }

  function payloadFor(el, act) {
    if (act === "clear_backoff") {
      return { platforms: (el.getAttribute("data-platforms") || "").split(",").filter(Boolean) };
    }
    if (act === "answer_undo") { return { id: Number(el.getAttribute("data-id")) }; }
    if (act === "answer") {
      var input = el.parentElement.querySelector("input");
      return { project: el.getAttribute("data-project"), number: Number(el.getAttribute("data-number")),
        text: el.hasAttribute("data-text") ? el.getAttribute("data-text") : (input ? input.value : "") };
    }
    if (act === "digest_seen") { return { upto: Number(el.getAttribute("data-upto")) || 0 }; }
    return {};
  }

  // a needs-you ping deep-links here: #needs/<project>/<n> lands the console on
  // that item in Triage (phone) / Needs you (desktop), no animation (mahler#257)
  function applyHash() {
    var m = /^#needs\/([^/]+)\/(\d+)$/.exec(location.hash || "");
    if (!m) { return; }
    var ref = m[1] + "#" + m[2];
    setView("needs");
    setTab("triage");
    apply();
    var el = app.querySelector('[data-need="' + ref.replace(/"/g, "") + '"]');
    if (el) { el.scrollIntoView({ behavior: "auto", block: "nearest" }); }
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
    if (el.hasAttribute("data-toggle")) {
      var name = "data-" + el.getAttribute("data-toggle");
      if (root.getAttribute(name) === "open") { root.removeAttribute(name); }
      else { root.setAttribute(name, "open"); }
      return;
    }
    if (el.hasAttribute("data-toggle-group")) {
      var key = el.getAttribute("data-toggle-group");
      var gs = openGroups(); gs[key] = !gs[key];
      store("local", "mahler.open", JSON.stringify(gs));
      apply();
      return;
    }
    if (el.hasAttribute("data-open-run")) { openRun = el.getAttribute("data-open-run"); apply(); return; }
    if (el.hasAttribute("data-close-run")) { openRun = null; apply(); return; }
    if (el.hasAttribute("data-act")) {
      var act = el.getAttribute("data-act");
      el.disabled = true;
      if (act === "digest_seen") { root.removeAttribute("data-digest"); }
      post(act, payloadFor(el, act)).catch(function (err) { if (window.console) { console.warn(err); } })
        .finally(function () { el.disabled = false; });
    }
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && openRun) { openRun = null; apply(); }
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { refresh(); } });

  // a stored view or tab this page no longer has falls back to the landing one
  if (!app.querySelector(".view-" + root.getAttribute("data-view"))) { root.setAttribute("data-view", "now"); }
  if (!app.querySelector(".tabv-" + root.getAttribute("data-tab"))) { root.setAttribute("data-tab", "now"); }
  if (root.getAttribute("data-view") === "history") { markSeen(); }
  apply();
  applyHash();
  setInterval(function () { refresh(); }, REFRESH_MS);
  window.addEventListener("hashchange", applyHash);
})();
