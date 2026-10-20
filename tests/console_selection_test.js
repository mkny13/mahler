// Exercise production navigation/refresh at the DOM boundary without a browser dependency.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("mahler/console/console.js", "utf8");
const slice = (a, b) => source.slice(source.indexOf(a), source.indexOf(b));
let attrs = {"data-view": "capture", "data-tab": "now", "data-project-scope": "",
  "data-all-view": "needs", "data-all-tab": "triage"}, inputs = [], form = null;
let requests = [], swaps = 0, applied = 0, revisionNotes = 0, resize;
const browserWindow = {
  matchMedia: () => media, scrollTo() {}, console, location: {href: "http://localhost/"}
};
browserWindow.history = {pushState(state, title, url) {
  browserWindow.location.href = new URL(url, browserWindow.location.href).href;
}};
const staleNotes = [{hidden: true, textContent: "", getAttribute: () => "Couldn't refresh · showing {time}"}];
const input = value => ({value, getAttribute: () => "draft", hasAttribute: () => true});
const media = {matches: false, addEventListener: (event, callback) => { resize = callback; }};
const context = vm.createContext({
  TextEncoder, URL, console, setTimeout, clearTimeout,
  root: {getAttribute: k => attrs[k], setAttribute: (k, v) => attrs[k] = v},
  document: {hidden: false, activeElement: null, getElementById: () => null},
  window: browserWindow,
  store() {}, load() { return null; }, markSeen() {},
  settingsDirty: false, errorToastTimer: null, suppressKeep: [],
  openRun: null, openRevert: null, openBug: null, openRelease: null, openCapture: false,
  apply() { applied++; }, noteRevision() { revisionNotes++; },
  app: {
    busy: false,
    setAttribute(k) { if (k === "aria-busy") { this.busy = true; } },
    removeAttribute(k) { if (k === "aria-busy") { this.busy = false; } },
    querySelectorAll: sel => sel === "[data-stale-note]" ? staleNotes : inputs,
    querySelector: () => form,
    set innerHTML(html) {
      swaps++;
      inputs = html === "capture" ? [input("")] : [];
      form = html === "settings" ? {replaceWith(old) { form = old; }} : null;
    }
  },
  fetch(url) {
    return new Promise(resolve => requests.push({url, finish(html) {
      resolve({ok: true, text: () => Promise.resolve(html)});
    }, fail() { resolve({ok: false, status: 502}); }}));
  }
});
vm.runInContext(slice("  var layoutMedia", "  var mermaidPromise") +
  slice("  function setView", "  // refresh() swaps"), context);
