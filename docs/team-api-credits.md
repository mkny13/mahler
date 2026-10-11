# Team-plan API credits (mahler#903, DESIGN D41)

Operator cheat-sheet for the work Team plan's pooled Claude API credits. The design
reasoning lives in [DESIGN.md](../DESIGN.md)'s D41; this page is just the setup steps and
current status.

## Status

- **Account question answered:** the linked Console org holds no purchased credits,
  auto-reload or invoiced overage ("Yes, credits only", 2026-10-09). Not re-asked.
- **Built and unit-tested:** the confirmed-grant model, the `api_credits.Gateway` request
  proxy (reserve-before-forward, model/token bounds, streaming usage, ambiguous-outcome and
  crash accounting), ledger reservations (transactional, idempotent, grant-scoped), key
  expiry notifications.
- **Harness: the Claude Agent SDK runner (mahler#918).** Live trial 1 (2026-10-10) ruled out
  Cline: its `anthropic` provider has no base-URL setting. `sdk_runner/` (`mahler-sdk-run`)
  reads `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`, which `runner.run_env` already sets to the
  gateway and the run's local token. Install it by hand: `uv tool install ./sdk_runner`.
  The gateway passes the SDK's `POST /v1/messages?beta=true` and its `anthropic-*` and
  `User-Agent` headers through. See `sdk_runner/FINDINGS.md` and DESIGN.md D41. Kilo is a
  verified fallback. Live trial 2 passed through the gateway (2026-10-10, $0.0035).
  `[api_credits] enabled` stays `false` until the end-to-end trial below passes.

## One-time setup (Mike, in Console — claude.ai Owner role)

1. Confirm the Team plan's API credits are linked to a Console organization, and that the
   org has no purchased credits, auto-reload or invoiced overage (already done; re-verify
   only if seats or billing change).
2. Create (or confirm) a Console workspace for Mahler, with its own spend limit — this is
   the server-side backstop behind Mahler's own accounting.
3. Create a workspace API key (`mahler-anthropic-api`) and, if using the Admin Cost API for
   the burst, an admin key (`mahler-anthropic-admin`). Both go in the mini's **login**
   keychain, account `work`:
   ```sh
   security add-generic-password -s mahler-anthropic-api   -a work -w
   security add-generic-password -s mahler-anthropic-admin -a work -w
   ```
4. Record each confirmed grant's dates in the live config as a `[[api_credits.grants]]`
   entry (see `config.example.toml`) as Console shows them. Don't invent a recurring
   schedule — an expired or not-yet-confirmed window simply refuses spending.
5. Set both keys' expiry dates in `[api_credits]` (`api_key_expires`, `admin_key_expires`)
   so Mahler can warn before they lapse.

## Qualifying the harness (each trial capped, gated on Mike's go-ahead)

1. **Done (2026-10-10, trial 1):** confirmed the direct `anthropic` provider can't reach the
   gateway. Ruled out, see Status above.
2. **Done (2026-10-10, trial 2): passed, $0.0035.** `mahler-sdk-run` installed on the mini
   with `uv tool install ./sdk_runner`. One no-tools Sonnet call through a real gateway
   (`python3.14 sdk_runner/bootstrap/trial-918.py`, keychain unlocked; `--fake-upstream
   host:port` does it for free) left two `credit_reserved`/`credit_settled` pairs in the
   gateway's ledger.
3. **Next (after #928 merges and the daemon self-updates): trial 3, end to end, on
   mahler#909, capped at $3.** A $0.10 cap can't work for a real run: every request carries
   Claude Code's ~76 KB of tool definitions plus a growing context, so the gateway's
   worst-case reservation is $0.20–0.80 per request and a $0.10 ceiling refuses the first
   one. The trial's live config:
   - `[api_credits]`: `enabled = true`, `account = "work"`, `allowance_usd = 3` (the trial
     cap; raise to 20 after), `run_reserve_usd = 1` (the default 5 exceeds a $3 allowance,
     so no run could start), and the confirmed grant (`granted_at = "2026-10-09"`,
     `expires_at = "2026-10-15T00:00:00Z"`, `pool_usd = 260`).
   - `[platforms.work-claude-api]`: `from = "claude-api"`, `account = "work"`,
     `enabled = true`. Not added to any routing list: #909 carries the
     `platform:work-claude-api` pin label, so it is the only item that can use it.

   It passes when #909's build run finishes through the gateway with its reservations
   settled, the run's priced cost and the ledger agree, and Console billing shows the spend
   against the promotional credits.
4. Then set `allowance_usd = 20` and `run_reserve_usd` back to its default, and add
   `work-claude-api` to work projects' routing. Until then the harness isn't in general use.

## Watching spend without the Console

- `mahler status` and the operator console show Mahler's credit spend this grant, the
  remaining headroom and when the grant expires, once `[api_credits]` is enabled.
- The third-party Claude Usage Tracker (macOS menu bar) can show Console API spend after
  signing in to the Console in its settings. Turn on its **API** menu-bar metric; its
  multi-profile layout shows only session/week. It may not break out promotional credits.

## Raising the burst cap

The Mahler workspace's Console spend limit ($20 today) is a hard server-side ceiling —
Mahler's own accounting cannot spend past it regardless of the burst math. Raise it by hand
in Console only when ready to let the burst actually spend past the allowance near a grant's
expiry.
