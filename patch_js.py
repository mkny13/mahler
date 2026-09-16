with open("mahler/console/console.js", "r") as f:
    text = f.read()

import re

poll_code = '''
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
'''

apply_old = '''    if (!shown) { openRun = null; }
    var reverts = app.querySelectorAll("[data-revert-detail]");'''

apply_new = '''    if (!shown) { openRun = null; }
    if (openRun) {
      if (!runLogTimer) {
        pollRunLog();
        runLogTimer = setInterval(pollRunLog, 10000);
      }
    } else {
      if (runLogTimer) { clearInterval(runLogTimer); runLogTimer = null; }
    }
    var reverts = app.querySelectorAll("[data-revert-detail]");'''

text = text.replace(apply_old, apply_new)
text = poll_code + "\n" + text

with open("mahler/console/console.js", "w") as f:
    f.write(text)
