#!/usr/bin/env node
// Integrated #1119 console journey (mahler#1257): drives headless Chrome over the
// DevTools protocol. Built-ins only (global WebSocket, fetch, node:sqlite): no
// Playwright, no npm packages.
//
//   node journey.mjs <console-url> --home <MAHLER_HOME of the fixture> --out <png dir>
//                    [--chrome <path>] [--project groundwork] [--other mahler]
//
// Exits 0 only if every step passes at desktop (1280x900) and phone (390x844).
// A failed assertion prints `STEP FAIL <layout>: <step>: <reason>` and exits 1.

import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const argv = process.argv.slice(2);
const opt = (name, dflt) => { const i = argv.indexOf(`--${name}`); return i >= 0 ? argv[i + 1] : dflt; };
const baseUrl = argv.find((a, i) => !a.startsWith('--') && !(i > 0 && argv[i - 1].startsWith('--')));
const home = opt('home');
const outDir = opt('out', 'screenshots');
const chromePath = opt('chrome', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
const PROJECT = opt('project', 'groundwork');
const OTHER = opt('other', 'mahler');
if (!baseUrl || !home) { console.error('usage: journey.mjs <url> --home <dir> [--out dir]'); process.exit(2); }
if (!existsSync(chromePath)) { console.error(`Chrome not found at ${chromePath}`); process.exit(3); }
mkdirSync(outDir, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- minimal CDP client ----
class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = []; }
  static async open(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws error')); });
    const c = new Cdp(ws);
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && c.pending.has(msg.id)) {
        const { res, rej } = c.pending.get(msg.id); c.pending.delete(msg.id);
        msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
      } else if (msg.method) { c.handlers.forEach((h) => h(msg)); }
    };
    return c;
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params, sessionId }));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
}

async function launchChrome() {
  const profile = mkdtempSync(join(tmpdir(), 'journey-chrome-'));
  const proc = spawn(chromePath, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-extensions', 'about:blank'], { stdio: 'ignore' });
  const portFile = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 100 && !existsSync(portFile); i++) await sleep(100);
  if (!existsSync(portFile)) { proc.kill(); throw new Error('Chrome did not publish a DevTools port'); }
  const [port, path] = readFileSync(portFile, 'utf8').trim().split('\n');
  return { proc, profile, wsUrl: `ws://127.0.0.1:${port}${path}` };
}

// ---- ledger / config readers (the fixture's, never ~/.mahler) ----
const dbRows = (sql, ...args) => {
  const db = new DatabaseSync(join(home, 'mahler.db'), { readOnly: true });
  try { return db.prepare(sql).all(...args); } finally { db.close(); }
};
const eventKinds = () => dbRows("SELECT kind FROM events WHERE kind LIKE 'priority_boost_%' OR kind LIKE 'project_%' ORDER BY id").map((r) => r.kind);
const maxParallel = (project) => {
  const text = readFileSync(join(home, 'config.toml'), 'utf8');
  const m = text.match(new RegExp(`\\[projects\\.${project}\\][^\\[]*?max_parallel\\s*=\\s*(\\d+)`));
  return m ? Number(m[1]) : null;
};

