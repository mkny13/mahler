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
- **Live trial 1 ran 2026-10-10 (synthetic, $0.10 cap, zero actual spend) and ruled out the
  original harness plan.** Cline's direct `anthropic` provider cannot be redirected to the
  gateway: `cline auth -p anthropic -b <url>` refuses outright ("base URL is only supported
  for OpenAI and OpenAI-compatible providers"), and its provider-settings schema has no
  `baseUrl` field at all for `anthropic`. The `openai-compatible` provider does accept one.
  The next step is an OpenAI-chat-completions↔Anthropic-Messages translation layer in the
  gateway, wired through `cline auth --data-dir <per-run dir>` (not env vars — Cline reads
  provider settings from `settings/providers.json`, not `ANTHROPIC_BASE_URL`). See DESIGN.md
  D41 for the full writeup. `[api_credits] enabled` stays `false` until that's built and
  passes its own live trial.

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
2. **Next:** build the `openai-compatible` translation layer in the gateway, switch
   `work-claude-api`'s provider and the runner's wiring (`cline auth --data-dir`, not env
   vars), then re-run a synthetic trial the same way: a trivial `max_tokens`-capped prompt,
   checking the gateway's own ledger events (`credit_reserved`/`credit_settled`) for the
   attempt, not just Cline's own output.
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
