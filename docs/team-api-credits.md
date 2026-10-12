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
  Live trial 3 passed (2026-10-11, below); `[api_credits] enabled = true` on the mini.

## One-time setup (Mike, in Console — claude.ai Owner role)

1. Confirm the Team plan's API credits are linked to a Console organization, and that the
   org has no purchased credits, auto-reload or invoiced overage (already done; re-verify
   only if seats or billing change).
2. Create (or confirm) a Console workspace for Mahler, with its own spend limit. Keep the
   limit as a belt-and-braces setting only: its effect on credit-covered spend is unverified
   (see "Raising the burst cap"). The enforced bounds are Mahler's own allowance and
   per-request reservation ceiling, the credit balance, and the absence of any payment method
   or purchased balance.
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
2. **Done (2026-10-10, trial 2): passed, $0.0035.** `mahler-sdk-run` installed on the mini
   with `uv tool install ./sdk_runner`. One no-tools Sonnet call through a real gateway
   (`python3.14 sdk_runner/bootstrap/trial-918.py`, keychain unlocked; `--fake-upstream
   host:port` does it for free) left two `credit_reserved`/`credit_settled` pairs in the
   gateway's ledger.
3. **Failed (2026-10-11, trial 3, $0.136):** the gateway was a thread in the one-shot tick,
   so it died seconds after the run started (mahler#951). Fixed by running it as its own
   detached process; see D41. Needs a re-run.
4. **Passed (2026-10-11, trial 3 re-run, mahler#930, run 11801):** exit DONE through the
   gateway, 13 reservations settled, PR merged. Console billing: $1.34 spent, $0.00 purchased
   balance, no payment method, $258.65 of $260 promotional credit left. The Console token
   export priced with D41's table is $1.343. The gateway's settled amounts run about 2% under
   real cost and the SDK's own `total_cost_usd` about 15% over; the allowance counts the
   larger, so it errs toward spending less.
5. **Live now:** `allowance_usd = 20`, `work-claude-api` enabled, in no routing list. Only an
   item labelled `platform:work-claude-api` can use it. Adding it to a work project's routing
   is Mike's call.

## Raising the burst cap

The Mahler workspace's Console spend limit ($20 today) is **not** a documented hard ceiling
for credits spend. On 2026-10-11 the workspace's billing page showed $0.00 of $20.00 while the
Admin Cost API attributed $1.031 of the org's usage to that workspace (and org Billing showed
$1.34 spent), so the meter does not count credit-covered spend. Whether the limit would stop
credit-covered traffic is unverified, and we will not spend past it to find out.

What actually bounds spend: Mahler's own allowance and per-request reservation ceiling (the
gateway's atomic check), the credit balance, and the absence of any payment method or purchased
balance (Console 2026-10-11: $0.00 purchased, no payment method). The workspace limit stays set
as a belt-and-braces measure.

The burst runs on the org-level Admin Cost API reading (`pool − org spend − margin`), so it
does not need the workspace limit raised. `workspace_id` only scopes the Cost API's attribution
of Mahler's own spend; it is not proven enforcement. The Cost API lags by about a day.


## Work discussions (#926)

`mahler.discuss` routes a single-account personal project to the personal Claude
subscription, and a single-account work project to `mahler.discuss_credits` and the
Agent SDK. Mixed/ambiguous or disabled project policies fail closed. Exactly one
enabled `agent-sdk` platform must match the work account and its credit pool;
there is no subscription or direct-provider fallback. Build routing is unchanged.

Qualification is complete: [PR #928](https://github.com/mkny13/mahler/pull/928)
merged #918, and the [owner's D41 evidence](https://github.com/mkny13/mahler/issues/903#issuecomment-6108885290)
records the capped gateway trials, promotional-credit billing and the supersession
of PR #913. Trial 2 used $0.0035 under a $0.10 ceiling; trial 3's successful run
11801 used a $3 ceiling. This change makes no additional live API call.

Reinstall the hand-managed runner from the merged checkout with
`uv tool install --force ./sdk_runner`. The new `--discussion-mcp` mode disables
native tools, hooks, plugins and project settings, attaches only the seven restricted
issue tools, and resumes the prior SDK session. An older runner fails before dispatch.
Restart an existing gateway after deploying this code (D41); it retains its old code
until stopped. Start the configured gateway with `mahler gateway <platform>` before
using work discussions. The discussion worker only attaches to an existing gateway;
it never reads either upstream key or starts a credential consumer using a different
configuration. Refresh missing/stale org usage through normal `mahler usage --probe`.

The worker receives a random gateway token scoped to the durable session, active turn,
worker fence and deadline. Only its hash is stored; it is revoked at turn exit. Each
helper, main, resumed or retried HTTP request reserves independently against the same
D41 pool and allowance. Reservation attempt keys retain the discussion/session IDs;
`run_id` stays null. Duplicate completion cannot charge again. An uncertain transport
outcome retains its worst-case reservation, even if the worker dies or retries.
No build run or lease is created. Work discussions require a fresh org sample as well
as a usable key, confirmed grant and available balance, including on each request.

Responses remain buffered at the gateway. Gateway loss or replacement stops the turn
with a retryable error within the existing 120-second deadline; retry is explicit.
Automated evidence is in `tests/test_discuss_credits.py`, `tests/test_discuss.py` and
`tests/test_api_credits.py`, using fake SDK/provider calls and isolated ledgers.
The remaining post-merge check is one work-project conversation in the approved panel,
confirming its displayed reply and issue links. No new spending authorization is needed.