// ---- page driver ----
class Page {
  constructor(cdp, sessionId, layout) {
    Object.assign(this, { cdp, sid: sessionId, layout, dialogs: [], shots: 0 });
    cdp.handlers.push((m) => {
      if (m.sessionId === sessionId && m.method === 'Page.javascriptDialogOpening') {
        this.dialogs.push(m.params.message);
        cdp.send('Page.handleJavaScriptDialog', { accept: this.acceptDialogs !== false }, sessionId);
      }
    });
  }
  async ev(expr) {
    const r = await this.cdp.send('Runtime.evaluate',
      { expression: expr, awaitPromise: true, returnByValue: true }, this.sid);
    if (r.exceptionDetails) throw new Error(`page error: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}`);
    return r.result.value;
  }
  async waitFor(expr, what, ms = 8000) {
    const end = Date.now() + ms;
    while (Date.now() < end) { try { if (await this.ev(expr)) return; } catch { /* page reloading */ } await sleep(100); }
    throw new Error(`timed out waiting for ${what}`);
  }
  async goto(url) {
    await this.cdp.send('Page.navigate', { url }, this.sid);
    await this.waitFor(`document.readyState === 'complete' && !!document.querySelector('[data-console-revision]')`, `page ${url}`);
  }
  // The console remembers the last section per session, so land on Overview explicitly.
  async overview(url) { await this.goto(url); await this.section('overview'); }
  // Visible text of the console app, whitespace collapsed.
  text() { return this.ev(`document.body.innerText.replace(/\\s+/g, ' ')`); }
  // Click the first visible element matching `sel` (optionally whose text contains `has`).
  async click(sel, has = '') {
    const ok = await this.ev(`(() => {
      const el = [...document.querySelectorAll(${JSON.stringify(sel)})].find(e =>
        e.getClientRects().length && !e.disabled && e.textContent.includes(${JSON.stringify(has)}));
      if (!el) return false; el.scrollIntoView({block: 'center'}); el.click(); return true; })()`);
    if (!ok) throw new Error(`no visible enabled ${sel}${has ? ` containing "${has}"` : ''}`);
  }
  count(sel) { return this.ev(`[...document.querySelectorAll(${JSON.stringify(sel)})].filter(e => e.getClientRects().length).length`); }
  async section(key) { // desktop rail or phone tab
    await this.click(this.layout === 'phone' ? `[data-tab-go="${key}"]` : `.rail-i[data-go="${key}"]`);
    await sleep(500);
  }
  async scope(name) {
    await this.ev(`(() => { const s = [...document.querySelectorAll('[data-project-scope-switcher]')].find(e => e.getClientRects().length);
      s.value = ${JSON.stringify(name)}; s.dispatchEvent(new Event('change', {bubbles: true})); })()`);
    await this.waitFor(`document.querySelector('[data-console-revision]').getAttribute('data-project-scope') === ${JSON.stringify(name)}`, `scope ${name}`);
    await sleep(500);
  }
  async shot(step) {
    const { data } = await this.cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }, this.sid);
    writeFileSync(join(outDir, `${String(++this.shots).padStart(2, '0')}-${step}-${this.layout}.png`), Buffer.from(data, 'base64'));
  }
}

const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
// Headings are CSS-uppercased in innerText, so compare case-insensitively.
const has = (text, ...needles) => needles.forEach((n) => assert(text.toLowerCase().includes(n.toLowerCase()), `missing "${n}"`));
const lacks = (text, ...needles) => needles.forEach((n) => assert(!text.toLowerCase().includes(n.toLowerCase()), `unexpected "${n}"`));

