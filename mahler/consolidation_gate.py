"""PR/CI gate for lesson consolidation (DESIGN D44).

    python -m mahler.consolidation_gate --base <ref> --head <ref> [--repo DIR]

Reads lessons, archive and snapshots from the committed trees of both refs and
runs lessons.check_consolidation. Exit 0 accepted, 1 rejected, 2 on any
ref/git error (fail closed)."""
import argparse
import sys

from . import lessons


def _tree(repo, ref, prefix):
    """{path under prefix: bytes} for every blob at `ref` below `prefix`."""
    listing = lessons._git_bytes(repo, "ls-tree", "-r", "-z", ref, "--", prefix)
    out = {}
    for entry in filter(None, listing.split(b"\0")):
        meta, _, path = entry.partition(b"\t")
        mode, _, sha = meta.split()[:3]
        rel = path.decode("utf-8", "replace")[len(prefix) + 1:]
        link = mode in (b"120000", b"160000")
        out[rel] = b"" if link else lessons._git_bytes(repo, "cat-file", "blob", sha.decode())
        if link:
            out[rel + " (link)"] = b""
    return out


def _nest(flat, depth):
    """Group flat paths by their first `depth` segments -> nested dicts."""
    out = {}
    for rel, data in flat.items():
        parts = rel.split("/")
        if len(parts) <= depth:
            continue
        node = out
        for p in parts[:depth]:
            node = node.setdefault(p, {})
        node["/".join(parts[depth:])] = data
    return out


def run(repo, base_ref, head_ref):
    base = lessons.read_base(repo, base_ref)
    head = lessons.read_base(repo, head_ref)
    arch = _tree(repo, head_ref, lessons.ARCHIVE_DIR)
    snaps = _nest({k[len("_snapshots/"):]: v for k, v in arch.items()
                   if k.startswith("_snapshots/")}, 2)
    archived = _nest({k: v for k, v in arch.items()
                      if not k.startswith("_snapshots/")}, 1)
    return lessons.check_consolidation(base, head, snaps, archived)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="consolidation_gate")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    try:
        problems = run(a.repo, a.base, a.head)
    except Exception as e:  # fail closed
        print(f"consolidation gate error: {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"consolidation gate: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
