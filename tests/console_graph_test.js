// Minimal DOM boundary for the production graph lifecycle (no CDN in tests).
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("mahler/console/console.js", "utf8");
const graphCode = source.slice(source.indexOf("  var mermaidPromise"), source.indexOf("  function e(value)"));
async function scenario(failure) {
  let open = false, theme = "light", downloads = 0, renders = 0, warnings = 0;
  let graphs = [], containers = 0;
  const document = {
    createElement: () => ({style: {}, remove() { containers--; }}),
    head: {appendChild(script) {
      downloads++;
      assert(script.src.includes("mermaid@12.0.0/"));
      assert(script.integrity.startsWith("sha384-"));
      assert.equal(script.crossOrigin, "anonymous");
      queueMicrotask(() => failure === "load" ? script.onerror() : script.onload());
    }},
    body: {appendChild() { containers++; }}
  };
  const mermaid = {
    initialize(config) {
      assert.equal(config.startOnLoad, false);
      assert.equal(config.securityLevel, "loose");
      assert.equal(config.theme, theme === "dark" ? "dark" : "default");
    },
    async render(id, src) {
      renders++;
      if (failure === "render") throw Error("broken graph");
      return {svg: '<svg id="' + id + '">' + src + '</svg>'};
    }
  };
  const context = vm.createContext({
    document, window: {mermaid, matchMedia: () => ({matches: false}), console: {}},
    console: {warn() { warnings++; }},
    root: {getAttribute: key => key === "data-theme" ? theme : open ? "open" : null},
    app: {querySelectorAll: () => graphs, contains: graph => graphs.includes(graph)}
  });
  vm.runInContext(graphCode, context);
  function graph(src) {
    const classes = new Set();
    const out = {innerHTML: ""};
    return {out, classes, getAttribute: () => "view_project",
      querySelector: selector => selector === ".mermaid-src" ? {textContent: src} : out,
      classList: {add: value => classes.add(value), remove: value => classes.delete(value)}};
  }
  graphs = [graph("flowchart LR\nn1 --> n2")];
  context.renderGraphs();
  assert.equal(downloads, 0, "List must not fetch Mermaid");
  open = true;
  context.renderGraphs();
  context.renderGraphs(); // repeated apply during a pending render
  await context.graphQueue;
  assert.equal(downloads, 1);
  assert.equal(containers, 0);
  if (failure) {
    assert.equal(graphs[0].out.innerHTML, "");
    assert(!graphs[0].classes.has("rendered"), "failure must retain fallback");
    assert.equal(warnings, 1);
    return;
  }
  assert.equal(renders, 1);
  const svg = graphs[0].out.innerHTML;
  graphs = [graph("flowchart LR\nn1 --> n2")]; // poll swaps #app
  context.renderGraphs();
  assert.equal(graphs[0].out.innerHTML, svg, "cached SVG restored synchronously");
  assert(graphs[0].classes.has("rendered"));
  assert.equal(renders, 1);
  graphs = [graph("flowchart LR\nn2 --> n3")];
  context.renderGraphs();
  await context.graphQueue;
  assert.equal(renders, 2);
  theme = "dark";
  context.renderGraphs();
  await context.graphQueue;
  assert.equal(renders, 3);
  assert.equal(downloads, 1);
}
(async () => {
  await scenario(null);
  await scenario("load");
  await scenario("render");
})().catch(err => { console.error(err); process.exitCode = 1; });
