# Audit: Routing, Quota, and Account Policy against Architecture (Issue #1670 / #1673)

- **Audited source SHA:** `c01dee62` (main), Python 3.14.10, macOS.
- **Audited scope:** DESIGN D8, D21, D22, D23, D24, D25, D26, D33, D34, D35, D36, D41, ARCHITECTURE Router, and ARCHITECTURE Accounts.
- **Audited modules:**
  - Production code: `mahler/config.py`, `mahler/router.py`, `mahler/platforms.py`, `mahler/usage.py`, `mahler/resets.py`, `mahler/warmup.py`, `mahler/api_credits.py`, `mahler/run_usage.py`, `mahler/scorecard.py`, `mahler/model_availability.py`, `mahler/capacity.py`, `mahler/cline_free.py`.
  - Paired test suites: `tests/test_config.py`, `tests/test_router.py`, `tests/test_platforms.py`, `tests/test_usage.py`, `tests/test_burst_lifetime.py`, `tests/test_warmup.py`, `tests/test_api_credits.py`, `tests/test_run_usage.py`, `tests/test_scorecard.py`, `tests/test_model_availability.py`, `tests/test_capacity.py`, `tests/test_cline_free.py` (with additional validation in `tests/test_accounts.py`, `tests/test_routing.py`, `tests/test_codex_quota.py`).
- **Method:** Hermetic source analysis, verification of credential and routing isolation boundaries, trace of quota fail-closed mechanics and special policies, and execution of focused and repository-wide test gates using isolated fixtures (in-memory SQLite, isolated mock environments). No live provider or network probes were used.

---

## Decision-to-Code Matrix

