<!-- The Phase 2 console design, as handed off from Claude Design on 2026-09-15.
     Everything above "Implementation (D27)" is the designer's README, verbatim:
     its copy is final. The prototype HTML and its runtime are not in the repo;
     screens/ holds JPEG copies of the reference screenshots. -->

# Handoff: Mahler console (Phase 2 UI)

## Overview

The operator console for Mahler, the autonomous conductor that runs coding agents
(Claude Code, Antigravity `agy`, Cline, Copilot, Kilo) through each project's
GitHub-issue backlog. Two form factors, one state model:

- **Phone** — triage from a Pixel in under a minute: what needs a decision, what
  is running, what is waiting for UAT, and fast capture into the backlog.
- **Desktop** — a single pane of glass: a left rail of views, a dense main
  column, and a right sidebar of standing status.

Served over Tailscale by `mahler serve` (ROADMAP Phase 2 / Phase 3 item 1). It
replaces today's read-only status page (`mahler/serve.py`) — that page's content
is preserved as the desktop Event stream + Backlog views.

## About the design files

`Mahler Console v2.dc.html` is a **design reference created in HTML**. It is a
prototype of look and behavior, not production code to copy. It runs on a
bespoke streaming-template runtime (`support.js`) that has nothing to do with
Mahler's stack, and all its data is mock data inlined in the file.

The task is to **recreate these designs in the target environment**. Mahler is
Python + stdlib `http.server` today with no frontend build step, so the natural
implementation is server-rendered HTML from `mahler/serve.py` plus a small amount
of vanilla JS — no framework, no bundler, matching how `serve.py` already works.
If you would rather introduce a framework, that is a codebase decision; the
design does not require one.

Open the file in a browser to see it live. It is a canvas of turns — turn 3
(top) is the wired desktop console, turn 2 below it holds the working phone
console (`2a`) and two desktop alternatives that were **not** chosen (`2c` is
the ancestor of `3a`; `2b` and `2d` are rejected). **Build `3a` and `2a` only.**

## Fidelity

**High-fidelity.** Colors, type sizes, spacing, states and copy are all final and
intended to be matched. Two caveats:

- The **light theme is the reference**; dark is a token swap of the same layout.
- All numbers, issue titles, log lines and timestamps are **plausible mock data**
  taken from the real ledger/router/config shapes. Do not ship them as strings —
  they are there to show density and formatting.

The design deliberately uses **no design system**. Broadsheet is bound to the
design project but was ruled out for this console by the user: they asked for a
minimally styled, maximally readable, neutral light/dark utility. Do not
reintroduce serif type or the cyan/magenta accents.

---

## Design tokens

Two themes as CSS custom properties on a wrapper class. Light is the reference.

| Token | Light | Dark | Used for |
| --- | --- | --- | --- |
| `--bg` | `#ffffff` | `#0f1113` | page ground |
| `--surf` | `#f4f5f6` | `#181b1e` | left rail, log panel |
| `--ink` | `#0d1013` | `#f2f4f6` | body text |
| `--mut` | `#4a5158` | `#a7aeb5` | secondary text, metadata |
| `--line` | `#d6dadd` | `#2b3135` | 1px rules, gauge tracks, button borders |
| `--acc` | `#1256c4` | `#7cb0ff` | running state, links, primary actions |
| `--accq` | `#e8f0fd` | `#16243a` | accent-quiet fill (Pass button, chips) |
| `--warn` | `#8f4a06` | `#e59a5c` | paused, past estimate, over soft limit |
| `--warnq` | `#fcf0e6` | `#2b1d14` | warning banner ground |
| `--bad` | `#a8180f` | `#f4918a` | needs-you, p1, hard limit, Fail |
| `--badq` | `#fdeceb` | `#2e1a18` | needs-you banner ground |
| `--good` | `#10602c` | `#62d68f` | confirmations (passed, saved) |

