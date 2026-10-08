# Branch Protection Policy and Runbook

Target: `mkny13/mahler` (default branch: `main`)

This runbook documents the classic branch protection configuration for `mkny13/mahler`'s default branch `main`, the design rationale preserving conductor autonomy under DESIGN D18 and D19, and the explicit commands to apply, verify, and audit settings.

## Policy Overview

The `main` branch is protected by GitHub classic branch protection to enforce continuous integration gates on pull requests prior to merge, without disrupting Mahler's autonomous conductor merge lifecycle.

| Setting | Value | Rationale |
| :--- | :--- | :--- |
| **Required Status Checks** | `["test"]` (strict: `true`) | Ensures PRs pass CI on the latest base before merging. |
| **Check App Identity** | GitHub Actions (`app_id: 15368`) | Originates from `.github/workflows/ci.yml` job `test`. |
| **Enforce Admins** | `true` | Gating applies to repository administrators. |
| **Pull Request Reviews** | `null` (disabled) | Autonomous shipping (D18); review is conductor-managed. |
| **Actor Restrictions** | `null` (disabled) | No actor/push restriction list. |
| **Allow Force Pushes** | `false` (disabled) | Protects history on `main`. |
| **Allow Deletions** | `false` (disabled) | Prevents branch deletion. |
| **Repository Merge Policy** | `allow_squash_merge: true` | Required by Mahler conductor squash merging. |

## Design Rationale

- **DESIGN D18 (Agents build, the conductor ships):** Autonomous build agents implement changes and push to their issue branch. The conductor opens PRs, watches CI, coordinates review, and squash-merges on green. Requiring pull request approvals (`required_pull_request_reviews`) would block autonomous shipping and require the repository owner to approve automated changes manually. Review gating remains owned by the conductor's review state machine.
- **DESIGN D19 (Base verification):** PR branches must be tested against the latest base before merging. Setting `strict: true` requires the head branch to be up to date with `main` before merging, aligning GitHub's server-side gate with the conductor's `base_in_head` ancestry verification.

## Application Commands

To configure branch protection via GitHub CLI:

```sh
gh api --method PUT repos/mkny13/mahler/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "test"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

## Readback Commands

Verify the applied settings using GitHub CLI:

```sh
# Read classic branch protection
gh api repos/mkny13/mahler/branches/main/protection

# Confirm effective rulesets are empty / compatible
gh api repos/mkny13/mahler/rules/branches/main

# Verify repository default branch and squash merge policy
gh api repos/mkny13/mahler --jq '{default_branch: .default_branch, allow_squash_merge: .allow_squash_merge}'
```

Expected readback output:

- `required_status_checks.strict`: `true`
- `required_status_checks.contexts`: `["test"]`
- `required_status_checks.checks[0].context`: `"test"`
- `required_status_checks.checks[0].app_id`: `15368`
- `enforce_admins.enabled`: `true`
- `allow_force_pushes.enabled`: `false`
- `allow_deletions.enabled`: `false`
- `allow_squash_merge`: `true`

## Acceptance Check

Execute the practices audit branch-protection check from the repository worktree root:

```sh
python3 - <<'PYTEST'
from pathlib import Path
from mahler.gh import GH
from mahler.practices_audit import scan_project

project = {
    'name': 'mahler',
    'repo': 'mkny13/mahler',
    'verify': 'python3 -m unittest discover -s tests',
    'path': str(Path.cwd())
}
finding = next(f for f in scan_project(project, GH(project['repo'])).findings if f.check == 'branch-protection')
print(finding)
assert finding.state == 'pass', finding.reason
PYTEST
```

## Verification Evidence (2026-10-07)

- `default_branch`: `main`
- `allow_squash_merge`: `true`
- `branches/main/protection`:
  - `required_status_checks.strict`: `true`
  - `required_status_checks.contexts`: `["test"]`
  - `required_status_checks.checks`: `[{"context": "test", "app_id": 15368}]`
  - `enforce_admins.enabled`: `true`
  - `allow_force_pushes.enabled`: `false`
  - `allow_deletions.enabled`: `false`
- `rules/branches/main`: `[]`
