# Execution and permission boundary audit (#1631, part of #1628)

Audited **2026-11-21**, source SHA **48f30e7e534214c00017bc8a88660a13373713b3**
(planning baseline was dbde249fd68560bf2377826a581ded962c62c27c).
Only this report and `tests/test_security_execution_audit.py` change. Production
remediation and security policy changes are separate work. Sources below are
repository-relative; line numbers refer to the audited SHA, not a moving upstream.

Read DESIGN D12, D18, D25, D46 and D47. Earlier history (#77's deny rendering)
was treated as background and rechecked against source. No account probes,
credential reads, external CLI permission experiments, destructive commands or
live database operations were performed. This is a finite source and mocked
execution audit, not a claim that the repository is vulnerability-free.

## Boundary matrix

| Boundary | Verdict | Provenance, enforcement and evidence | Remaining limit / proposal |
| --- | --- | --- | --- |
| Git/gh operands versus options | **Supported** for tested SHA rejection and blob lookup; **coverage gap** for generic operands | `runner.git:61`, `gh._gh:102`, `gh._git:152` use argv lists, not shell strings. These wrappers do **not** reject options. `runner.slug:149` restricts issue-title branch suffixes to lowercase alphanumeric/hyphen, prefixed with `mahler/<number>-`. `remote_has:154` prefixes refs; `GH.base_in_head:1109` validates the head as exactly 40 hex characters and validates a qualified base ref. `DiscussionTools._read_file:204` resolves a tracked path to a Git-produced blob OID, never passes the requested filename to Git. New tests `test_option_like_pr_head_is_refused_before_execution` and `test_option_like_tracked_filename_resolves_to_blob_operand` cover both refusal and safe resolution. | List argv prevents shell parsing, **not option injection**. `remove_worktree:1108` passes branch to `git branch -D` without `--`, unlike `cleanup_interactive_worktree:109`. Caller/generated-ref constraints matter. Inventory all callers and use command-specific validation/terminators in a bounded follow-up; do not indiscriminately insert `--` into arbitrary wrapper arguments. |
| Prompt/worktree metacharacters in launch/resume | **Supported** shell quoting; **coverage gap** CLI option parsing | `platforms.argv_for:571`, `resume_argv_for:637` produce lists; `runner.spawn:332` uses `shlex.join(argv)` plus quoted stdin/log/status paths. Cwd is passed separately. `launch:603` writes the SDK prompt file. New `test_launch_and_resume_metacharacters_remain_shell_data` exercises all nine fresh builders and five resume builders through mocked Popen, with spaces, quotes, semicolons, newlines, dollar substitution and backticks. | Positional prompts in cline/codex/kilo/kiro and codex resume's session ID have no `--` separator. A direct synthetic prompt `--help` is emitted in an option-capable position. Actual CLI parser behavior and exploitability from rendered recipe prompts are **unverified**, not a demonstrated remote exploit. Verify pinned parsers offline and add adapter-specific delimiters or file/stdin transport where supported. Flag-value operands also need parser-specific treatment for leading hyphens. |
| Setup and verification shell strings | **Accepted limitation** (intentional trusted code); **supported** separation from issue interpolation | `runner.launch:640` inserts `pol['setup']` intentionally into a shell prefix only for build. `config.project_policy:1643` merges configured defaults/project policy; setup/verify defaults are in `config.py:182,199`. `finalize._verify_fallback` selects `pol.get('verify')`, not issue text, and passes it to `start_verify_in_worktree`. Both verification APIs intentionally run shell commands. `.mahler/project.toml` is the repository's declared verification contract; it is not evidence that these APIs sanitize arbitrary commands. | These are trusted operator configuration, not a safe interface for issue-supplied shell text. Verify runs worktree code, which can itself execute commands. The verifier inherits conductor environment (no `env` argument), so source separation is **not credential or process containment**. Follow-up: evaluate a restricted verifier environment/identity separately without changing accepted policy here. |
| Lease pre-push fence | **Supported** cooperative fence; **accepted limitation** against bypass | `fence_hooks:199` writes pre-push lease-check before chaining executable original hooks. Paths and MAHLER_BIN are quoted. `run_env:447` supplies hooksPath, project/issue/epoch. New `test_fence_quotes_hook_paths_and_checks_lease_before_chaining`; existing `test_runner.test_run_env_contains_fence_and_epoch`, `test_hooks.test_lease_check_reads_the_run_env_and_allows_outside` cover wiring and stale refusal. | Agents can bypass local hooks with `--no-verify`, change environment/config, or execute other tools. Conductor snapshot/catch-up deliberately bypass them. D47 scoped credentials and remote branch protection are separate fences; neither their deployed configuration nor resistance to a malicious local agent was live-tested. |
| Console writes and discussion dispatch | **Supported** under network/local trust model; **accepted limitation** as user authentication | `serve.write_allowed_from:436` accepts loopback/Tailnet including mapped IPv4, not forwarded headers. `_refusal:308` requires console marker, JSON for writes, matching Origin netloc when supplied, bounded length. `do_POST:334` requires a named action and refuses before reading or dispatching. `DiscussionTools.call:74` rechecks session/worker binding, permits seven exact names, validates argument keys/types and bound project; issue reads/writes enforce scope and D46 admission. New console and discussion refusal tests assert no body read/action/Git/client call; existing console, serve and discussion tests listed below exercise real fixture paths. | Any local or Tailnet client can supply the marker and omit Origin; this is not a per-user login or capability token. Origin comparison is netloc-only, not full scheme/host/port equality. Proxy deployments must preserve the intended peer trust boundary. Discussion exposes issue mutation, not shell, merge, claim or arbitrary HTTP. |
| Deny rendering across nine platform kinds | **Supported** rendering on three; **accepted limitation / coverage gap** elsewhere | See platform matrix. Existing guardrail tests pin the exact shared stems; all nine builders are exercised by the new mocked launch test. | D12 prefix-only rules catch accidents, not adversarial command semantics. CLI enforcement was not executed or independently verified in this audit. No sandbox containment claim. |

No newly confirmed exploitable defect is asserted. The option-parsing and verifier
identity observations are concrete **coverage gaps**, with bounded follow-up
proposals, rather than unsafe behavior encoded as a desired regression contract.

## Platform deny matrix

All entries describe emitted source at the audited SHA, not a claim about today's
installed CLI implementation. `DENY_STEMS` is at `platforms.py:201`; it includes
force/delete/mirror pushes, history rewriting, hard resets, worktree removal,
destructive gh operations, token display, secrets/variables, DELETE API prefixes,
workflow/rerun operations, protection edits and selected `rm -rf/-fr` forms.

| Kind | Fresh/resume builder | Emitted mechanism | Verdict / gap |
| --- | --- | --- | --- |
| claude | `claude_argv:411`; `resume_argv_for:637` | `--disallowedTools` with every `Bash(<stem>:*)`; resume reuses fresh argv | Supported rendering, prefix-only limitation; bypassPermissions remains enabled. |
| copilot | `copilot_argv:451`; resume unsupported | One `--deny-tool shell(<stem>:*)` per stem alongside allow-all tools/paths/URLs | Supported rendering; precedence documented in source, runtime untested here. |
| agy | `agy_argv:421`; resume unsupported | No deny flag; skips permissions | Accepted flagless gap. |
| cline | `cline_argv:436`, `cline_resume_argv:593` | No deny flag; auto-approve true | Accepted gap; resume builder is a fresh-run fallback, not true continuation. Hook contract unverified. |
| codex | `codex_argv:467`; `resume_argv_for:637` | No deny flag; bypass approvals and sandbox | Accepted gap; execpolicy behavior under bypass/ephemeral not established. |
| kilo | `kilo_argv:482`, `kilo_resume_argv:615` | No deny flag; auto mode | Accepted gap. |
| agent-sdk | `agent_sdk_argv:492`; resume unsupported | One `--disallowed-tool Bash(<stem>:*)` per stem; prompt-file transport | Supported rendering; SDK consumption is outside the five-module audit. |
| kiro | `kiro_argv:507`; resume unsupported | `--trust-tools read,write,glob,grep`, omitting shell | Supported allowlist rendering, not a deny list; upstream enforcement and indirect effects of file writes remain coverage gaps. |
| vibe | `vibe_argv:524`, `vibe_resume_argv:536` | No command deny list; trust and auto-approve | Accepted gap; price-zero flag is spend policy, not execution isolation. |

D12 explicitly acknowledges reordered flags and alternate forms such as
`git push origin :branch` escaping prefix rules. Wrapping commands, aliases,
subshells and other tools are not proven blocked. Neither these patterns nor
flagless platforms are repaired or silently treated as secure here.

## Subprocess / shell call-site inventory

Complete direct subprocess sink inventory for the five named modules, including
the subprocess call inside the verifier's generated Python source. The generic
wrappers cover their indirect callers; representative command families and operand
provenance are included below. No subprocess/os.system sink exists in `serve.py`.

| Source symbol / sink line | Inputs and construction | Enforcement / residual trust |
| --- | --- | --- |
| `runner.git:62` | `git -C <configured repo/worktree>`, caller argv | No shell; generic wrapper has no operand validation. Used by prepare, fetch, catch-up, snapshot, cleanup and status helpers. |
| `runner.remote_has:155` | `rev-parse --verify --quiet refs/remotes/origin/<ref>` | Ref prefix prevents leading option interpretation; Git validates syntax. |
| `runner.start_ref:167` | `merge-base --is-ancestor origin/<a> origin/<b>` | Fixed prefixes; branches originate in saved run/item state. |
| `runner.spawn:382` | `/bin/sh -c <trusted prefix + quoted argv/redirections>` | Explicit shell boundary; quoting is tested. Setup prefix is trusted code. |
| `runner.spawn:401` | Python `-c` constant tracker code; run ID, PID and paths are separate argv | Numeric marker checked, status directory matched; no data interpolation into Python source. |
| `runner._merge_open_marker_ownership:848` | `lsof -Fn -- <marker path>` | Explicit option terminator; configured run-root marker, symlink refusal, process UID checks. |
| `runner._ps_process_listing:896` | Fixed ps argv; optional fixed `-E` | No issue operands; process output is sensitive and must not be logged. |
| `runner.verify_in_worktree:1157` | `subprocess.run(verify_cmd, shell=True, cwd=wt)` | Trusted policy command; timeout, result capture; inherited environment. |
| `runner.start_verify_in_worktree` generated code `:1190` | `subprocess.call(command, shell=True, timeout=...)` | Command arrives as argv to constant Python supervisor, then intentionally becomes shell code. |
| `runner.start_verify_in_worktree:1205` | Python `-c` constant script, verify command, numeric timeout, status path | List argv; fresh process group and output file; no isolation from worktree code or inherited identity. |
| `platforms.probe_cline:257` | Config model, temporary cwd, constant prompt | List argv; no auto-approval; bounded timeout. Not run here. |
| `platforms.probe_copilot_login:275` | Resolved executable, constant prompt | List argv; timeout; supplied env. Not run here. |
| `platforms.probe_cline_model:304` | Selected model as `-m` value, temporary cwd, constant prompt | List argv; approval false; external model/parser trust. Not run here. |
| `platforms._claude_oauth:698` | `security find-generic-password -s <configured service> -w` | Config service is a flag value, not shell; reads credentials, deliberately not run. |
| `platforms._codex_rpc:967` | Resolved executable plus fixed `app-server` | JSON RPC on stdin, not command interpolation; supplied env. Not run here. |
| `platforms.probe_claude:1116` | Fixed probe args/model/prompt, resolved executable | Tools disabled, strict MCP/settings switches; env supplied. Not run here. |
| `platforms.probe_agy:1154` | Fixed `/usage` prompt and timeout flags | No issue inputs, no shell; executable/PATH trusted. Not run here. |
| `platforms._gh_login:1168` | Fixed `gh api user --jq .login` | Supplied identity; stdout login consumed by next probe. Not run here. |
| `platforms.probe_copilot:1192` | `/users/<API login>/settings/billing/ai_credit/usage` | Starts with `/`, cannot become an argv option; server identity/path semantics trusted. Not run here. |
| `platforms.probe_kiro:1266` | Fixed `/usage`, empty trusted-tools value | No issue operands; timeout/env. Not run here. |
| `gh._gh:104` | `gh` plus caller args; optional stdin | No shell; no generic validation. Titles/bodies normally explicit flag values or JSON/stdin; issue/PR numbers from caller state. Env and error redaction applied. |
| `gh._git:156` | `git -C path` plus caller args | No shell; timeout; identity env and error redaction, not generic option sanitization. |
| `GH.push_branch:818` | `ls-remote origin refs/heads/<branch>` through `_git_args` | Qualified ref; App mode replaces origin with configured repository HTTPS URL. Conductor authority, not an agent tool. |
| `GH.base_in_head:1127` | `merge-base --is-ancestor <Git tip> <validated head>` | Head exact SHA; target validated/fetched under qualified ref; distinguishes drift from lookup failure. |
| `DiscussionTools._git:171` | Fixed `ls-tree ... HEAD`, then `cat-file blob <OID>` | No shell; caller cannot choose command; only tracked regular files, byte limits and wait timeout. |
| `serve` | No direct subprocess/shell call | `do_POST` dispatches only `actions.ACTIONS` after refusal checks. Downstream actions are outside this sink inventory. |

`runner.fence_hooks:199` also **writes shell programs**, although it does not
execute a subprocess itself. It quotes configured original-hook paths and the
Mahler executable, invokes `lease-check`, then `exec`s the original hook with
`"$@"`. `fetch_origin:74` installs a fixed `!gh auth git-credential` Git helper
string on its SSH-to-HTTPS retry: an intentional Git-executed shell helper,
not issue-derived text. `launch:640` constructs the setup shell prefix described
above. These shell-producing paths are included even though an AST search for
subprocess calls alone would miss them.

Indirect Git families: `prepare` consumes trusted path/base/link settings plus
admitted issue number/title and saved branch, creates generated worktrees and
branches; `catch_up` uses `origin/<base>` and qualified push refspecs;
`snapshot` consumes Git-produced tree/commit IDs and generated snapshot names,
stages untracked files in a temporary index and deliberately force-pushes with
conductor identity. `remove_worktree` checks resolved root containment before
removal, but its branch deletion lacks the option delimiter noted above.
`GH` reads/writes use fixed subcommands/API endpoints with repository config,
server numbers/refs and body/title data. Qualified refspecs and SHA validation
are specific protection; generic wrappers must not be credited with either.

## Evidence and reproducibility

New selectors (class `ExecutionAuditTests` in `tests/test_security_execution_audit.py`):

- `test_launch_and_resume_metacharacters_remain_shell_data`
- `test_option_like_pr_head_is_refused_before_execution`
- `test_option_like_tracked_filename_resolves_to_blob_operand`
- `test_discussion_unauthorized_names_and_project_do_not_dispatch`
- `test_console_refusal_precedes_body_read_and_action`
- `test_fence_quotes_hook_paths_and_checks_lease_before_chaining`

Run with `python3 -m unittest discover -s tests -p test_security_execution_audit.py`:
**6 passed**. Tests use synthetic operands, temporary hook files, an in-memory
Ledger, mocked subprocess/Git/GitHub, and scoped patches. No real ~/.mahler state.
The initial fixture attempt failed because Ledger is not a context manager;
`contextlib.closing` now ensures cleanup. That setup error is not security evidence.

This audit adds coverage rather than production behavior. To check failure
sensitivity without changing the read-only targets, an in-memory mutation patched
`mahler.runner.shlex.join` to `lambda argv: ' '.join(argv)` and ran the launch test:
it **failed with assertions and no execution**; the unmodified implementation
passes. This is the red/green check for an existing supported contract, not a
claim of a newly fixed production bug. Reproduce with unittest TestSuite and
`unittest.mock.patch` around that single selector; never execute the generated shell.

Existing evidence revalidated by the full suite:
`tests/test_runner.py` (adversarial launch, quoted repository hooks, fence epoch,
scoped identity and credential fallback refusal), `tests/test_hooks.py`
(`test_lease_check_reads_the_run_env_and_allows_outside`, takeover heartbeat),
`tests/test_guardrails.py` (exact stems, Claude/Copilot rendering),
`tests/test_console.py` (bad/stale action input), `tests/test_serve.py`
(`test_guard`, `test_same_origin_is_allowed`,
`test_only_this_machine_and_the_tailnet_may_write`), and
`tests/test_discuss_tools.py` (binding changes, extra keys, cross-project,
symlink/untracked paths, admission/comment filtering and mutation receipts).

## Verification results

CI at reviewed head `8a3491b9910f1b3993be280fa23a833596ecc6c2`
(review run 15136) recorded the following verification outcomes:

- `python3 -m mahler.repo_guard`: passed (exit 0).
- `python3 -m unittest discover -s tests`: passed, 3,858 tests.
- `python3 tests/run_random.py 393288850`: passed, 3,858 tests;
  randomized seed `393288850`.
- `python3 -X dev tests/run_strict.py`: passed, 3,858 tests.

## Handoff

Decided: preserve production policy and report the CLI operand and verifier
identity coverage gaps for bounded follow-up. Conductor owns shipping and any
linked remediation work. No human check is required. External CLI enforcement,
remote branch protection, deployed proxy topology and verifier credential
containment remain unverified; passing repository tests do not establish them.
