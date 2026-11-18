#!/usr/bin/env python3
"""Manifest + fingerprint + incremental-build support.

A manifest pins what a build saw: per-file mtime/size/sha256, the
source commit, and the sha256 of the graph bytes. Incremental builds
compare the working tree against the manifest with an mtime/size fast
path and fall back to hashes, so unchanged trees reuse the previous
graph bytes (deterministic) and changed trees list exactly what moved.

Stdlib only. Manifests record paths and hashes, never file content
and never secret values.
"""

import hashlib
import json
import os
import subprocess

MANIFEST_VERSION = 1


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def git_commit(root):
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def walk_files(root):
    """Sorted relpaths of all regular files under root."""
    rels = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.isfile(full) and not os.path.islink(full):
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                rels.append(rel)
    return sorted(rels)


def tracked_files(root):
    """Set of git-tracked rel paths, or None when not a git repo."""
    try:
        probe = subprocess.run(["git", "-C", root, "rev-parse",
                                "--git-dir"],
                               capture_output=True, text=True, timeout=10)
        if probe.returncode != 0:
            return None
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return set(p for p in out.stdout.split("\0") if p)
    except Exception:
        return None


def candidate_rels(root):
    """Relpaths feeding the graph: tracked only in git repos."""
    tracked = tracked_files(root)
    if tracked is None:
        return walk_files(root)
    rels = []
    for rel in sorted(tracked):
        full = os.path.join(root, rel)
        if os.path.isfile(full) and not os.path.islink(full):
            rels.append(rel)
    return rels


def fingerprint(root, rels):
    """Per-file fingerprint entries (sorted by path)."""
    entries = []
    for rel in sorted(rels):
        full = os.path.join(root, rel)
        try:
            st = os.stat(full)
            entries.append({"path": rel,
                            "sha256": sha256_file(full),
                            "mtime": st.st_mtime_ns,
                            "size": st.st_size})
        except OSError:
            continue
    return entries


def load_manifest(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict) or doc.get("manifest_version") != 1:
        raise ValueError("not a project-graph manifest v1: %s" % path)
    return doc


def save_manifest(path, doc):
    blob = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(blob)
    return blob


def rel_inside(root, path):
    """Relpath of path under root, or None when outside."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return rel.replace(os.sep, "/")


def build_manifest(root, project, graph_blob, generated_at,
                   source_commit, counts, version="", exclude=()):
    """Build the manifest dict for a fresh graph build.

    exclude: relpaths (e.g. the graph + manifest outputs themselves)
    left out of the fingerprint so outputs never dirty the next
    incremental check.
    """
    root = os.path.abspath(root)
    excluded = sorted(set(exclude))
    rels = [r for r in candidate_rels(root) if r not in excluded]
    return {
        "manifest_version": MANIFEST_VERSION,
        "project": project,
        "generator": "project-graph-template-core",
        "version": version,
        "source_commit": source_commit,
        "generated_at": generated_at,
        "graph_sha256": sha256_bytes(graph_blob),
        "counts": counts,
        "outputs": excluded,
        "files": fingerprint(root, rels),
    }


def changed_files(old, root):
    """Sorted relpaths that changed since the manifest, via mtime+hash.

    mtime/size equality is the fast path (assumed unchanged); any
    mtime/size difference falls back to a sha256 comparison, so
    timestamp-only noise never forces a rebuild.
    """
    recorded = {f["path"]: f for f in old.get("files", [])}
    skipped = set(old.get("outputs", []))
    current = {}
    for rel in candidate_rels(os.path.abspath(root)):
        if rel in skipped:
            continue
        full = os.path.join(os.path.abspath(root), rel)
        try:
            st = os.stat(full)
        except OSError:
            continue
        current[rel] = (st.st_mtime_ns, st.st_size)
    changed = sorted(set(recorded) - set(current))  # removed
    changed += sorted(set(current) - set(recorded))  # added
    for rel in sorted(set(current) & set(recorded)):
        mtime, size = current[rel]
        rec = recorded[rel]
        if mtime == rec.get("mtime") and size == rec.get("size"):
            continue
        try:
            digest = sha256_file(os.path.join(os.path.abspath(root), rel))
        except OSError:
            changed.append(rel)
            continue
        if digest != rec.get("sha256"):
            changed.append(rel)
    return sorted(set(changed))


def needs_rebuild(old, root):
    """(rebuild_needed, changed_relpaths) against a manifest."""
    changed = changed_files(old, root)
    return (len(changed) > 0, changed)
