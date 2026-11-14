// mahler#1552: choosing the phase sort after another sort restores server order.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("mahler/console/console.js", "utf8");
const code = source.slice(source.indexOf("  var PROJ_FILTERS"), source.indexOf("  function refresh(force)"));
function mkRow(name, open, touched) {
  const a = {"data-phase": "building", "data-name": name, "data-open": String(open), "data-touched": touched};
  return {hidden: false, parentNode: null,
    getAttribute: k => (k in a ? a[k] : null), setAttribute: (k, v) => { a[k] = v; },
    hasAttribute: k => k in a};
}
const rows = [mkRow("c", 1, "2026-01-01"), mkRow("a", 3, "2026-03-01"), mkRow("b", 2, "")];
const parent = {children: rows.slice(), appendChild(r) {
  this.children = this.children.filter(x => x !== r); this.children.push(r); }};
rows.forEach(r => { r.parentNode = parent; });
const root = {setAttribute() {},
  querySelectorAll: sel => sel === "[data-phase][data-name]" ? parent.children.slice() : []};
let sort = "phase";
const ctx = vm.createContext({
  app: {querySelectorAll: () => [root]},
  localStorage: {getItem: k => (k === "mahler.projects.sort" ? sort : null), setItem() {}},
});
vm.runInContext(code, ctx);
const names = () => parent.children.map(r => r.getAttribute("data-name")).join("");
ctx.applyProjects();
assert.strictEqual(names(), "cab");
sort = "name"; ctx.applyProjects();
assert.strictEqual(names(), "abc");
sort = "open"; ctx.applyProjects();
assert.strictEqual(names(), "abc".split("").sort((x, y) => ({a: 3, b: 2, c: 1}[y] - {a: 3, b: 2, c: 1}[x])).join(""));
sort = "phase"; ctx.applyProjects();
assert.strictEqual(names(), "cab");
console.log("ok");