async function finish(promise, html) {
  requests.at(-1).finish(html);
  await promise;
}
(async () => {
  inputs = [input("unsent")];
  let first = context.setView("now");
  assert.equal(attrs["data-view"], "now"); // highlight switches before fetch
  assert.equal(swaps, 0); // previous content stays
  let second = context.setView("models");
  assert(requests.at(-1).url.includes("layout=desktop&view=models"));
  await finish(second, "models");
  requests[0].finish("now"); await first;
  assert.equal(swaps, 1, "stale navigation must never replace the latest view");
  await finish(context.setView("capture"), "capture");
  assert.equal(inputs[0].value, "unsent", "draft survives leaving and returning");
  context.suppressKeep = ["draft"];
  await finish(context.refresh(true), "capture");
  assert.equal(inputs[0].value, "", "submitted draft clears even with identical HTML");
  const before = swaps;
  await finish(context.refresh(), "capture");
  assert.equal(swaps, before, "identical poll does not churn DOM");

  await finish(context.setView("settings"), "settings");
  form.edited = true;
  const edited = form;
  context.settingsDirty = true;
  let n = requests.length;
  await context.refresh();
  assert.equal(requests.length, n, "poll preserves unsaved settings");
  await finish(context.setView("now"), "now");
  await finish(context.refresh(), "now");
  await finish(context.setView("settings"), "settings");
  assert.strictEqual(form, edited, "route order and unsaved settings survive navigation");
  context.openBug = "mahler#9";
  let overlay = context.refresh(true);
  assert(requests.at(-1).url.includes("bug=mahler%239"));
  await finish(overlay, "settings");
  media.matches = true;
  resize();
  assert(requests.at(-1).url.includes("layout=phone"));
  assert.equal(context.document.cookie, "mahler_tab=now; Path=/; SameSite=Strict");
  requests.at(-1).finish("phone");
  await new Promise(resolve => setImmediate(resolve));
  await finish(context.setTab("triage"), "triage");
  assert.equal(attrs["data-tab"], "triage");
  assert(applied >= revisionNotes);
  const priorApplies = applied, priorSwaps = swaps;
  await finish(context.refresh(true), "triage");
  assert.equal(swaps, priorSwaps);
  assert.equal(applied, priorApplies + 1, "reopening an unchanged overlay restores visibility");

  // A failed poll keeps the old section and says so in the header; the next success hides it.
  const keptSwaps = swaps, quiet = context.console;
  context.console = {warn() {}};
  let failing = context.refresh();
  requests.at(-1).fail(); await failing;
  context.console = quiet;
  assert.equal(swaps, keptSwaps, "a failed refresh never replaces the section");
  assert.equal(staleNotes[0].hidden, false, "a failed refresh shows the stale note");
  assert.match(staleNotes[0].textContent, /^Couldn't refresh · showing \d\d:\d\d$/);
  await finish(context.refresh(), "triage");
  assert.equal(staleNotes[0].hidden, true, "the next good refresh hides the stale note");

  // A slow switch dims the old section after 150 ms and clears when it lands.
  let slow = context.setTab("triage");
  await new Promise(resolve => setTimeout(resolve, 200));
  assert.equal(context.app.busy, true, "a slow view switch shows it is loading");
  await finish(slow, "triage");
  assert.equal(context.app.busy, false, "loading state clears when the view arrives");

  let toProject = context.setProjectScope("project / one");
  assert.equal(attrs["data-project-scope"], "project / one");
  assert.equal(attrs["data-view"], "overview");
  assert.equal(new URL(browserWindow.location.href).searchParams.get("project"), "project / one");
  assert(requests.at(-1).url.includes("project=project%20%2F%20one"));
  assert(requests.at(-1).url.includes("layout=phone"));
  await finish(toProject, "overview");
  let toAll = context.setProjectScope("");
  assert.equal(new URL(browserWindow.location.href).search, "");
  assert.equal(attrs["data-view"], "needs");
  assert.equal(attrs["data-tab"], "triage");
  assert(!requests.at(-1).url.includes("&project="));
  await finish(toAll, "needs");

  // Telemetry excludes short tasks and works without the optional heap API.
  let callback, reports = [];
  context.PerformanceObserver = function (cb) {
    callback = cb;
    this.observe = options => assert.equal(options.type, "longtask");
  };
  context.performance = {now: () => 120000};
  context.fetch = (url, options) => {
    reports.push([url, JSON.parse(options.body)]);
    return Promise.resolve();
  };
  vm.runInContext(slice("  try {\n    var longTasks", "  // a stored view or tab"), context);
  callback({getEntries: () => [{duration: 999}, {duration: 1200}]});
  assert.equal(reports.length, 1);
  assert.equal(reports[0][0], "/api/client_log");
  assert.equal(reports[0][1].layout, "phone");
  assert.equal(reports[0][1].view, "triage");
  assert.equal(reports[0][1].heap_bytes, null);
  assert.equal(reports[0][1].minutes_since_load, 2);
  assert.equal(reports[0][1].fragment_bytes, 5);
  context.fetch = () => { throw Error("offline"); };
  callback({getEntries: () => [{duration: 1400}]}); // best effort never throws
})().catch(err => { console.error(err); process.exitCode = 1; });
