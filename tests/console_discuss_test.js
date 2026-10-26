// Node's built-in WebSocket drives headless Chrome; no browser automation package.
if (typeof document === 'undefined') {
  (async () => {
    const [socketURL, url, width] = process.argv.slice(2);
    const socket = new WebSocket(socketURL);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    let id = 0;
    const pending = new Map();
    socket.onmessage = event => {
      const data = JSON.parse(event.data);
      if (pending.has(data.id)) {
        const [resolve, reject] = pending.get(data.id); pending.delete(data.id);
        data.error ? reject(Error(JSON.stringify(data.error))) : resolve(data.result);
      }
    };
    const call = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      pending.set(++id, [resolve, reject]); socket.send(JSON.stringify({id, method, params, sessionId}));
    });
    try {
      const {targetId} = await call('Target.createTarget', {url: 'about:blank'});
      const {sessionId} = await call('Target.attachToTarget', {targetId, flatten: true});
      await call('Emulation.setDeviceMetricsOverride', {width: Number(width), height: 900, deviceScaleFactor: 1, mobile: false}, sessionId);
      await call('Page.navigate', {url}, sessionId);
      for (let i = 0; i < 200; i++) {
        const {result} = await call('Runtime.evaluate', {expression: 'document.body && document.body.dataset.testResult'}, sessionId);
        if (result.value) {
          if (result.value !== 'PASS') throw Error(result.value);
          console.log('PASS'); return;
        }
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      throw Error('Browser journey did not finish');
    } finally { socket.close(); }
  })().catch(error => { console.error(error); process.exitCode = 1; });
} else {
// Executed against the production controller and native dialog in headless Chrome.
(async () => {
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const el = name => document.getElementById('discussion-' + name);
  const dialog = document.getElementById('discussion');
  const close = () => new Promise(resolve => {
    dialog.addEventListener('close', resolve, {once: true}); dialog.requestClose();
  });
  const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
  const sessions = new Map(), requests = [], timers = [];
  let next = 0, offlineRead = false, lostResponse = false;
  const timeout = window.setTimeout;
  window.setTimeout = (fn, ms) => {
    if (ms === 2000) { timers.push(ms); return 99999; }
    return timeout(fn, ms);
  };
  window.fetch = async (url, options) => {
    const body = options.method === 'GET' ? Object.fromEntries(new URL(url, location.href).searchParams) : JSON.parse(options.body);
    requests.push({...body, method: options.method});
    if (options.method === 'GET' && offlineRead) throw Error('Disconnected');
    if (body.op === 'start') {
      let session = [...sessions.values()].find(s => s.request === body.request_id);
      if (!session) { session = {id: String(++next), request: body.request_id, turns: []}; sessions.set(session.id, session); }
      return {ok: true, json: async () => ({session_id: session.id, account: 'personal'})};
    }
    const session = sessions.get(body.session_id);
    if (body.op) {
      let turn = session.turns.find(t => t.request_id === body.request_id);
      if (!turn) session.turns.push({request_id: body.request_id, text: body.text, pending: true});
      if (lostResponse) { lostResponse = false; throw Error('Lost response'); }
      return {ok: true, json: async () => ({turn_id: session.turns.length})};
    }
    const turn = session.turns.at(-1);
    const after = Number(body.after || 0);
    const complete = session.turns.filter(t => !t.pending);
    return {ok: true, json: async () => ({
      html: complete.slice(after).map((t, i) => '<article><p>Reply ' + (after + i + 1) + ' &lt;img src=x&gt;</p>' +
        (after + i === 1 ? '<a href="https://github.com/mkny13/mahler/issues/77">Confirmed issue #77</a>' : '') + '</article>').join(''),
      after: complete.length, more: false, pending: !!(turn && turn.pending), error: turn && turn.error || null,
      turn_id: session.turns.length || null, context: 'Issue context <script>', account: 'personal', accepted_at: new Date().toISOString()
    })};
  };
  try {
    check(innerWidth === Number(new URLSearchParams(location.search).get('width')), 'exact viewport width: ' + innerWidth);
    const controller = initDiscussion();
    for (const kind of ['triage', 'capture']) {
      const button = document.getElementById(kind);
      button.focus(); button.click();
      check(dialog.matches(':modal'), 'native modal opens');
      check(document.activeElement === el('draft'), 'composer focused');
      const newline = new KeyboardEvent('keydown', {key: 'Enter', cancelable: true});
      el('draft').dispatchEvent(newline);
      check(!newline.defaultPrevented, 'plain Enter retains native newline behavior');
      check(dialog.getBoundingClientRect().width <= innerWidth, 'panel fits viewport');
      check(dialog.getBoundingClientRect().height <= innerHeight + 1, 'panel fits height');
      document.getElementById('background').focus();
      check(document.activeElement !== document.getElementById('background'), 'background is inert');
      for (let i = 0; i < 2; i++) {
        el('draft').value = 'Turn ' + i;
        el('draft').dispatchEvent(new Event('input'));
        el('draft').focus();
        if (i === 0) {
          const event = new KeyboardEvent('keydown', {key: 'Enter', ctrlKey: true, cancelable: true});
          el('draft').dispatchEvent(event);
          check(event.defaultPrevented, 'keyboard sends');
          el('send').click(); // duplicate while POST is pending
          await flush();
        } else await controller.send(false);
        const session = [...sessions.values()].at(-1);
        check(session.turns.length === i + 1, 'duplicate send has one durable turn');
        check(el('project').disabled, 'project fixed after start');
        check(el('send').disabled, 'pending send disabled');
        check(timers.includes(2000), 'pending polls at two seconds');
        offlineRead = true; await controller.poll();
        check(!el('retry').disabled, 'pending read failure allows explicit retry');
        offlineRead = false; el('retry').click(); await flush();
        // The same replacement used by the normal 30-second refresh.
        el('draft').value = 'next draft'; el('draft').dispatchEvent(new Event('input')); el('draft').focus();
        document.getElementById('app').innerHTML = document.getElementById('app').innerHTML;
        check(document.activeElement === el('draft') && el('draft').value === 'next draft', 'refresh retains focus/draft');
        session.turns.at(-1).pending = false;
        await controller.poll();
        check(el('draft').value === 'next draft', 'reply does not erase a newer draft');
        check(!el('transcript').querySelector('img'), 'reply text escaped');
        check(document.activeElement === el('draft'), 'reply does not steal focus');
      }
      check(el('transcript').textContent.indexOf('Reply 1') < el('transcript').textContent.indexOf('Reply 2'), 'ordered replies');
      check(el('transcript').querySelectorAll('a').length === 1, 'one confirmed issue link');
      const count = requests.length;
      await close(); await controller.poll();
      check(document.activeElement === document.getElementById(kind), 'close restores refreshed invoker focus: ' + kind);
      check(requests.length === count, 'closed dialog stops polling');
      const reopen = document.getElementById(kind); reopen.focus(); reopen.click(); await flush();
      check(el('transcript').querySelectorAll('article').length === 2, 'reopen resumes without POST');
      check(requests.slice(count).every(r => r.method === 'GET'), 'reopen never resubmits');
      await close();
    }
    document.getElementById('capture').click(); await flush();
    el('draft').value = 'Lost response'; el('draft').dispatchEvent(new Event('input'));
    lostResponse = true;
    await controller.send(false);
    check(!el('retry').hidden, 'lost response offers reconciliation');
    el('retry').click(); await flush();
    const session = [...sessions.values()].at(-1);
    check(session.turns.length === 3, 'lost response retry deduplicates');
    session.turns.at(-1).pending = false;
    session.turns.at(-1).error = {code: 'timeout', message: 'Timeout', retryable: true};
    await controller.poll();
    check(el('error').textContent.includes('2 minutes'), 'timeout explained');
    el('retry').click(); await flush();
    check(session.turns.length === 4 && requests.some(r => r.op === 'retry'), 'explicit failed-turn retry');
    session.turns.at(-1).pending = false;
    session.turns.at(-1).error = {code: 'gateway_restarted', message: 'Restart', retryable: true};
    await controller.poll(); check(el('error').textContent.includes('Gateway restarted'), 'restart explained');
    session.turns.at(-1).error = {code: 'quota_unavailable', message: 'Quota', retryable: true};
    await controller.poll(); check(el('error').textContent.includes('quota'), 'quota explained');
    offlineRead = true; await controller.poll();
    check(el('error').textContent.includes('unavailable'), 'read error explained');
    const before = requests.filter(r => r.method === 'POST').length;
    offlineRead = false; el('retry').click(); await flush();
    check(requests.filter(r => r.method === 'POST').length === before, 'read retry never submits a turn');
    await close();
    check(JSON.parse(sessionStorage.getItem('mahler.discussions'))['capture:mahler'].session, 'session persisted for reload');
    document.getElementById('capture').click(); await flush();
    el('new').click();
    check(!el('project').value && !el('project').disabled, 'new discussion allows project choice');
    check(document.activeElement === el('project'), 'missing project receives focus');
    el('project').value = 'mahler'; el('project').dispatchEvent(new Event('change'));
    el('draft').value = 'New conversation'; el('draft').dispatchEvent(new Event('input'));
    await controller.send(false);
    check(sessions.size === 3, 'new discussion creates a fresh session');
    await close();
    parent.document.body.dataset.testResult = 'PASS';
  } catch (error) {
    parent.document.body.dataset.testResult = 'FAIL: ' + error.stack;
  }
})();

}