| Decision / Claim | Result | Key Source Symbols | Concrete Test Evidence | Summary & Verification Proof |
|---|---|---|---|---|
| **DESIGN D8** (Platforms, quota sensing and routing) | `conformant` | `config.DEFAULTS["platforms"]`, `config.DEFAULTS["routing"]`, `platforms.claude_argv`, `platforms.agy_argv`, `platforms.cline_argv`, `platforms.copilot_argv`, `platforms.codex_argv`, `platforms.probe_*`, `router.candidates`, `router.pick`, `router.cap_escalation` | `tests.test_platforms.PlatformArgvTests`, `tests.test_platforms.AgyClaudeArgvTests`, `tests.test_platforms.CopilotProbeTests`, `tests.test_platforms.CodexRpcProbeTests`, `tests.test_platforms.OAuthUsageTests`, `tests.test_router.ReviewTierTests`, `tests.test_router.GhWritesTests`, `tests.test_routing` | Platform adapters implement headless invocation and non-interactive JSON/stream output. Quota probes read usage without inference cost. Default routing orders conform to specification: Sort (Claude → agy-gemini → agy-claude), Build (agy-claude → agy-gemini → cline-free → copilot → copilot-high → kilo → claude-opus → claude). Escalation rule 4 advances two failed attempts one tier up. At 100% usage platforms are marked exhausted; pay-as-you-go extra usage is prohibited. |
| **DESIGN D21** (Opus plans; free tiers build what it planned) | `conformant` | `config.DEFAULTS["routing"]["plan"]`, `config.DEFAULTS["platforms"]["claude-opus"]`, `tick.needs_plan`, `router.route_role`, `router.pick` | `tests.test_schedule.DesignRoleTests`, `tests.test_routing.RoutingTests.test_plan_routes_through_routing_plan`, `tests.test_routing.RoutingTests.test_claude_opus_soft_threshold_45_percent`, `tests.test_tick_capacity` | Goals (`type:goal`), maintenance passes (`pass:*`), and `size:l` items sort exclusively via `routing.plan` (`claude-opus`). Plan runs are exempt from `min_size`/`max_size` filtering. When headroom is absent or peak hours apply, planning waits and never falls back to free tiers. Sibling slots share `quota_group: "claude"`; Opus starts only below 5h 45% soft limit. |
| **DESIGN D22** (Claude's peak window) | `conformant` | `config.DEFAULTS["claude_peak"]`, `router.peak_state`, `router.peak_overridden`, `router.pick` | `tests.test_router.ClaudePeakTests` in `tests.test_routing`, `tests.test_accounts.RouterTests.test_peak_status_and_override` | Weekdays 5am–11am Pacific (8am–2pm Eastern), new Claude starts (`kind == "claude"`) are blocked with peak countdown reason unless explicit pin is set or overridden via `mahler peak --off` (kv `peak_override_until`). Running work continues uninterrupted; non-Claude platforms are unaffected. |
| **DESIGN D23** (Use it or lose it: burst before Claude reset) | `conformant` | `config.DEFAULTS["burst"]`, `usage.compute_burst`, `router.burst_status`, `router.all_bursts`, `router.platform_burst`, `router.effective_lines`, `router.candidates` | `tests.test_burst_lifetime.BurstLifetimeTests`, `tests.test_routing.RoutingTests.test_burst_promotion`, `tests.test_accounts.RouterTests.test_personal_burst_never_lifts_work_claude` | In the final hours before a Claude window resets (1h for 5h, 6h for weekly), reserve quota is released: soft line rises to 90%, hard line to 97%. Bursting Claude platforms move to the front of build routing. Bursts are strictly per quota-group (`all_bursts`); human presence (5h usage rise or transcript activity) defers burst for that group. |
| **DESIGN D24** (One canonical lease table for shared projects) | `conformant` | `router.platform_slot`, `router.pick` (`busy`, `exclude`), `capacity.Observer` | `tests.test_router.StaleCandidatesTests`, `tests.test_capacity.IntervalTests`, `tests.test_accounts.RouterTests.test_each_claude_login_has_its_own_run_slot` | Single canonical lease coordination across machines respects busy slot reservations. The router tracks active slot leases (`platform_slot`) to prevent double-allocation of run capacity while preserving deterministic local candidate ranking. |
| **DESIGN D25** (Accounts: work logins on same machine, never crossed) | `conformant` | `config.CREDENTIAL_VARS`, `config.account_of`, `config.accounts_of`, `config.run_env`, `config.validate_accounts`, `usage.claude_oauth_source`, `usage.codex_env`, `router.candidates`, `router.pick` | `tests.test_accounts.ConfigTests`, `tests.test_accounts.RouterTests`, `tests.test_accounts.SlotAndUsageTests`, `tests.test_accounts.LaunchAndGitHubTests`, `tests.test_router.AccountBoundaryAndFailClosedTests` | Non-personal account environments strip all `CREDENTIAL_VARS` before injecting `accounts.<name>.env`. Routing is per-account: a work project receives only work platforms, and pins across accounts are refused. Undefined accounts fail closed. Quota readings, slots, and burst states are segregated by login quota group. |
| **DESIGN D26** (Accounts: project may span multiple accounts) | `conformant` | `config.account_mode_of`, `config.resolve_github_app_provider`, `config.github_app_settings`, `router.candidates_for_accounts`, `router.candidates_for_priority`, `router.pick_for_project` | `tests.test_accounts.MultiAccountTests`, `tests.test_accounts.LaunchAndGitHubTests`, `tests.test_accounts.ConfigTests.test_config_rejects_duplicate_project_accounts`, `tests.test_router.AccountBoundaryAndFailClosedTests` | Multi-account projects configure `accounts = [...]`. `account_mode` supports `"order"` (fallback chain), `"equal"` (round-robin merge preserving per-account preference), and `"priority"` (exact cross-account route). Priority routes are strictly validated against declared accounts. GitHub App installation is inherited only by effective personal accounts unless explicitly scoped. |
| **DESIGN D33** (Cheapest variant that does the job, measured on our own work) | `conformant` | `scorecard.attempts`, `scorecard.completion_rate`, `scorecard.policy`, `scorecard.ranked`, `run_usage.collect`, `router.measured_order`, `router.explore_for_project`, `router.risk_min_tier` | `tests.test_scorecard.AttemptTests`, `tests.test_scorecard.TableTests`, `tests.test_scorecard.CompletionTests`, `tests.test_scorecard.LinkPatternTests`, `tests.test_run_usage.UsageTests`, `tests.test_run_usage.RequestEvidenceTests`, `tests.test_measured_routing` | Measures performance over 14-day defect window using 80% Wilson lower bound. Good-enough variants require ≥8 attempts and meet role bars (build/fix 70%, sort 80%, plan 75%). Free platforms rank ahead of paid platforms (`cost_class = "free" \| "paid"`). Exploration allocates 15% (build/fix/sort) and 5% (plan) to unproven candidates; risk keywords suppress exploration. |
| **DESIGN D34** (Warm-up: keep 5-hour windows chained during active hours) | `conformant` | `config.configure_warmup`, `warmup.active`, `warmup.environment`, `warmup.read_usage`, `warmup.nudge`, `warmup.exhausted`, `usage._recent_warmup_fire` | `tests.test_warmup.WarmupTests`, `tests.test_warmup.WarmupConfigTests`, `tests.test_warmup.WarmupAdapterTests` | Schedules idle window nudges during active hours (weekdays 7am–6pm local). Fires ~700-token lean probe 15 minutes before 5-hour window expiry if login has been quiet. Environment strictly respects account credential isolation (D25) and strips API billing keys. |
| **DESIGN D35** (Spend banked Codex resets before they expire) | `conformant` | `resets.spend_banked`, `resets._codex_logins`, `resets._claude_logins`, `resets._handle_login`, `resets._spend`, `resets._spend_claude`, `platforms.CodexUsage`, `platforms.consume_codex_credit`, `platforms.consume_claude_credit` | `tests.test_codex_quota`, `tests.test_platforms.CodexRpcProbeTests`, `tests.test_usage` | Banks free reset credits and applies finite spending rules: Rule (a) when weekly usage ≥ hard line, natural reset >24h away, and ready work exists; Rule (b) when credit expires within 48h, weekly usage ≥60%, and natural reset is after credit expiry. Covers Codex and Claude (`cedar_ember`). Only credits with confirmed expiry are spent; purchased resets are never touched. Attempt backoff is 30 minutes. |
| **DESIGN D36** (Learn each login's best warm-up time, data only) | `conformant` | `warmup.learn_slice`, `warmup.learn_time`, `warmup.learned_lines`, `warmup.collect_days`, `warmup.relearn`, `usage.note_human_rise` | `tests.test_warmup.LearnTimeTests`, `tests.test_warmup.HumanRiseTests`, `tests.test_warmup.TranscriptTests`, `tests.test_warmup.LearnCollectTests` | Pure calculation `W = F + E - 5h` clamped to `[F - 4h30, F - 30m]` (or `F - 3h` without exhaustion); 15-minute rounded median across 30-day window (minimum 5 days of usage). Read-only signals: Claude Code user timestamps (excluding Mahler worktrees and temp dirs) and ledger human-use events. Report-only; persisted in kv and never alters schedules autonomously. |
| **DESIGN D41** (Team-plan API credits: $20 allowance, rest before expiry) | `conformant` | `api_credits.Gateway`, `api_credits.current_grant`, `api_credits.usage_state`, `api_credits.reserve`, `api_credits.settle`, `api_credits.estimate_request_cost`, `api_credits.validate`, `api_credits.attach`, `router._quota_usage_state` | `tests.test_api_credits.GrantModelTests`, `tests.test_api_credits.GatewayTests`, `tests.test_api_credits.PricingTests`, `tests.test_api_credits.RoutingBoundaryTests`, `tests.test_api_credits.LineTests`, `tests.test_api_credits.CostReportTests`, `tests.test_api_credits.ExpiryTests` | Work Team plan API credits are governed by confirmed grants (`[[api_credits.grants]]`) with conservative UTC cutoffs (no forecast spending). Standard-library loopback HTTP gateway holds the real workspace key and issues per-run local tokens. Worst-case cost is reserved atomically in SQLite before forwarding. Stale org readings fail closed to the $20 allowance; unconfirmed grants halt spending. |
| **ARCHITECTURE Router** | `conformant` | `mahler/router.py`, `mahler/platforms.py`, `mahler/config.py` | `tests.test_router`, `tests.test_routing`, `tests.test_platforms`, `tests.test_config` | Router implements deterministic, quota-aware platform and account choice. Validates candidate routes, account boundaries, capability tiers, size constraints, peak hours, burst overrides, measured scorecard ranking, and platform holds. Stale, expired, or missing quota readings fail closed. |
| **ARCHITECTURE Accounts** | `conformant` | `mahler/config.py`, `mahler/platforms.py`, `mahler/usage.py` | `tests.test_accounts.ConfigTests`, `tests.test_accounts.RouterTests`, `tests.test_accounts.LaunchAndGitHubTests`, `tests.test_router.AccountBoundaryAndFailClosedTests` | Projects explicitly declare allowed accounts (`account` or `accounts`). Work logins execute under sanitised environments without personal credentials. A missing or unconfigured account fails closed with an explicit error. GitHub App identities remain segregated by installation. |

---

## Detailed Policy Traces

### 1. Candidate Expansion, Authorization, and Filtering
- Candidate expansion begins with `router.routing_for(cfg, account)`, resolving `@group` references via `config.expand_route` iteratively to prevent recursion cycles.
- For single-account projects (`account = "personal"` or `"work"`), `router.candidates` strictly filters candidates where `account_of(platform) == account`.
- For multi-account projects (`DESIGN D26`):
  - `"order"` mode iterates declared accounts sequentially, selecting the first account with available headroom.
  - `"equal"` mode invokes `router.candidates_for_accounts`, round-robin interleaving candidate lists from each declared account via `zip_longest` while preserving internal priority.
  - `"priority"` mode uses `router.candidates_for_priority`, executing the project's exact custom cross-account routing table while ensuring every candidate belongs to a declared account.
- Filtering in `router.pick`:
  - **Account authorization:** Pins to platforms outside the project's declared accounts are rejected with an explicit reason (`"<pin>: pinned, but it spends the <acct> account, not <project_accts>"`).
  - **Capability tiers:** Candidates below `min_tier` are skipped (`"<name>: tier <t> below escalation tier <min_tier>"`), unless explicitly pinned.
  - **Size filtering:** Sorters respect explicit item size ceilings (`role == "sort"` checks `max_size`); plan and review roles are exempt; builders enforce both `min_size` and `max_size` (pins override size limits).
  - **Platform holds:** Both system holds (startup silence, auth failure, credit exhaustion) and project-specific holds (`project_hold`, where an account login was refused) block candidate selection.
  - **Independent review:** The builder platform slot is excluded from reviewer selection via `platform_slot`.

### 2. Quota Freshness and Fail-Closed Mechanics
- In `router._quota_usage_state`:
  - Platforms marked unmetered (`is_metered == False`) operate normally unless a quota/rate-limit error placed them in backoff (`used_pct >= 100` with `resets_at > now`), in which case they return `"hard"`.
  - Metered platforms evaluate target windows (`5h`, `weekly`, `monthly`, or `cycle`).
  - Missing sample (`u is None`): returns `"stale"`.
  - Expired sample (`sampled_at < now - stale_after`): returns `"stale"`.
  - Window reset passed (`resets_at <= now`): returns `"stale"`, requiring a re-probe.
  - Codex unsupported quota shape: returns `"stale"`.
  - Usage above soft line: returns `"soft"`; usage above hard line: returns `"hard"`.
  - `router.pick` requires state `"ok"` to dispatch. Any candidate in state `"soft"`, `"hard"`, `"stale"`, `"blocked"`, or `"no_credit"` is bypassed.

### 3. Special Spending Policies
- **Claude Burst (D23):** Computed in `usage.compute_burst` and `router.burst_status`. Within lead hours (1h for 5h window, 6h for weekly), soft/hard lines rise to 90%/97%, and Claude candidates move to the head of the build order. Burst states are tracked per quota group (`router.all_bursts`), and human presence (5h usage rise or transcript activity) defers the burst.
- **Warm-up Windows (D34, D36):** `warmup.nudge` issues small probes (~700 tokens for Claude, zero-token for Antigravity/Codex) during configured active hours (weekdays 7am–6pm) 15 minutes before 5-hour window expiry. Learned warm-up times (`warmup.learn_time`) are computed purely from empirical first-use and exhaustion events over 30 days and persisted as report-only kv.
- **Codex & Claude Reset Credits (D35):** `resets.spend_banked` spends free banked resets under Rule (a) (weekly ≥ hard line, natural reset >24h out, work waiting) or Rule (b) (expires within 48h, weekly ≥60%, natural reset after credit expiry). Spends are bounded by 30-minute attempt keys (`reset:attempt:<platform>` and `reset:attempt:claude:<account>`).
- **Cline Free Candidate Selection (mahler#1507):** `cline_free.py` fetches recommended models from `free_models_url`, caches validated models in SQLite, and purges models that bill. If all models fail probes, `unusable_until` triggers a 10-minute platform hold rather than consuming credits or blocking tick execution.
- **Team API Credits (D41):** `api_credits.Gateway` enforces worst-case pre-request reservation via `Ledger.reserve_credit` on localhost HTTP proxy, restricting models to `allowed_models` and output tokens to `max_output_tokens`. Confirmed grants (`current_grant`) prevent speculative forecast spending.

### 4. Account Isolation and Credential Boundary Verification
- `config.CREDENTIAL_VARS` defines the canonical set of credential environment variables:
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, `COPILOT_HOME`, `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, `GH_CONFIG_DIR`, `GH_HOST`, `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_HOME`.
- When constructing a non-personal execution environment (`config.run_env`), all variables in `CREDENTIAL_VARS` are unconditionally stripped from the base process environment before injecting the account's configured `env`.
- An undefined account raises `ValueError` immediately, failing closed.
- `usage.claude_oauth_source` and `usage.codex_env` strictly verify that non-personal accounts use their own configured directories and credentials; if missing, they return `None` and never fall back to `~/.claude.json`, `~/.codex/auth.json`, or the personal macOS Keychain.
- In `config.resolve_github_app_provider`, default GitHub App configuration is only inherited by personal GitHub accounts (`gh_account_of(pol) == DEFAULT_ACCOUNT`). Work accounts must explicitly define `github_app_installation_id` to use App credentials; otherwise they fall back to their own account `gh` environment.

---

## Retained Compatibility Paths and Justifications

The audit inspected all legacy branches and compatibility paths across the bounded source files:

1. **`codex` compact personal alias (`mahler/config.py`):**
   - *Status:* Retained and conformant.
   - *Justification:* Retained as the default personal medium-tier slot alias mapping to `gpt-5.6-terra`, while `codex-low` (Luna) and `codex-high` (Sol) provide capability boundaries.
   - *Exercised tests:* `tests.test_codex_quota`, `tests.test_accounts`, `tests.test_routing`.
2. **`codex:unsupported-shape:*` legacy KV key migration (`mahler/usage.py`):**
   - *Status:* Retained and conformant.
   - *Justification:* Migrates four legacy split KV keys into a single structured JSON payload (`codex:unsupported-shape:<account>`).
   - *Exercised tests:* `tests.test_codex_quota.TestCodexQuota.test_legacy_unsupported_shape_keys_are_migrated`.
3. **`model_unavailable` legacy hold migration (`mahler/model_availability.py`):**
   - *Status:* Retained and conformant.
   - *Justification:* Migrates legacy untyped `hold_reason:<platform>` flags into structured model-fingerprint rejection records in SQLite KV.
   - *Exercised tests:* `tests.test_model_availability.ModelAvailabilityTests.test_blocked_migrates_legacy_hold_when_run_metadata_exists`.
4. **`legacy_fail_reviews` fallback in scorecard attribution (`mahler/scorecard.py`):**
   - *Status:* Retained and conformant.
   - *Justification:* Correctly attributes older historical run rows that lack structured review-fix trigger event records, preventing historical data skew.
   - *Exercised tests:* `tests.test_scorecard.AttemptTests.test_adjudicated_false_review_excuses_fix_and_restores_success`.
5. **Opt-in platforms (`jetstream`, `kiro`, `vibe` in `mahler/config.py`):**
   - *Status:* Retained and conformant.
   - *Justification:* Declared with `enabled: false` and excluded from default routing. Available for explicit project opt-in (e.g. Jetstream2 work resource, Kiro credit pools).
   - *Exercised tests:* `tests.test_platforms.PlatformArgvTests.test_jetstream_disabled_and_unrouted_by_default`, `tests.test_platforms.VibeTests`, `tests.test_usage.VerifyingUsageTests.test_pinned_opt_in_platform_is_probed_only_when_pinned`.
6. **Disqualified platforms (MiMo Code, Hermes Agent, Kimi Code):**
   - *Status:* Verified absent from all production routing tables and configuration defaults, matching D8 investigation records.

---

## Corrections and Regression Coverage

1. **Test Module Self-Containment (`tests/test_burst_lifetime.py`, `tests/test_warmup.py`):**
   - *Finding:* Direct execution of `tests.test_burst_lifetime` and `tests.test_warmup` via `python3 -m unittest <module>` failed with `ModuleNotFoundError` when `PYTHONPATH=tests` was not set, due to un-prefixed imports of sibling test fixtures (`test_accounts` and `local_timezone`).
   - *Correction:* Added import fallbacks (`try: from ... except ImportError: from . ...`) ensuring both direct invocation and unittest runner discovery succeed hermetically.
2. **Account Boundary Negative / Abuse Tests (`tests/test_router.py`):**
   - *Added Suite:* `AccountBoundaryAndFailClosedTests` in `tests/test_router.py`.
   - *Coverage:*
     - Cross-account pin refusal: asserts pinning an unauthorized account platform is refused with a specific reason.
     - Priority route filtering: asserts platform candidates belonging to undeclared accounts are excluded from candidate generation.
     - Stale quota fail-closed: asserts candidates with stale usage samples are rejected.
     - Max size bounds: asserts oversized items without a pin are refused on size-limited platforms.

---

## Verification Evidence

1. **Focused Test Execution:**
   Command:
   ```bash
   python3 -m unittest \
     tests.test_config \
     tests.test_router \
     tests.test_platforms \
     tests.test_usage \
     tests.test_burst_lifetime \
     tests.test_warmup \
     tests.test_api_credits \
     tests.test_run_usage \
     tests.test_scorecard \
     tests.test_model_availability \
     tests.test_capacity \
     tests.test_cline_free
   ```
   Result: **Ran 457 tests in 3.327s, OK (exit 0)**.

2. **Repository Verification Gates:**
   - `python3 -m mahler.repo_guard`: PASSED (exit 0).
   - `python3 -m unittest discover -s tests`: Ran 3892 tests in 339.763s, OK (skipped=2, exit 0).