Contrast: `--ink` on `--bg` ≈ 18:1, `--mut` on `--bg` ≈ 7:1, and every accent
is a deep step so 13px text on it still clears 4.5:1. **The earlier gold
(`#8a5b00` / `#e0b155`) was rejected as unreadable — do not reinstate it.**

**Theme resolution.** Three states: `auto` (default) follows
`prefers-color-scheme` live via a `matchMedia` change listener; `light` and
`dark` override. The header/rail button cycles auto → light → dark → auto and
labels itself with the current state.

### Type

System UI stack (`system-ui, -apple-system, "Segoe UI", sans-serif`) for
everything except numbers and identifiers, which use
`ui-monospace, SFMono-Regular, Menlo, monospace` with
`font-variant-numeric: tabular-nums`. Monospace carries issue refs
(`mahler#41`), platform names, percentages, timestamps, state chips, priority
chips and log lines — anything you scan or compare rather than read.

| Role | Size / weight / line-height |
| --- | --- |
| Brand "Mahler" | 16–17px / 600 / 1 |
| View title (desktop header) | 15px / 600 / 1 |
| Section label (`ACTIVE RUNS · 2`) | 10.5px mono / 400 / 1, `letter-spacing: .1em`, uppercase, `--mut` |
| Needs-you question | 14.5px (desktop) / 15px (phone) / 400 / 1.45 |
| Body + table cell | 13px / 400 / 1.45 |
| Metadata line | 11px mono / 400, `--mut` |
| Run timing, percentages | 11–11.5px mono, tinted by state |
| Log line | 11.5px mono / 1.5 |
| Button label | 12.5–14px / 600 (primary) or 400 (secondary) |

### Spacing, radius, borders

- Radius: `4px` on buttons, inputs, cards and gauges; `6px` on desktop dialogs;
  `20px` (pill) on chips; `18px` on the phone frame (prototype only).
- Section gap 14–18px; card padding 11–14px; page padding 14px (phone) /
  16–20px (desktop).
- **No shadows and no filled cards in the content area.** Structure is 1px
  `--line` rules and whitespace. The only filled surfaces are the left rail
  (`--surf`), the log panel (`--surf`), banner grounds (`--warnq` / `--badq`)
  and primary buttons.
- Left-edge accent: a 3px solid border-left in `--bad` marks a needs-you item
  or banner; `--warn` marks a warning banner.

### Touch targets

Every phone control is `min-height: 44px` (`48px` on sheet confirm buttons).
Desktop drops to `min-height: 32–38px`. Filter chips and inline Undo buttons are
`30px`. Do not go below these.

---

## Shared state model

Both form factors read one state object. Field names below are the prototype's;
rename freely, but the transitions matter.

| State | Type | Meaning |
| --- | --- | --- |
| `theme` | `"auto" \| "light" \| "dark"` | persist across sessions |
| `paused` | bool | Pause All. Drives banner + header state label |
| `peakOverride` | bool | true = Claude may build during peak hours |
| `tab` | `"now" \| "triage" \| "browse"` | phone view |
| `dtab` | `"now" \| "needs" \| "test" \| "capture" \| "backlog" \| "history"` | desktop view |
| `answers` | `{ [itemId]: string }` | needs-you answer, optimistic, undoable |
| `drafts` | `{ [itemId]: string }` | typed reply in progress |
| `uat` | `{ [itemId]: "pass" \| "fail" }` | UAT verdict |
| `capture` / `project` / `attached` | string / string\|null / bool | capture composer |
| `open` | `{ [project]: bool }` | backlog group expansion |
| `digestOpen` / `digestSeen` | bool | "3 new" filter chip |
| `bannersOpen` | bool | expanded banner stack |
| `runId` / `bugId` / `undoId` | id \| null | which overlay is open |

Every mutation in the prototype is local and optimistic. In the real console each
one is a POST that writes a ledger event; the UI should apply optimistically,
then reconcile on the next poll. Poll interval: today's page refreshes every 30s;
keep that, or use SSE if `serve.py` grows it.

### Derived values

