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
  verified fallback. `[api_credits] enabled` stays `false` until the steps below pass.

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

## Qualifying the harness (capped at $0.10 per trial, gated on Mike's go-ahead)

1. **Done (2026-10-10, trial 1):** confirmed the direct `anthropic` provider can't reach the
   gateway. Ruled out, see Status above.
2. **Next:** `uv tool install ./sdk_runner`, then re-run a synthetic trial through the
   gateway: a trivial `max_tokens`-capped prompt, checking the gateway's own ledger events
   (`credit_reserved`/`credit_settled`) for the attempt, and the Anthropic console to confirm
   which credit pool was debited.
3. Once that passes, an end-to-end run on an enabled work-account project, still capped at
   $0.10, confirms the whole path: routing, the gateway, settlement and promotional-credit
   attribution visible in Console billing.
4. Both trials count inside the $20 allowance. Once both are sanitized and recorded, flip
   `[api_credits] enabled = true` and add `work-claude-api` to a work project's routing.

## Raising the burst cap

The Mahler workspace's Console spend limit ($20 today) is a hard server-side ceiling —
Mahler's own accounting cannot spend past it regardless of the burst math. Raise it by hand
in Console only when ready to let the burst actually spend past the allowance near a grant's
expiry.