async function runJourney(p) {
  const L = p.layout;
  const projectUrl = `${baseUrl}?project=${PROJECT}`;
  const steps = [];
  const step = (name, fn) => steps.push([name, fn]);

  step('a1-all-projects-page-offers-switcher', async () => {
    await p.goto(baseUrl);
    await p.shot('all-projects');
    assert(await p.count('[data-project-scope-switcher]') > 0, 'no project scope switcher on the all-projects page (mahler#1274)');
  });

  step('a2-scope-persists-and-scoped-views', async () => {
    await p.goto(projectUrl); // the all-projects page has no switcher yet (a1), so enter by URL
    await p.scope(OTHER);
    assert(new URL(await p.ev('location.href')).searchParams.get('project') === OTHER, 'URL did not follow the switcher');
    await p.scope(PROJECT);
    assert(new URL(await p.ev('location.href')).searchParams.get('project') === PROJECT, 'URL did not gain ?project=');
    await p.goto(projectUrl); // reload by link
    assert(await p.ev(`document.querySelector('[data-console-revision]').getAttribute('data-project-scope')`) === PROJECT, 'scope lost on reload');
    const sel = await p.ev(`[...document.querySelectorAll('[data-project-scope-switcher]')].find(e => e.getClientRects().length).value`);
    assert(sel === PROJECT, `switcher shows "${sel}"`);
    const leaks = ['Mahler item should not leak', 'Mahler question should not leak', 'Other project question?'];
    const wanted = { needs: ['Drive scope'], backlog: ['9 open'], releases: [], stats: [], history: [] };
    for (const [key, texts] of Object.entries(wanted)) {
      await p.section(key);
      const t = await p.text();
      has(t, ...texts); lacks(t, ...leaks);
      if (key === 'needs') await p.shot('scoped-needs-you');
      if (key === 'history') await p.shot('scoped-event-stream');
    }
    await p.section('overview');
  });

  step('b-overview-content', async () => {
    await p.overview(projectUrl);
    const t = await p.text();
    // six headline measures
    if (L === 'desktop') {
      assert(await p.count('.kpis.five .kpi') === 6, 'expected six headline measures');
      has(t, 'open', 'active', 'queued · next #33', 'waiting on you', 'blocked otherwise', 'done 7d');
    } else {
      has(t, 'open', 'active', 'queued', 'on you', 'blocked', 'done 7d');
    }
    // inline owner question with answer buttons
    has(t, 'Drive scope: drive.readonly or drive.file?');
    assert(await p.count('[data-act="answer"]') > 0, 'no inline answer button');
    // active / queued / blocked
    has(t, 'Retry batch', 'Calendar shared', 'Gmail attachments', 'Drive export', 'Chat paginate');
    // shipping: six stage dots per row (desktop table); explainer on both
    if (L === 'desktop') {
      const dots = await p.ev(`[...document.querySelectorAll('.pipe tr')].filter(r => r.querySelector('td')).map(r => r.querySelectorAll('.dot').length)`);
      assert(dots.length === 6 && dots.every((n) => n === 6), `stage dots per row: ${JSON.stringify(dots)}`);
      has(t, 'test_chat', 'agy-gemini', 'quiet 1/14d', 'verified');
    } else {
      has(t, '#44', '#43', '#38');
    }
    has(t, 'How a change ships', 'Dependencies', 'Completed · last 7 days', 'Activity',
        "What's new", 'Open issues · 30 days');
    assert(/v2\.4\.1/.test(t), 'current release version missing');
    lacks(t, 'Mahler item should not leak');
    await p.ev(`document.querySelectorAll('details').forEach(d => { if (/How a change ships/.test(d.textContent)) d.open = true; })`);
    await p.shot('overview');
    await p.ev(`window.scrollTo(0, document.body.scrollHeight)`);
    await sleep(300);
    await p.shot('overview-bottom');
  });

  step('c-prioritize-replace-stop', async () => {
    await p.overview(projectUrl);
    await p.click('[data-act="priority_boost_start"]', '1 hour');
    await p.waitFor(`document.body.innerText.includes('Priority boost active for ${PROJECT}')`, 'boost banner');
    await sleep(500);
    const labels = await p.ev(`[...document.querySelectorAll('[data-project-scope-switcher] option')].map(o => o.textContent)`);
    assert(labels.some((l) => l.startsWith(`▲ ${PROJECT}`)), `no ▲ in switcher: ${JSON.stringify(labels)}`);
    has(await p.text(), 'left', 'Extend 1 hour', 'Stop');
    await p.shot('prioritize-active');
    await p.scope(OTHER);
    await p.waitFor(`document.body.innerText.includes('Replace with 1 hour')`, 'replace controls');
    p.dialogs.length = 0;
    await p.click('[data-act="priority_boost_start"]', 'Replace with 1 hour');
    await p.waitFor(`document.body.innerText.includes('Priority boost active for ${OTHER}')`, 'replacement banner');
    assert(p.dialogs.length === 1 && p.dialogs[0].includes(`Replace the active priority boost for ${PROJECT}`),
      `expected one replacement confirmation, got ${JSON.stringify(p.dialogs)}`);
    await p.shot('prioritize-replaced');
    await p.click('[data-act="priority_boost_stop"]');
    await p.waitFor(`!document.body.innerText.includes('Priority boost active for')`, 'banner gone after Stop');
    await p.shot('prioritize-stopped');
    const kinds = eventKinds();
    for (const k of ['priority_boost_started', 'priority_boost_replaced', 'priority_boost_stopped'])
      assert(kinds.includes(k), `ledger lacks ${k}: ${JSON.stringify(kinds)}`);
    await p.scope(PROJECT);
  });

  step('d-parallel-jobs-stepper', async () => {
    await p.overview(projectUrl);
    const before = maxParallel(PROJECT), otherBefore = maxParallel(OTHER);
    assert(before === 2 && otherBefore === 1, `fixture max_parallel ${before}/${otherBefore}`);
    await p.click('[aria-label="More parallel jobs"]');
    await p.waitFor(`document.querySelector('[aria-label="Fewer parallel jobs"]')?.dataset.value === '2'`, 'stepper re-render at 3');
    assert(maxParallel(PROJECT) === 3, `max_parallel after + is ${maxParallel(PROJECT)}`);
    await p.shot('parallel-plus');
    await p.click('[aria-label="Fewer parallel jobs"]');
    await p.waitFor(`document.querySelector('[aria-label="More parallel jobs"]')?.dataset.value === '3'`, 'stepper re-render at 2');
    assert(maxParallel(PROJECT) === 2, `max_parallel after − is ${maxParallel(PROJECT)}`);
    assert(maxParallel(OTHER) === otherBefore, 'other project max_parallel changed');
    has(await p.text(), 'Mahler-wide: 1 of 8 in use');
    await p.shot('parallel-minus');
  });

  step('e-pause-resume-project', async () => {
    await p.overview(projectUrl);
    await p.click('[data-act="pause_project"]');
    await p.waitFor(`!![...document.querySelectorAll('[data-act="resume_project"]')].find(e => e.getClientRects().length)`, 'Resume project button');
    let t = await p.text();
    has(t, 'Retry batch'); // running item still listed as active
    const opts = await p.ev(`[...document.querySelectorAll('[data-project-scope-switcher] option')].map(o => o.textContent)`);
    assert(opts.some((o) => o.includes(`${PROJECT} · paused`)), `switcher lacks paused status: ${JSON.stringify(opts)}`);
    await p.shot('project-paused');
    await p.click('[data-act="resume_project"]');
    await p.waitFor(`!![...document.querySelectorAll('[data-act="pause_project"]')].find(e => e.getClientRects().length)`, 'Pause project button');
    has(await p.text(), 'Retry batch');
    assert(eventKinds().includes('project_pause') && eventKinds().includes('project_resume'), 'pause/resume events missing');
    await p.shot('project-resumed');
  });

  step('f-dark-mode-overview', async () => {
    await p.cdp.send('Emulation.setEmulatedMedia',
      { features: [{ name: 'prefers-color-scheme', value: 'dark' }] }, p.sid);
    await p.overview(projectUrl);
    assert(await p.ev(`matchMedia('(prefers-color-scheme: dark)').matches`), 'dark media not emulated');
    await p.shot('overview-dark');
    await p.cdp.send('Emulation.setEmulatedMedia', { features: [] }, p.sid);
  });

  let allOk = true;
  for (const [name, fn] of steps) { // a failed step is reported, then the rest still run
    try { await fn(); console.log(`STEP PASS ${L}: ${name}`); }
    catch (err) {
      allOk = false;
      console.log(`STEP FAIL ${L}: ${name}: ${err.message}`);
      await p.shot(`FAILED-${name.split('-')[0]}`).catch(() => {});
    }
  }
  return allOk;
}