- **System state** — `paused` → `PAUSED`; else `runs.length` → `RUNNING · N`;
  else `IDLE`. Colors `--warn` / `--acc` / `--mut`.
- **Run progress** — `min(100, round(elapsed / estimate * 100))%`. Under
  estimate: `--acc`, label `"18m of ~26m"`. Over: `--warn`, label
  `"34m · 12m past estimate"`.
- **Quota bar** — worst window fills the bar; a 1px `--ink` tick at 55% opacity
  marks the soft line. Hard/backed-off platforms fill 100% in `--bad` and label
  `off`; unmetered platforms fill 0% and label `unmetered`.
- **Badge counts** — Triage badge = needs-you items with no answer yet. Rail
  "Ready to test" = items with no verdict. Rail "Event stream" = `3 new` until
  `digestSeen`.

---

## Screens — phone (`2a`)

390 × 772 reference frame (Pixel-class). Fluid: single column, everything
`max-width`-free, one vertical scroll container between a fixed header and the
overlays.

### Header (fixed, both tabs)

Two rows inside a `1px --line` bottom border:

1. Brand `Mahler` + state label (`RUNNING · 2`, 10px mono, tinted) on the left;
   on the right a theme button (44×36, bordered, label `Auto`/`Light`/`Dark`) and
   a **Pause all / Resume** button (36px tall, 12px horizontal padding,
   `white-space: nowrap`, `flex: none`). Paused state inverts it to a filled
   `--acc` button reading `Resume`.
2. A three-up tab row, each tab `flex: 1`, 46px tall, `border-bottom: 2px`
   (`--ink` when active, transparent otherwise), label 15px (600 active / 400
   inactive), with a small mono count beside it: `Now` + run count in `--acc`,
   `Triage` + needs-you count in `--bad`, `Browse` (no count).

**Landing tab:** open on `Triage` if anything is in needs-you or awaiting UAT,
otherwise `Now`.

### Now

The health view. Sections top to bottom:

1. **Peak-hours line** — one bordered, tappable 44px row. Label (12.5px,
   `--mut`): *"Claude peak hours 05:00–11:00 PT — planning only, free tiers
   build"*; right side action word `Override` in `--warn`. Overridden state
   reads *"Peak hours overridden — Claude may build until you switch back"* with
   `Restore` in `--acc`. Tapping toggles. **Semantics: during peak hours free
   tiers keep building and only Claude is held back.** This is not in the
   codebase yet — see Open questions.
2. **Section header** — `ACTIVE RUNS · N` with, beside it, a pill chip reading
   `3 new` (6px `--acc` dot + 10.5px mono label) when there is an unseen digest.
   Tapping expands the digest **below the header, above the runs**: three
   `HH:MM — event text` lines and a `Mark all seen` link that clears the chip
   permanently. A right-aligned link toggles the mock 0-runs scenario (prototype
   affordance only — do not ship).
3. **Run cards** — bordered, tappable, 44px min. Issue ref (12.5px mono) left,
   platform badge right (10px mono, bordered, `--mut`); title 14px; a 3px
   progress bar; then timing (tinted) left and live status right
   (`pushing commits`, `no tool call 6m`). Tap → full-screen run detail.
4. **0-runs explanation** (replaces the run list when nothing is running) —
   a bordered block headed *"Nothing is running. Four things are holding it:"*,
   then one row per binding reason, each a plain-English sentence, a countdown in
   mono `--mut`, and where an escape exists an outlined `--acc` button:
   - *"Claude is in your peak hours — it plans but doesn't build until 11:00 PT.
     Free tiers are unaffected."* · `42m left` · **Let Claude build**
   - *"agy-claude and agy-gemini are both past 85% on the weekly window, so the
     scheduler won't start there."* · `weekly resets Mon 00:00` · no override
   - *"kilo and cline-free are backing off after quota errors."* ·
     `kilo 18m · cline-free 47m` · **Clear backoff**
   - *"mahler#39 holds the project's only parallel slot until its PR merges — CI
     has been pending 6 minutes."* · `verify timeout in 54m` · **Open PR #112**

   Verbosity is variable by design: list every reason that is actually binding,
   with a countdown when one exists and an override only when the user can
   genuinely act.
