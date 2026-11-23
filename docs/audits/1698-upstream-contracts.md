# Upstream decoder fixture provenance and contract-check audit (#1701, part of #1698)

Scope: four decoders in `mahler/platforms.py` that parse output from services Mahler
does not control. Source read at base commit `b6314e3`. Nothing here contacted a
network, a CLI, a keychain or a credential file. Recommendations are findings, not
completed remediation. The couch-tour#405 reference motivated the audit; no work was
done in that repository.

## Evidence vocabulary

- **Recorded with provenance**: bytes captured from the real service, with source,
  version and date committed beside them. **None of the four decoders has this.**
- **Synthetic**: built by hand in test code to fit the decoder. May be plausible; it is
  not evidence of what upstream sends.
- **Provenance unknown**: looks like it may derive from a real response, but the
  repository does not say so.
- **Local replay**: a unit test feeding a payload to the production decoder offline.
  It proves the decoder is deterministic on that payload only.
- **Live upstream verification**: running the decoder on current upstream output. No
  scheduled contract assertion does this; routine production polling is described below. All evidence in this report is local replay.

## Matrix

|  | Upstream | Fields consumed | Fixtures (path : name) | Provenance | Missing field | Wrong type | Failure behaviour (from code) | Scheduled check invoking the production decoder on current upstream output |
|---|---|---|---|---|---|---|---|---|
| `_claude_usage` | `GET api.anthropic.com/api/oauth/usage?cedar_ember=1` via `oauth_usage` (`platforms.py:752`). Undocumented (DESIGN D35). | `five_hour`/`seven_day`: `utilization`, `resets_at`. `cedar_ember`: `eligible`, `grants[]` (`id`, `resets_left`, `usable_now`, `paused`, `ends_at`; `gives_back_weekly` and `clears` type-checked only) | `tests/test_claude_quota.py` `body()`/`grant()` (lines 21-38), used by `CedarEmberParsingTests` and `test_oauth_usage_asks_for_cedar_ember_and_returns_metadata` | **Synthetic.** Hand-built dicts with placeholder ids (`grant_1`) and a patterned org UUID. DESIGN D35 says only that the POST body follows t3code's `claudeResetCredits.ts`; it records no capture of the GET body. | Window skipped when `utilization` is None or the window is absent/falsy (not directly covered here). Null `resets_at` and grant `ends_at` are covered by `test_usable_grants_are_counted_with_sorted_expiries_and_no_ids`; missing resets pass through as None without validation. Absent `cedar_ember` means no credits, tested by `test_missing_null_and_ineligible_mean_no_credits`. | Grants: strict and fails closed, well tested (`test_malformed_cedar_ember_fails_closed`, 13 bad grants and 3 bad `cedar_ember` values). **Windows: untested.** Probed offline: `utilization: "x"` raises `ValueError`; `utilization: true` is accepted as 1.0; a non-dict window raises `AttributeError`. `oauth_usage` catches both and returns `[]`, so a bad window drops the whole reading, but no test pins that. | `oauth_usage` returns `[]` on URL, OS, value, type, attribute or key errors. Fail closed. | **None.** |
| `_codex_usage` | `codex app-server` JSON-RPC `account/rateLimits/read` via `probe_codex` | `rateLimits.primary`/`secondary`: `windowDurationMins`, `usedPercent`, `resetsAt`. `ordinaryUsageAllowed`. `rateLimitResetCredits`: `availableCount`, `credits[]` (`id`, `status`, `expiresAt`) | `tests/test_codex_quota.py` `response()` (line 20), used by `CodexProbeTests` and `CodexRefreshTests`; the fake app-server script (~line 40) replays it over stdio | **Synthetic.** Hand-built dict with `secret-credit-id` and epoch `1900000000`. DESIGN says "verified end-to-end 2026-09-13 (mahler#157)" for the CLI run path, not for this payload. | `primary` None is skipped. `secondary` None tested via `personal=True`. Absent `rateLimits` raises `KeyError`, caught in `probe_codex`, returns `[]` (absence itself untested). Null `resetsAt` is covered by `test_windows_null_reset_and_sanitized_credits`; absent credit metadata yields unknown count and no expiries. Empty window tested (`test_malformed_shapes_fail_closed`: `{"rateLimits": {"primary": {}}}`). | Strict: `windowDurationMins` and `usedPercent` type, finiteness and sign checked; `availableCount` type checked. Tested for a subset (`[1]`, list `rateLimits`, empty window, multi-window). Credit rows with a non-dict element or non-numeric `expiresAt` are untested. | `probe_codex` returns `[]` on OS, subprocess, value, type, key, attribute or overflow errors; `_codex_rpc` bounds time and size. Fail closed. | **None.** |
| `parse_agy_usage` | `agy -p /usage --output-format json` via `probe_agy` | `command.data.groups[]`: `name`, `buckets[]` (`window`, `remaining_fraction`, `reset_time`) | `tests/test_routing.py` `ParseTests.test_agy_usage` (inline dict, line 695+) | **Provenance unknown.** Values (`Gemini Models`, `2026-09-18T21:25:17Z`) look copied from a real reading and DESIGN row for agy says `/usage` was "Verified 2026-09-12", but no test, commit text or fixture file says the dict is that capture. | Bucket without `remaining_fraction` or with an unknown window is dropped. The fixture includes a `weekly` bucket without `reset_time`, but asserts its percentage only; it does not pin the None reset result. Missing containers default to empty dict/list; explicit null containers raise `AttributeError`/`TypeError`, untested. | **Not defended.** Probed offline: top-level list or string raises `AttributeError`; a non-dict group raises `AttributeError`; `remaining_fraction: "x"` raises `ValueError`; `5` yields used 100*(1-5) = -400.0%; `true` yields 0.0%. `probe_agy` catches `SubprocessError, OSError, ValueError` only, so `AttributeError` escapes it. No test. | `probe_agy` returns `{}` for subprocess, OS and value errors. A shape drift that raises `AttributeError` propagates to callers (`usage.py:312`, `cli.py:605`, `warmup.py:47`); this audit did not trace whether the tick isolates it. Drift that parses without raising (negative or over-100 percent) is recorded as is. | **None.** |
| `parse_kiro_usage` | `kiro-cli chat /usage --output-format stream-json` text via `probe_kiro` | Free text: `resets on YYYY-MM-DD` and `Credits (X of Y covered`. The printed percentage is not read; it is recomputed. | `tests/test_platforms.py` `KiroUsageProbeTests`: `test_parse_kiro_usage_real_output`, `_half_credit`, `_uses_reset_date`, `test_probe_kiro_parses_acp_jsonl` | **Provenance unknown for the text, synthetic for the event stream.** A comment above `_KIRO_USAGE_RE` (`platforms.py`, ~line 1218) says the text was "verified 2026-10-01 against kiro-cli 2.26.1 ... KIRO FREE". It is a transcription in a comment and test string, not a stored capture. The JSONL event wrapper in `test_probe_kiro_parses_acp_jsonl` is hand-built; no real stream is stored. | No `Credits (` line returns `[]`. Tested. Missing reset date falls back to next month start. Not tested for the fallback value. | Out-of-range input accepted: `99 of 50` yields 198.0%; an impossible date `2026-13-45` is passed through as an ISO-looking string. No test. | Returns `[]` on no match or non-positive total. A drifted but still-matching text produces a wrong sample silently. | **None.** |