const LAYOUTS = [
  ['desktop', { width: 1280, height: 900, mobile: false }],
  ['phone', { width: 390, height: 844, mobile: true }],
];

let chrome, ok = true;
try {
  chrome = await launchChrome();
  const cdp = await Cdp.open(chrome.wsUrl);
  for (const [layout, vp] of LAYOUTS) {
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    await cdp.send('Page.enable', {}, sessionId);
    await cdp.send('Runtime.enable', {}, sessionId);
    await cdp.send('Emulation.setDeviceMetricsOverride',
      { width: vp.width, height: vp.height, deviceScaleFactor: 1, mobile: vp.mobile }, sessionId);
    if (vp.mobile) await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true }, sessionId);
    const page = new Page(cdp, sessionId, layout);
    ok = (await runJourney(page)) && ok;
    await cdp.send('Target.closeTarget', { targetId });
  }
} catch (err) {
  console.log(`JOURNEY ERROR: ${err.message}`);
  ok = false;
} finally {
  if (chrome) { chrome.proc.kill(); try { rmSync(chrome.profile, { recursive: true, force: true, maxRetries: 5 }); } catch { /* Chrome still flushing */ }; }
}
console.log(ok ? 'JOURNEY PASS' : 'JOURNEY FAIL');
process.exit(ok ? 0 : 1);
