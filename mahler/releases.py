"""The release ledger and deterministic rolling draft (DESIGN D31).

A release is a durable, tagged checkpoint with a semantic version, checkpoint SHA,
publication state/timestamps, notes, and remote URL. Shipped items belong to at
most one release.

Between releases, conductor-shipped changes accumulate in a deterministic
rolling draft. Notes, SemVer bumps, and advisory readiness suggestions are
synthesized purely in the standard library without calling an LLM during the tick.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from typing import Any, List, Optional

from .ledger import iso, parse, row_get

SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
STRICT_SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class ReleaseConflictError(RuntimeError):
    """Raised when remote or local tag/release state conflicts with the publish request."""
    pass


def validate_semver(version: str) -> tuple[int, int, int]:
    """Validate strict SemVer (X.Y.Z, optional leading 'v') and return (major, minor, patch)."""
    if not isinstance(version, str):
        raise ValueError("version must be a string")
    m = STRICT_SEMVER_RE.fullmatch(version.strip())
    if not m:
        raise ValueError(f"invalid SemVer {version!r}: expected X.Y.Z")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def normalize_semver(version: str) -> str:
    """Normalize a version string to X.Y.Z without leading 'v'."""
    major, minor, patch = validate_semver(version)
    return f"{major}.{minor}.{patch}"


@dataclass
class ReleaseItem:
    project: str
    number: int
    pr: Optional[int] = None
    title: str = ""
    summary: str = ""
    merge_sha: str = ""
    labels: List[str] = field(default_factory=list)
    shipped_at: str = ""
    release_id: Optional[int] = None

    def is_maintenance(self) -> bool:
        """type:chore or maintenance-pass work."""
        for l in self.labels:
            if l == "type:chore" or l.startswith("pass:") or l == "maintenance":
                return True
        return False

    def is_feature(self) -> bool:
        if self.is_maintenance():
            return False
        return "type:feature" in self.labels

    def is_fix(self) -> bool:
        if self.is_maintenance() or self.is_feature():
            return False
        return "type:bug" in self.labels

    def is_other(self) -> bool:
        """Other user-facing changes (e.g. type:goal, type:uat, untyped)."""
        return not (self.is_maintenance() or self.is_feature() or self.is_fix())

    def formatted_line(self) -> str:
        """Human-readable markdown line describing the item."""
        title = (self.title or "").strip()
        summary = (self.summary or "").strip()
        if title and summary and title.lower() != summary.lower():
            desc = f"{title}: {summary}"
        elif summary:
            desc = summary
        elif title:
            desc = title
        else:
            desc = f"Issue #{self.number}"

        ref = f"#{self.number}, PR #{self.pr}" if self.pr and self.pr != self.number else f"#{self.number}"
        return f"- {desc} ({ref})"


def _to_release_item(it: Any) -> ReleaseItem:
    if isinstance(it, ReleaseItem):
        return it
    raw_labels = row_get(it, "labels", [])
    if isinstance(raw_labels, str):
        try:
            labels = json.loads(raw_labels)
        except Exception:
            labels = []
    elif isinstance(raw_labels, (list, tuple)):
        labels = list(raw_labels)
    else:
        labels = []

    return ReleaseItem(
        project=row_get(it, "project", ""),
        number=row_get(it, "number", 0),
        pr=row_get(it, "pr", None),
        title=row_get(it, "title", ""),
        summary=row_get(it, "summary", ""),
        merge_sha=row_get(it, "merge_sha", ""),
        labels=labels,
        shipped_at=row_get(it, "shipped_at", ""),
        release_id=row_get(it, "release_id", None),
    )


@dataclass
class StructuredNotes:
    features: List[ReleaseItem] = field(default_factory=list)
    fixes: List[ReleaseItem] = field(default_factory=list)
    other: List[ReleaseItem] = field(default_factory=list)
    maintenance: List[ReleaseItem] = field(default_factory=list)

    @property
    def main_summary(self) -> str:
        """Features and fixes in the main summary."""
        parts = []
        if self.features:
            parts.append("### Features\n" + "\n".join(it.formatted_line() for it in self.features))
        if self.fixes:
            parts.append("### Fixes\n" + "\n".join(it.formatted_line() for it in self.fixes))
        return "\n\n".join(parts)

    @property
    def other_section(self) -> str:
        """Other user-facing changes in an additional section."""
        if not self.other:
            return ""
        return "### Other changes\n" + "\n".join(it.formatted_line() for it in self.other)

    @property
    def maintenance_section(self) -> str:
        """Maintenance items in standard section format."""
        if not self.maintenance:
            return ""
        return "### Maintenance\n" + "\n".join(it.formatted_line() for it in self.maintenance)

    @property
    def maintenance_details(self) -> str:
        """Maintenance items wrapped in a collapsible details block."""
        if not self.maintenance:
            return ""
        lines = "\n".join(it.formatted_line() for it in self.maintenance)
        return f"<details>\n<summary>Maintenance details ({len(self.maintenance)})</summary>\n\n{lines}\n</details>"

    @property
    def summary(self) -> str:
        """Main summary plus other user-facing changes (maintenance excluded by default)."""
        sections = []
        if self.main_summary:
            sections.append(self.main_summary)
        if self.other_section:
            sections.append(self.other_section)
        return "\n\n".join(sections)

    @property
    def expanded_notes(self) -> str:
        """Main summary, other section, and collapsible maintenance details."""
        sections = []
        if self.main_summary:
            sections.append(self.main_summary)
        if self.other_section:
            sections.append(self.other_section)
        if self.maintenance_details:
            sections.append(self.maintenance_details)
        return "\n\n".join(sections)

    def render(self, include_maintenance: bool = False, collapsed_maintenance: bool = True) -> str:
        sections = []
        if self.main_summary:
            sections.append(self.main_summary)
        if self.other_section:
            sections.append(self.other_section)
        if include_maintenance and self.maintenance:
            if collapsed_maintenance:
                sections.append(self.maintenance_details)
            else:
                sections.append(self.maintenance_section)
        return "\n\n".join(sections)

    def __str__(self) -> str:
        return self.render(include_maintenance=False)


def synthesize_notes(items: List[Any]) -> StructuredNotes:
    """Group items into structured sections: features, fixes, other, maintenance."""
    notes = StructuredNotes()
    for raw in items:
        item = _to_release_item(raw)
        if item.is_maintenance():
            notes.maintenance.append(item)
        elif item.is_feature():
            notes.features.append(item)
        elif item.is_fix():
            notes.fixes.append(item)
        else:
            notes.other.append(item)
    return notes


def parse_semver(v: Optional[str]) -> Optional[tuple[int, int, int]]:
    if not v:
        return None
    m = SEMVER_RE.match(v.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def propose_next_version(last_version: Optional[str], items_or_labels: Any) -> str:
    """Propose the next SemVer from the last release and included labels.

    - An initial project release proposes 0.1.0.
    - Feature work ('type:feature') proposes minor: X.(Y+1).0.
    - Fixes and other non-breaking work propose patch: X.Y.(Z+1).
    - A major version remains an explicit operator selection and is never proposed automatically.
    """
    if not last_version:
        return "0.1.0"

    parsed = parse_semver(last_version)
    if not parsed:
        return "0.1.0"
    major, minor, patch = parsed

    has_feature = False
    if items_or_labels:
        for entry in items_or_labels:
            if isinstance(entry, str):
                if entry == "type:feature":
                    has_feature = True
                    break
            else:
                item = _to_release_item(entry)
                if item.is_feature():
                    has_feature = True
                    break

    if has_feature:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def check_readiness(items: List[Any], now: Optional[datetime] = None) -> tuple[bool, List[str]]:
    """Check if a draft is ready for release.

    Marked suggested when:
    - it has at least 5 unreleased items, OR
    - its oldest item is at least 7 days old.

    This is advisory only and never publishes by itself.
    """
    if not items:
        return False, []
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    reasons = []
    if len(items) >= 5:
        reasons.append(f"{len(items)} unreleased items (threshold: 5)")

    oldest_dt = None
    for raw in items:
        item = _to_release_item(raw)
        if item.shipped_at:
            dt = parse(item.shipped_at)
            if dt:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if oldest_dt is None or dt < oldest_dt:
                    oldest_dt = dt

    if oldest_dt is not None:
        age = now - oldest_dt
        if age >= timedelta(days=7):
            days = age.days
            reasons.append(f"oldest item is {days} days old (threshold: 7 days)")

    return len(reasons) > 0, reasons


@dataclass
class ReleaseDraft:
    project: str
    items: List[ReleaseItem]
    notes: StructuredNotes
    proposed_version: str
    is_suggested: bool
    readiness_reasons: List[str]
    checkpoint_sha: Optional[str]
    last_version: Optional[str]

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def oldest_shipped_at(self) -> Optional[datetime]:
        for it in self.items:
            if it.shipped_at:
                dt = parse(it.shipped_at)
                if dt:
                    return dt
        return None

    @property
    def newest_shipped_at(self) -> Optional[datetime]:
        for it in reversed(self.items):
            if it.shipped_at:
                dt = parse(it.shipped_at)
                if dt:
                    return dt
        return None


def get_draft(led: Any, project: str, now: Optional[datetime] = None) -> ReleaseDraft:
    """Compute the rolling unreleased draft for a project."""
    rows = led.unreleased_items(project)
    items = [_to_release_item(r) for r in rows]
    notes = synthesize_notes(items)
    last_rel = led.latest_release(project)
    last_version = last_rel["version"] if last_rel else None
    proposed = propose_next_version(last_version, items)
    current_time = now or (led.now() if hasattr(led, "now") else datetime.now(timezone.utc))
    is_suggested, reasons = check_readiness(items, current_time)

    checkpoint_sha = None
    for it in reversed(items):
        if it.merge_sha:
            checkpoint_sha = it.merge_sha
            break
    if not checkpoint_sha and last_rel:
        checkpoint_sha = last_rel["checkpoint_sha"]

    return ReleaseDraft(
        project=project,
        items=items,
        notes=notes,
        proposed_version=proposed,
        is_suggested=is_suggested,
        readiness_reasons=reasons,
        checkpoint_sha=checkpoint_sha,
        last_version=last_version,
    )


def create_release(led: Any, project: str, version: Optional[str] = None,
                   checkpoint_sha: Optional[str] = None, notes: Optional[str] = None,
                   remote_url: Optional[str] = None, state: str = "published",
                   published_at: Optional[str] = None, item_numbers: Optional[List[int]] = None):
    """Complete a release from the unreleased draft."""
    draft = get_draft(led, project)
    ver = version or draft.proposed_version
    sha = checkpoint_sha or draft.checkpoint_sha
    if notes is None:
        rendered_notes = draft.notes.expanded_notes or draft.notes.summary
    else:
        rendered_notes = notes

    return led.create_release(
        project=project,
        version=ver,
        checkpoint_sha=sha,
        notes=rendered_notes,
        state=state,
        published_at=published_at,
        remote_url=remote_url,
        item_numbers=item_numbers,
    )


def get_release(led: Any, project: str, version_or_id: Any):
    if isinstance(version_or_id, int):
        return led.get_release(project, release_id=version_or_id)
    return led.get_release(project, version=str(version_or_id))


def list_releases(led: Any, project: str) -> List[Any]:
    return led.list_releases(project)


def get_release_items(led: Any, project: str, version_or_id: Any) -> List[ReleaseItem]:
    rel = get_release(led, project, version_or_id)
    if not rel:
        return []
    rows = led.release_items_for_release(rel["id"])
    return [_to_release_item(r) for r in rows]


def format_preview(draft: ReleaseDraft, version: Optional[str] = None,
                   checkpoint_sha: Optional[str] = None) -> str:
    """Format the human-readable release preview."""
    sha = checkpoint_sha or draft.checkpoint_sha or "unknown"
    suggested_str = f"yes ({'; '.join(draft.readiness_reasons)})" if draft.is_suggested else "no"

    lines = [f"Release preview for {draft.project}:"]
    if version:
        norm_v = normalize_semver(version)
        lines.append(f"  Selected version:  {norm_v}")
        lines.append(f"  Proposed version:  {draft.proposed_version}")
    else:
        lines.append(f"  Proposed version:  {draft.proposed_version}")
    lines.append(f"  Checkpoint SHA:    {sha}")
    lines.append(f"  Item count:        {draft.count}")
    lines.append(f"  Release suggested: {suggested_str}")
    lines.append("")
    lines.append("Notes:")

    notes_rendered = draft.notes.render(include_maintenance=True, collapsed_maintenance=True)
    if notes_rendered.strip():
        lines.append(notes_rendered)
    else:
        lines.append("  (no unreleased changes)")
    return "\n".join(lines)


def publish_release(led: Any, gh: Any, project: str, version: str,
                    checkpoint_sha: str, notes: Optional[str] = None,
                    item_numbers: Optional[List[int]] = None) -> dict:
    """Publish a release to GitHub and atomically record it locally.

    Validates strict SemVer and requires it to be greater than the latest
    recorded version (unless idempotently reconciling an existing release).
    Inspects remote release/tag:
    - If matching remote release exists at same tag and SHA with matching notes,
      finishes the local record and returns status='reconciled'.
    - If tag, SHA, or notes conflict, raises ReleaseConflictError and leaves
      the draft unsealed.
    - Otherwise publishes tag vX.Y.Z and GitHub Release against checkpoint_sha,
      and atomically seals the included draft items into the local release record.
    """
    if not checkpoint_sha:
        raise ValueError("checkpoint_sha is required to publish a release")

    norm_ver = normalize_semver(version)
    parsed = validate_semver(norm_ver)

    local_rel = led.get_release(project, version=norm_ver)
    latest_rel = led.latest_release(project)

    if latest_rel:
        last_parsed = parse_semver(latest_rel["version"])
        if last_parsed:
            if parsed < last_parsed:
                raise ValueError(
                    f"version {norm_ver} must be greater than latest recorded version {latest_rel['version']}"
                )
            if parsed == last_parsed and not local_rel:
                raise ValueError(
                    f"version {norm_ver} must be greater than latest recorded version {latest_rel['version']}"
                )

    draft = get_draft(led, project)
    if notes is not None:
        rendered_notes = notes
    elif local_rel and row_get(local_rel, "notes"):
        rendered_notes = row_get(local_rel, "notes")
    else:
        rendered_notes = draft.notes.render(include_maintenance=True, collapsed_maintenance=True)

    if item_numbers is None:
        previewed_item_numbers = [it.number for it in draft.items]
    else:
        previewed_item_numbers = list(item_numbers)

    tag = f"v{norm_ver}"
    remote_rel = gh.get_release(tag)
    tag_sha = gh.get_tag_sha(tag)

    if remote_rel is not None:
        remote_tag = remote_rel.get("tagName")
        remote_target = remote_rel.get("targetCommitish")
        remote_body = (remote_rel.get("body") or "").replace("\r\n", "\n").strip()
        expected_body = rendered_notes.replace("\r\n", "\n").strip()

        if remote_tag != tag:
            raise ReleaseConflictError(
                f"remote release tag mismatch: {remote_tag!r} != {tag!r}"
            )
        sha_matches = (remote_target == checkpoint_sha) or (tag_sha == checkpoint_sha)
        if not sha_matches:
            raise ReleaseConflictError(
                f"remote release {tag} exists at target SHA {remote_target or tag_sha}, "
                f"conflicting with previewed SHA {checkpoint_sha}"
            )
        if remote_body != expected_body:
            raise ReleaseConflictError(
                f"remote release {tag} exists but its notes conflict with previewed notes"
            )

        if not local_rel:
            local_rel = create_release(
                led, project, version=norm_ver, checkpoint_sha=checkpoint_sha,
                notes=rendered_notes, remote_url=remote_rel.get("url"),
                item_numbers=previewed_item_numbers
            )
        url = row_get(local_rel, "remote_url") or remote_rel.get("url")
        led.event("release", project=project, detail=f"{tag} reconciled: {url}")
        return {
            "status": "reconciled",
            "release": local_rel,
            "url": url,
        }

    if tag_sha is not None and tag_sha != checkpoint_sha:
        raise ReleaseConflictError(
            f"remote tag {tag} already exists at SHA {tag_sha}, conflicting with previewed SHA {checkpoint_sha}"
        )

    if local_rel and row_get(local_rel, "checkpoint_sha") != checkpoint_sha:
        raise ReleaseConflictError(
            f"local release {norm_ver} already recorded at SHA {row_get(local_rel, 'checkpoint_sha')}, "
            f"conflicting with previewed SHA {checkpoint_sha}"
        )

    url = gh.release_create(tag=tag, target=checkpoint_sha, title=tag, notes=rendered_notes)

    if not local_rel:
        local_rel = create_release(
            led, project, version=norm_ver, checkpoint_sha=checkpoint_sha,
            notes=rendered_notes, remote_url=url, item_numbers=previewed_item_numbers
        )
    led.event("release", project=project, detail=f"{tag} published: {url}")
    return {
        "status": "published",
        "release": local_rel,
        "url": url,
    }


def semver_options(last_version: Optional[str], proposed_version: str) -> dict[str, str]:
    """Calculate SemVer choices (proposed, patch, minor, major) for operator selection."""
    if not last_version:
        return {
            "proposed": proposed_version,
            "patch": "0.1.1",
            "minor": "0.2.0",
            "major": "1.0.0",
        }
    parsed = parse_semver(last_version)
    if not parsed:
        return {
            "proposed": proposed_version,
            "patch": "0.1.1",
            "minor": "0.2.0",
            "major": "1.0.0",
        }
    maj, min_, pat = parsed
    return {
        "proposed": proposed_version,
        "patch": f"{maj}.{min_}.{pat + 1}",
        "minor": f"{maj}.{min_ + 1}.0",
        "major": f"{maj + 1}.0.0",
    }


def build_feed(led: Any, project: str, limit: int = 20) -> dict[str, Any]:
    """Build the What's New JSON feed for a project (schema v1, DESIGN D31 / #358).

    Strictly read-only, project-scoped, privacy-conscious:
    - Backed only by published releases.
    - Ordered newest first (descending by published_at / ID).
    - Excludes sensitive operational data (prompts, logs, comments, credentials, checklist items).
    - Items contain only number, pr, title, summary.
    - Sections contain features, fixes, other; maintenance list separated.
    - Empty state: HTTP 200 with "releases": [].
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    rows = led.list_releases(project)
    published_rows = [r for r in rows if r["state"] == "published"][:limit]

    now_dt = led.now() if hasattr(led, "now") else datetime.now(timezone.utc)
    feed_releases = []

    for r in published_rows:
        item_rows = led.release_items_for_release(r["id"])
        notes = synthesize_notes(item_rows)

        def _feed_item(it: ReleaseItem) -> dict[str, Any]:
            return {
                "number": it.number,
                "pr": it.pr,
                "title": it.title,
                "summary": it.summary,
            }

        rel_obj = {
            "version": normalize_semver(r["version"]),
            "checkpoint_sha": r["checkpoint_sha"],
            "published_at": (r["published_at"] or "").replace("+00:00", "Z"),
            "remote_url": r["remote_url"] or None,
            "sections": {
                "features": [_feed_item(it) for it in notes.features],
                "fixes": [_feed_item(it) for it in notes.fixes],
                "other": [_feed_item(it) for it in notes.other],
            },
            "maintenance": [_feed_item(it) for it in notes.maintenance],
        }
        feed_releases.append(rel_obj)

    return {
        "schema_version": 1,
        "project": project,
        "generated_at": (iso(now_dt) or "").replace("+00:00", "Z"),
        "releases": feed_releases,
    }