## Is any scheduled check a contract test?

- `.github/workflows/ci.yml` triggers on `pull_request`, `merge_group` and push to
  `main`. There is no `schedule:` trigger, and `.github/dependabot.yml` only updates
  Action versions. CI runs the unit suite, which uses the synthetic and
  transcribed fixtures above. That is local replay on code changes only; upstream can
  change on any day with no code change and CI stays green.
- DESIGN (D25 section on warmup) states "No GitHub Actions: exporting work OAuth
  logins to GitHub secrets would violate the on-Mini boundary". So CI is
  deliberately not the place for live checks that need a login.
- `mahler/platform_audit.py` is a **maintenance audit**, not a contract test. It
  reviews tier/size/model judgment calls from DESIGN verification dates and ledger
  outcomes. By grep it never imports or calls `_claude_usage`, `_codex_usage`,
  `parse_agy_usage` or `parse_kiro_usage`; its CLI model probe is the opt-in
  catalog (`agy_catalog`, off unless `platform_audit.model_list_cli`). It also
  refreshes the Cline free-model catalog in `_availability_lines`; neither catalog
  check exercises these four quota decoders.
- The conductor tick calls these decoders on live output, every 5 to 15 minutes depending
  on the platform (`usage.py`, `warmup.py`, `resets.py`). That is real use of the
  production decoder, but it asserts nothing about shape. Three of the four fail
  closed to "no sample", which routing reads as `stale`. Codex has a deduplicated
  alert after 4 hours of a stable unsupported shape (DESIGN, Codex row). No
  equivalent shape-drift alert was found for Claude, agy or Kiro in this audit.
  A reading that parses but is wrong (agy, Kiro) raises nothing.

## Access prerequisites (what is and is not established)

| Needed to capture live output | Status from repository docs |
|---|---|
| Claude OAuth token | Local-only: read from the login's own keychain entry or credentials file by `claude_oauth_token` on the Mac mini; D25 forbids ambient fallback across accounts. **No documented CI secret or variable name.** Unavailable to GitHub-hosted runs. |
| Codex login | `CODEX_HOME` per account, consumed by `run_env` on the mini. **No documented CI secret.** |
| agy login, Kiro login | Local CLIs signed in on the mini. **No documented secret path or variable name for any runner.** |
| Protected live acceptance access | **Unavailable/unverified.** Nothing in the repository names a provider, external secret path or variable, or a permitted non-mini consumer. Do not assume one. |

