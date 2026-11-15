const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync("mahler/console/console.js", "utf8");
const extract = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));
const payloadSource = extract("  function payloadFor(el, act) {", "\n  // a needs-you ping");
const discardStart = source.indexOf("  function discardPendingIssueIds(container, keep) {");
const context = vm.createContext({});
vm.runInContext(payloadSource + "\nthis.payloadFor = payloadFor;", context);
if (discardStart !== -1) {
  const discardSource = extract("  function discardPendingIssueIds(container, keep) {",
    "\n  function markSeen()");
  vm.runInContext(discardSource + "\nthis.discardPendingIssueIds = discardPendingIssueIds;", context);
}

const fields = {
  ".pissue-title": {value: "A careful title"},
  ".pissue-body": {value: "Description"},
  ".pissue-client": {value: ""}
};
const box = {querySelector: selector => fields[selector]};
const button = {
  closest: selector => selector === ".pissue" ? box : null,
  getAttribute: name => ({
    "data-key": "prototype-a",
    "data-kind": "onboard"
  }[name])
};

const first = context.payloadFor(button, "atlas_issue");
const retry = context.payloadFor(button, "atlas_issue");
assert.strictEqual(retry.client_id, first.client_id,
  "retrying an unchanged composer must reuse its client ID");
assert.deepStrictEqual(JSON.parse(fields[".pissue-client"].value)[0],
  JSON.stringify(["prototype-a", "onboard", "A careful title", "Description"]));

fields[".pissue-title"].value = "A revised title";
const revised = context.payloadFor(button, "atlas_issue");
assert.notStrictEqual(revised.client_id, first.client_id,
  "editing the request must start a distinct submission");

if (context.discardPendingIssueIds) {
  const keep = {"atlas_issue:prototype-a:onboard": fields[".pissue-client"].value};
  const pending = {
    querySelectorAll: selector => selector === "[data-pending-issue][data-key][data-kind]" ?
      [{getAttribute: name => ({
        "data-key": "prototype-a", "data-kind": "onboard"
      }[name])}] : []
  };
  context.discardPendingIssueIds(pending, keep);
  assert(!Object.hasOwn(keep, "atlas_issue:prototype-a:onboard"),
    "a visible queued action must retire its retry token for the next submission");
}