5. **Capacity** — `CAPACITY` label plus one sentence, e.g. *"3 of 6 platforms
   available — agy-claude, agy-gemini, copilot. claude is held by peak hours
   until 11:00; kilo is backing off until 11:12."* Then `All quota gauges →`
   linking to Browse.

### Triage

1. **Banners** (see Alerts) — at most one expanded, the rest behind a summary
   line. Triage only; never on Now.
2. **`NEEDS YOU · N`** — one card per item: 3px left border (`--bad` for p1,
   `--line` otherwise), a meta row (ref · priority chip · `waiting 42m`), the
   question at 15px/1.45, then two answer buttons side by side (first filled
   `--acc`, second outlined) at 44px, and below them a 44px text input
   (*"or say something…"*) with a 44×44 send button. Answering collapses the
   card to `You said: <answer>` + an `Undo` link.
3. **`READY TO TEST · N`** — per item: ref + meta, title, a one-line *what to
   check* in `--mut`, a staging link, then **Pass** (filled `--accq`, `--acc`
   text and border) and **Fail** (outlined `--bad`) at 44px. Pass →
   *"Passed — issue closed, UAT recorded."* Fail → bug sheet, then
   *"Failed — p1 bug filed and routed. The revert is one tap away in History."*
4. **`CAPTURE`** — 3-row textarea (*"Type or dictate. Goes to the inbox — no
   project needed."*), a wrapping row of pill project chips
   (`inbox (no project)` selected by default, then `mahler`, `groundwork`,
   `couch-tour`), a dashed 44px attach button that toggles to
   `screenshot-2026-09-14.png ✓`, and a filled 44px **Save**. Saving clears the
   composer, prepends the item to that project's backlog group, auto-expands it,
   and shows *"Saved to mkny13/&lt;project&gt; as a new issue · sorting run queued. It
   settles 10 minutes before anything picks it up."*

### Browse

Order: **quota → backlog → history**.

1. **`QUOTA · WORST WINDOW`** — per platform: name (12.5px mono) + model
   (10.5px mono `--mut`, ellipsised) on the left, state label right; a 12px bar
   with the soft-line tick; then a detail line (*"5h 63% · weekly 44% · soft line
   60%"*). Footnote: *"Tick marks the soft line — Mahler stops starting runs
   there. Hard line yields work in flight."*
2. **`BACKLOG`** — one collapsible group per project. Header row (44px) with the
   project name and `N open · show|hide`; expanded rows are priority chip ·
   title · state chip, separated by 1px rules.
3. **`HISTORY`** — `HH:MM` · `event_kind` · detail, one row per event. Merge
   events carry a 36px `Undo` button that opens the revert confirm sheet.

### Overlays (phone)

- **Run detail** (full screen) — back link `← Now` + ref; title, then a mono
  meta line (`agy-claude · run 118 · worktree 41-118 · epoch 3 · lease held`);
  progress bar + timing; `LIVE LOG` panel on `--surf` with newest-first mono
  lines tinted by kind (tool calls and heartbeats `--mut`, commits `--ink`,
  passing verify `--good`, stalls `--warn`); footer **Leave running** (outlined)
  and **Stop & hand off** (outlined `--bad`), both 48px.
- **Bug sheet** (bottom sheet over a `rgba(10,12,14,.5)` scrim) — *"What went
  wrong?"*, mono meta line for the failed item, a 3-row textarea
  (*"One line is enough — it opens a p1 bug with the build SHA and your note."*),
  the dashed attach button, then **Cancel** / **File p1 bug** (filled `--bad`).
- **Undo sheet** — *"Revert &lt;event detail&gt;?"* + *"This opens a revert PR, waits
  for CI, merges it and redeploys. The branch is kept for 14 days."*, then
  **Keep it** / **Open revert PR**. Undo is always confirm-then-act; never an
  act-then-undo toast.

---

## Screens — desktop (`3a`)

`grid-template-columns: 190px 1fr 306px`, min-height 660px, reference width
1200px. The main and right columns each own a 1px `--line` edge. Below ~1100px,
collapse the right column into the bottom of the main column; below ~760px fall
back to the phone layout.

### Left rail (`--surf`)

Brand, then six full-width view buttons (40px, `border-left: 2px` — `--acc` when
active, transparent otherwise; active row also flips to `--bg` and 600 weight),
each with a right-aligned mono count:

`Now` (run count, `--acc`) · `Needs you` (open count, `--bad`) ·
`Ready to test` (count) · `Capture` · `Backlog` (total items) ·
`Event stream` (`3 new` until seen).

**The event stream is a rail item on purpose.** The user does not want a live
feed in their peripheral vision — it must be one click away and otherwise
invisible. Do not put it back in the sidebar.

Pinned at the rail's bottom: a `Theme · Auto` button.

### Main column

Header row: current view title, and right-aligned a bordered peak-hours button
(*"Peak hours until 11:00 · Claude plans only"* + `Override`).

- **Now** — peak-hours sentence, then `ACTIVE RUNS · N` as dense rows
  (`grid-template-columns: 124px 1fr 100px 116px 90px` — ref, title, platform,
  timing, mini bar), 1px rules, whole row clickable. 0-runs state is the same
  reason block, laid out horizontally: sentence, countdown, override button.
  Capacity sentence below a rule.
- **Needs you** — banners first, then one row per item: question + meta (+ a
  34px inline reply input) on the left, answer buttons right-aligned, `Undo`
  after answering.
- **Ready to test** — title, ref/meta, what-to-check, staging link on the left;
  **Pass** / **Fail** right-aligned at 36px.
- **Capture** — same composer at `max-width: 560px`, plus an explicit target
  line: *"Opens a GitHub issue in mkny13/&lt;project&gt; · labels type:feature, p2,
  mahler:inbox"* (reads *"the inbox repo"* with no project selected).
- **Backlog** — collapsible groups; rows are
  `grid-template-columns: 34px 1fr 84px` (priority, title, state).
- **Event stream** — rows are `grid-template-columns: 46px 108px 1fr 74px`
  (time, event kind, detail, Undo).

### Right sidebar (standing status, same on every view)

1. State label + **Pause all / Resume** on one row.
2. **Needs-you card** — the single oldest open item: `NEEDS YOU · N` label, the
   question, ref + wait time, its answer buttons, and `All N →`. Hidden when the
   queue is empty.
3. **`BACKLOG`** — one row per project: name + `14 · 5 ready · 2 live · 1 you`.
   Clicking jumps to that project, expanded, in the Backlog view.
4. **`CAPACITY`** — the compact quota gauges (76px name column, 9px bar, 52px
   label) with the model name on a second line, then `Event stream →`.

There is deliberately **no quick-capture box here** — Capture is one rail click
away, and a second composer was redundant.

Desktop overlays are the same bug and undo flows as centered 430px dialogs
(6px radius, `rgba(10,12,14,.45)` scrim, right-aligned actions).

---

## Alerts & notifications

None of this exists in the codebase yet; it was designed from the user's
priorities. Three mechanisms:

**1. Persistent banners** for blocked states, in Triage / Needs you only, never
on Now. Full-width, `--warnq` or `--badq` ground, 3px left border, a mono
uppercase kind label, one plain-English sentence, and an action button when
there is a way out. **At most one is expanded** — the most severe — with the
rest collapsed behind a tappable summary line (*"1 more notice · run sat
silent"*) so runs stay above the fold.

Severity order and copy:

1. `PAUSED BY YOU` (`--warn`) — *"Nothing new will start. Runs in flight finish
   at their next checkpoint. Quota probes and GitHub sync keep going."* ·
   **Resume all**
2. `ALL PLATFORMS OVER SOFT LINE` (`--warn`) — *"Every builder is at or past its
   soft line, so the scheduler is holding. It starts again on its own as windows
   roll over."*
3. `HOT HOLD · <project>` (`--warn`) — *"You have uncommitted edits in
   groundwork from 4 minutes ago. No new runs start there until 20 minutes after
   you stop. Work in flight continues."*
4. `RUN SAT SILENT · <platform>` (`--bad`) — *"Run 124 printed nothing for 10
   minutes and was stopped. Check the Mac mini for a macOS permission dialog —
   it blocks the agent with no output."* · **How to fix**

**2. Unread digest** — a small `3 new` filter chip beside the `ACTIVE RUNS`
label, expanding in place to the events since the last look, with `Mark all
seen`. It is a chip, not a banner: an earlier card-style digest was rejected for
interrupting the flow.

**3. Severity tiers** — needs-you events are full-ink `--ink`; FYI events are
`--mut`. The same split governs push: needs-you pushes, FYI accumulates into the
digest. Existing ntfy delivery is the transport; tapping a needs-you push should
deep-link to that item in Triage / Needs you.

---

## Behavior notes

- **Pause all** is a single toggle, immediate, no confirm. It shows its banner
  and flips the state label; it does not kill running work.
- **Peak-hours override** is a toggle with no confirm, and it is reversible from
  the same control.
- **Needs-you answers** apply optimistically with an inline `Undo`; the typed
  reply and the buttons write the same field.
- **UAT fail** always goes through the bug sheet — never a bare fail.
- **Undo a merge** always confirms first.
- **Capture** never blocks on project selection; unassigned captures create the
  `inbox` group.
- Transitions: none beyond default; this is a utility console, not an
  experience. Keep any state change instant.

## Backend surface this implies

Read (all already in the ledger / `mahler status`): active runs with elapsed vs
estimate and last status; items by state per project with priority; quota per
platform per window with soft/hard thresholds, reset times and backoff; recent
events. Plus two things that are **not** in the ledger today: a per-run live log
tail, and a structured "why is nothing running" answer (the scheduler knows all
four reasons; it needs to expose them).

Write: pause/resume; peak-hours override; answer a needs-you item (comment +
state transition); UAT pass (close) / fail (file p1 bug, link, route); create an
issue from capture with labels `type:*`, `p2`, `mahler:inbox`; stop-and-hand-off
a run; open a revert PR; clear a platform backoff; mark digest seen.

## Data shown (all mock)

Platforms, models and thresholds are current as of the last rescan of
`mahler/config.py`:

| Platform | Model | Windows | Soft / hard |
| --- | --- | --- | --- |
| `claude` | `sonnet` | 5h, weekly | 60/70 · 70/80 |
| `agy-claude` | `claude-opus-4-6-thinking` | 5h, weekly | 85/85 · 90/90 |
| `agy-gemini` | `gemini-3.1-pro-high` | 5h, weekly | 85/85 · 90/90 |
| `cline-free` | free tier, size s only | unmetered | backoff 60m |
| `copilot` | `claude-sonnet-5` | monthly (1500 AI credits) | 80 · 95 |
| `kilo` | `kilo-auto/free`, size s only | unmetered | backoff 60m |

Mock runs: `mahler#41` on agy-claude (18m of ~26m), `groundwork#83` on
cline-free (34m, 12m over). Mock needs-you: `couch-tour#9` (API key, p1),
`groundwork#81` (date format, p2), `mahler#36` (watchdog test, p1, 3 attempts).
Mock UAT: `mahler#39`, `groundwork#87`. Projects: mahler, groundwork,
couch-tour.

## Assets

None. No images, no icon font, no SVG. The few glyphs are text characters
(`←`, `↑`, `▴`, `▾`, `↗`, `→`, `·`). If you add icons, the design has no
opinion — but it needs none.

## Open questions for the implementer

1. **Claude peak hours (5–11am PT) do not exist in the codebase.** The design
   assumes: Claude is held back from building, free tiers keep building, and a
   manual override releases Claude until toggled back. `ROADMAP` has
   *"peak-hours reserve for interactive Claude"* as a backlog item — build that
   first, or the header control has nothing to toggle.
2. **Live log tail** — no endpoint exists. Either tail the run's `log_path` or
   drop the log panel and keep the run detail to metadata + Stop.
3. **Structured idle reasons** — the scheduler decides these already but does not
   record them. Without them the 0-runs view degrades to one generic sentence,
   which is the single most valuable thing in the design.
4. **Not surfaced anywhere:** routing preference order (`sort` vs `build` lists),
   and why Claude's thresholds are tighter than everything else (60/70 vs 85/85).
   Both are visible in `config.py` and invisible in the console.

## Files in this bundle

- `HANDOFF_PROMPT.md` — paste into Claude Code to start the build.
- `Mahler Console v2.dc.html` — the design reference. Build **turn 3 / `3a`**
  (desktop) and **turn 2 / `2a`** (phone). `2b`, `2c` and `2d` are rejected
  desktop alternatives kept for context only.
- `support.js` — the prototype runtime. Required to open the HTML; **not** part
  of the implementation.
- `screens/` — reference screenshots, light theme at 2x:
  - `desktop-01-needs-you.jpg` — default landing view, banner + needs-you rows
  - `desktop-02-now.jpg` — active runs, dense rows
  - `desktop-03-now-zero-runs.jpg` — the idle explanation with countdowns and
    overrides
  - `desktop-04-ready-to-test.jpg` — UAT queue
  - `desktop-05-capture.jpg` — composer with the GitHub issue target line
  - `desktop-06-backlog.jpg` — collapsible project groups
  - `desktop-07-event-stream.jpg` — history with inline Undo
  - `phone-01-triage.jpg` — banner, needs-you cards, UAT, capture
  - `phone-02-now.jpg` — peak-hours line, digest chip, run cards
  - `phone-03-now-zero-runs.jpg` — the idle explanation on a phone
  - `phone-04-run-detail.jpg` — full-screen live log + Stop
  - `phone-05-browse-quota.jpg` — quota gauges with models and soft-line ticks

---

## Implementation (D27)

How the design maps onto Mahler. The decision and its reasons are DESIGN.md D27.
The code is `mahler/console/` (state, page, actions, CSS, JS), served by
`mahler/serve.py`.

### Built in the first change

- Both layouts (`3a` desktop, `2a` phone) in one server-rendered document, both
  themes, the 1100px and 760px breakpoints, the 30-second refresh.
- Now (runs, the 0-runs explanation, capacity), Needs you (read-only), Backlog,
  Event stream, quota gauges, the right sidebar, banners, the unread-digest chip,
  the run detail (metadata and progress; no log panel yet).
- Writes that only touch the ledger: Pause all / Resume, the peak-hours override,
  Clear backoff, and marking the digest seen.

### Filed as issues (label `area:console`)

Each is a planned issue for Mahler to build; the `area:` label makes them land one
at a time.

- The tick-applied write queue, and answering a needs-you item (buttons, reply box,
  Undo within 60 seconds).
- Structured answer options on NEEDS-YOU (so the two answer buttons exist).
- The scheduler recording why nothing started (structured holds), so the 0-runs
  view is complete rather than inferred.
- Ready to test: the UAT queue, Pass, and Fail through the bug sheet.
- Capture to a GitHub issue in the project you pick, then attachments for capture
  and the bug sheet.
- Stop & hand off from the run detail, and the live log tail.
- Undo a merge: the revert PR through the normal pipeline.
- The serve process restarting itself when its code updates, and ntfy
  needs-you pings deep-linking to the item.

### Changed from the design (owner, 2026-09-15)

These override the verbatim spec above.

- **Hot hold wording.** The designed banner said "You have uncommitted edits… No
  new runs start there", but Mahler detects an active Claude Code session, and a
  hot hold only holds builds. It now reads: *"You were working
  in groundwork with Claude Code 4 minutes ago. No new builds start there until 20
  minutes after you stop. Work in flight continues."*
- **No inbox.** Capture always files into a project you pick. The `inbox (no
  project)` chip, "the inbox repo" and the inbox backlog group are dropped; no project is preselected, and Save waits until you pick one. The
  placeholder becomes *"Type or dictate, then pick a project."*

### Judgment calls the design didn't settle

- **Peak override.** From the console it holds until you switch it back, as the
  copy says. `mahler peak off` keeps its timed default. The desktop header button
  only appears inside the window or while an override is live; the Now line
  always shows the schedule.
- **Digest on desktop.** Opening the Event stream marks it seen (the desktop has
  no Mark all seen link).
- **Run detail on desktop** opens as a centred dialog, like the other desktop
  overlays but 560px wide.
- **Gauges** are one per quota group (claude and claude-opus share one). The name
  column is 88px, not 76px: real names like `copilot-work` are longer than the
  mock's. The model line carries the plan (`plan` in the platform config, e.g.
  `free tier`, `business plan`), or the account for a second login.
- **Event stream** leaves out lease and stats bookkeeping, and the state changes
  that repeat a `run_started`, `pr_opened` or `pr_merged` row.
- **Until their issues land**, Capture is not in the rail, Ready to test shows 0,
  and a needs-you item links to its GitHub issue, where a reply already counts as
  the answer.

### Copy added during implementation

Sentences the design had no text for. Same voice, and reviewable here in one
place:

- Idle reasons: *"You paused everything, so nothing new starts."* (Resume all) ·
  *"claude and claude-work have no fresh quota reading, and Mahler counts unknown
  as over the line."* · *"kilo is on hold after a run never started."* (Clear
  backoff) · *"groundwork has max_parallel set to 0, so its 3 waiting item(s)
  stay put."* · *"You have been working in mahler, so new builds there wait until
  20 minutes after you stop."* · *"Nothing is waiting to start — finished changes
  are waiting on CI."* · headline *"Nothing is running."* when there is no list.
  Kept from the old status page: *"The backlog is empty — nothing to work on."*
  and *"3 item(s) queued, but no platform has headroom for them right now."*
- Quota details: *"no fresh reading — counted as over the line"* (label
  `stale`), *"on hold until 11:12 — a run never started"* (label `hold`),
  *"quota error — backing off until 11:12"*.
- Capacity line phrases: *"is past its soft line"*, *"has no fresh quota
  reading"*, *"is on hold until 11:12"*.
- Hot hold banner: *"under a minute ago"* when it is under a minute.
- Event kinds shown: `run_started`, `pr_opened`, `pr_merged`, `escalated`
  (*"retried a tier up (2 → 3)"*), `handoff`, `backoff_cleared` (*"cleared by hand
  on kilo"*), and the state an item moved to (`needs_you`, `ready`, …).

- Scheduler holds (fresh for three minutes): *"{n} item(s) need a builder that
  takes size:{size}, and none in the route does."* · *"{n} {role} item(s) have
  no platform with headroom — {summary}."* Summary groups use `busy`, `past the
  line`, `peak hours`, `too small`, `below required tier`, `no fresh reading`,
  `wrong account`, and `unavailable`; an empty route says `no platforms in the route`.
- *"{n} item(s) were just sorted and settle for {settle_minutes} minutes before
  a build starts."* · countdown `first in {m}m`.
- *"{ref} waits for {refs} to close."* With more than three: *"{n} items wait
  for other issues to close."*
- *"{ref} waits — {area or files} already in progress."* · *"{project} is at
  its limit of {max_parallel} run(s)."* · *"{project} waits — its canonical
  lease host is unavailable."*
- The generic queued/no-headroom sentence is only a fallback when there is no
  fresh scheduler snapshot. Existing quota, peak, backoff, slot and hot-hold
  explanations keep their countdowns and actions. Dry runs print decisions and
  collect holds without replacing the live snapshot.