Consequence: the mini is the proposed location, not proof that capture access is
available. Before implementation, document each provider, external credential path
or variable name, and permitted capture/check consumer; unknown access remains
unavailable/unverified. Do not read ambient credentials or export logins to CI.

## Prioritised recommendations

These are proposals only; nothing was implemented. Order is by risk times effort.

1. **Offline, no access needed: harden `parse_agy_usage` and `parse_kiro_usage`
   against the shapes probed above, with tests.** Reject non-dict containers, clamp or
   reject `remaining_fraction` outside 0..1 and `used > total`, validate the reset
   date, and catch `AttributeError` in `probe_agy`. These findings can produce wrong numbers rather than no sample; Claude
   also accepts boolean utilization as a number. It changes
   production code, so it needs its own issue.
2. **Add malformed-window tests for `_claude_usage`** (string, bool and non-dict
   `utilization`/window; expected `[]` through `oauth_usage`). Decide whether
   `utilization: true` should be rejected as `_codex_usage` does for `usedPercent`
   (it checks `type(...) in (int, float)`).
3. **Record one capture per decoder, then replay it offline.**
   - *Source*: an authorized capture process on the mini for each upstream, after
     its account provider, credential location and permitted consumer are documented. For Claude, only
     the body of `GET .../oauth/usage?cedar_ember=1`; for Codex, the single
     `account/rateLimits/read` result line; for agy, the `/usage` JSON; for Kiro, the
     `stream-json` stdout of `/usage`.
   - *Metadata committed beside it* (a `tests/fixtures/upstream/<decoder>.json` plus a
     sidecar): upstream name, CLI or API version (`--version`, `anthropic-beta`
     header value), capture date, plan tier, command line used (no secrets), and the
     redactions applied.
   - *Redaction*: replace org UUIDs, grant and credit ids, account names and
     emails with fixed placeholders; keep key names, types, nesting and numeric
     formats unchanged so the shape stays faithful; never store headers, tokens or
     stderr. Review the diff by eye before commit. Document the procedure in the
     sidecar.
   - *Replay*: a unit test loads the fixture and calls the production decoder
     (`_claude_usage`, `_codex_usage`, `parse_agy_usage`, `parse_kiro_usage`),
     asserting the consumed fields. It runs in the normal suite with no network.
   - Label each file as `recorded`; keep the existing hand-built payloads labelled
     synthetic for both happy-path and malformed-case coverage.
4. **A scheduled live contract check on the mini, not in GitHub Actions** (D25
   forbids exporting logins). Cadence: weekly, plus once after any CLI version change,
   run through the existing per-account `run_env`, one read per login. Each read
   reuses the zero-token paths already in production (`oauth_usage`, `probe_codex`,
   `probe_agy`, `probe_kiro`) and additionally checks structure, not only the parsed
   result: required keys present with expected types. Timeout: keep the existing
   bounds (15 s for Claude and Codex, 90 s for agy, 30 s for Kiro). Failure routing:
   a shape mismatch raises a deduplicated `mahler notify` ping, cleared on recovery,
   modelled on the Codex unsupported-shape alert, and routes a linked p1 fix issue through the conductor per decoder
   naming the diverging keys; a missing CLI or signed-out login is "unavailable",
   reported separately, never as a pass. This adds a scheduled job and so needs
   an owner-visible issue of its own.
5. **Shape-drift alerts for Claude, agy and Kiro**, parallel to the Codex 4-hour
   alert, so a decoder that silently returns "no sample" is noticed in the existing
   tick rather than only by the weekly check.

## Verification performed for this report

Offline decoder tests (stdlib `unittest`, run with `discover -s tests -p <file>`):

| Command | Result |
|---|---|
| `tests/test_claude_quota.py` | 27 tests OK |
| `tests/test_codex_quota.py` | 25 tests OK |
| `tests/test_platforms.py` | 82 tests OK |
| `tests/test_routing.py -k test_agy_usage` | 1 test OK |

(`python3 -m unittest tests.test_routing...` does not import in this tree because the
tests rely on `tests/` being on `sys.path`; use `discover -s tests`.)

Offline probes of malformed input against the production decoders were run in a
throwaway interpreter session, not committed; their outputs are quoted in the matrix.

Repository checks:

| Command | Result |
|---|---|
| `python3 -m mahler.repo_guard` | exit 0 |
| `python3 -m unittest discover -s tests` | 3917 tests, OK, 2 skipped, 344 s |
RANDOM_STRICT_PLACEHOLDER
