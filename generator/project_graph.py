#!/usr/bin/env python3
"""Template project-graph generator core (stdlib only).

Scans one project checkout and writes a JSON graph using the OCI
codebase-graph schema (keys: nodes/edges/sources/gaps/drift/
generated_at/source_commit; node {id,type,source,attrs};
edge {src,dst,rel,evidence}; sources [{path,sha256}]).

Node id form is "<type>:<relpath>" (forward slashes). All lists are
sorted, so output is deterministic for identical input. Additive
schema rule: this core never removes or renames graph keys.

Configuration (all optional, JSON file via --config):
  {"project": "name", "version": "1.2.3",
   "extractors": {"lua": false},
   "skip_dirs": ["extra-dir"], "skip_suffixes": [".gen"],
   "secret_globs": ["*internal*"]}
Extractor names are the registry keys in extractors.py; unknown names
are ignored. Without --config the core behaves exactly like the
reference scripts/py/project_graph.py (same node/edge sets).

Skipped (never nodes, sources, or evidence content): .git/,
node_modules/, dist/, build/, __pycache__/, *.c4z, *.pyc, binaries,
and secret-shaped files (.env*, *.dec.*, salt.txt, *.pem, *.key,
id_*, *secret*, *credential*). Skips are recorded in gaps by NAME
only. File content never enters attrs (paths/line counts only); the
secret-name scan below records identifier names only, never values.

Run with: python3 -I project_graph.py <verb>
  build --root <checkout> --project <name> [--out FILE]
      [--fixed-time 2026-01-01T00:00:00Z] [--config FILE]
      [--manifest FILE] [--incremental]
  manifest --root <checkout> --project <name> --graph FILE
      --out FILE [--fixed-time ...] [--version V]
  diagnostics --graph FILE [--old FILE]
  --selftest    offline fixture checks (imports, edges, skips,
                byte-identical rebuild, stdlib-only import check)
"""

import argparse
import ast
import datetime
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extractors import (  # noqa: E402
    EXTRACTORS, Ctx, dispatch_key, enabled_extractors, lang_for,
    lua_index,
)
from extractors import first_lineno as _first_lineno  # noqa: E402
import graph_symbols as _symbols  # noqa: E402 (stdlib-only outline I1)
from manifest import (  # noqa: E402
    build_manifest, candidate_rels, git_commit, load_manifest,
    needs_rebuild, rel_inside, save_manifest, sha256_bytes, sha256_file,
)
from diagnostics import render as render_diagnostics  # noqa: E402

SCHEMA_KEYS = ["nodes", "edges", "sources", "gaps", "drift",
               "generated_at", "source_commit"]

ALLOWED_TYPES = ["lua", "driver-xml", "manifest", "script", "workflow",
                 "doc", "page", "asset", "config", "vendor", "symbol"]

SKIP_DIRS = {".git", "node_modules", "dist", "build", "__pycache__",
             ".venv", "venv"}

SKIP_SUFFIXES = (".c4z", ".pyc", ".pyo", ".pdf", ".rtf", ".gif",
                 ".dec", ".pem", ".key")

SECRET_BASENAMES = {"salt.txt"}

SECRET_GLOBS = (".env*", "*.dec.*", "*.pem", "*.key", "id_*",
                "*secret*", "*credential*")

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico",
                  ".woff", ".woff2", ".ttf", ".eot")

# Identifier tokens shaped like secret names. Single common words only
# count when all-lower or all-upper, so prose like "Salt Lake City"
# ("Salt") does not match but code names ($salt, SALT) do. Only the
# token is recorded; values never are.
SECRET_TOKEN_RE = re.compile(r"\$?([A-Za-z_][A-Za-z0-9_]{1,64})")
SECRET_SUBSTRS = ("password", "passwd", "secret", "salt", "token",
                  "apikey", "api_key", "api-key", "private_key",
                  "privatekey", "bearer")
SECRET_SINGLE = ("password", "passwd", "secret", "salt", "token",
                 "bearer", "apikey")

first_lineno = _first_lineno


def default_config():
    return {}


def load_config(path):
    """Load a JSON config file; {} when path is empty/None."""
    if not path:
        return default_config()
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise ValueError("config must be a JSON object: %s" % path)
    return doc


def is_secret_token(tok):
    """True for code-identifier-shaped secret names (never values)."""
    low = tok.lower()
    if not any(s in low for s in SECRET_SUBSTRS):
        return False
    if "_" in tok or "-" in tok:
        return True
    return low in SECRET_SINGLE and (tok == low or tok == tok.upper())


def secret_names_in(text):
    """Return sorted distinct secret-shaped identifier names (no values)."""
    found = set()
    for m in SECRET_TOKEN_RE.finditer(text):
        tok = m.group(1)
        if is_secret_token(tok):
            found.add(tok)
    return sorted(found)


def cfg_skip_dirs(cfg):
    return SKIP_DIRS | set((cfg or {}).get("skip_dirs", []) or [])


def cfg_skip_suffixes(cfg):
    extra = tuple((cfg or {}).get("skip_suffixes", []) or [])
    return SKIP_SUFFIXES + extra


def cfg_secret_globs(cfg):
    extra = tuple((cfg or {}).get("secret_globs", []) or [])
    return SECRET_GLOBS + extra


def classify(rel, cfg=None):
    """Return node type for a repo-relative path, or None to skip."""
    skip_dirs = cfg_skip_dirs(cfg)
    parts = rel.split("/")
    if any(p in skip_dirs for p in parts):
        return None
    base = parts[-1]
    low = base.lower()
    if low in SECRET_BASENAMES or any(
            fnmatch.fnmatch(low, g) for g in cfg_secret_globs(cfg)):
        return None
    if low.endswith(cfg_skip_suffixes(cfg)):
        return None
    if base == "driver.xml":
        return "driver-xml"
    if low.endswith(".c4zproj"):
        return "manifest"
    if base in ("action.yml", "action.yaml", "Dockerfile"):
        return "manifest"
    if parts[0] == ".github" and len(parts) > 2 and parts[1] == "workflows" \
            and low.endswith((".yml", ".yaml")):
        return "workflow"
    if low.endswith(".lua"):
        return "lua"
    if low.endswith((".sh", ".py", ".ps1", ".go")):
        return "script"
    if low.endswith(".md"):
        return "doc"
    if low.endswith((".html", ".htm", ".php")):
        return "page"
    if low.endswith((".css", ".js")) or low.endswith(IMAGE_SUFFIXES):
        return "asset"
    if base in (".htaccess", ".user.ini", "robots.txt", "sitemap.xml") \
            or low.endswith((".json", ".yaml", ".yml", ".xml", ".txt",
                              ".htaccess", ".userini")):
        return "config"
    if "." not in base and base == "squish":
        return "vendor"
    return "config"


def is_binary(path):
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return True


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def nowstamp(fixed):
    if fixed:
        return fixed
    return datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Collection: classify, dedupe identical blobs, build nodes.
# --------------------------------------------------------------------------

# Text-scanned types for the secret-name pass. Pages and assets are
# structure-only (no content scan: no page text, contact or API
# values may leak into gaps).
SCANNED_TYPES = ("lua", "driver-xml", "manifest", "script", "workflow",
                 "doc", "config")


def collect(root, cfg=None):
    """Walk root (tracked-only in git repos); return (nodes, alias, texts...).

    nodes: id -> node. alias: relpath -> node id (dupes point at the
    canonical node). texts: node id -> decoded text for text files.
    skipped: [(rel, reason)]. dupes: [(dup_rel, canonical_rel)].
    """
    nodes = {}
    alias = {}
    texts = {}
    skipped = []
    dupes = []
    seen_hash = {}
    for rel in candidate_rels(root):
        ntype = classify(rel, cfg)
        if ntype is None:
            skipped.append((rel, "secret-shaped or generated"))
            continue
        full = os.path.join(root, rel)
        binary = is_binary(full)
        if binary and ntype not in ("asset", "vendor"):
            skipped.append((rel, "binary"))
            continue
        digest = sha256_file(full)
        if digest in seen_hash:
            canon = seen_hash[digest]
            dupes.append((rel, canon))
            continue
        seen_hash[digest] = rel
        nid = "%s:%s" % (ntype, rel)
        alias[rel] = nid
        try:
            size = os.path.getsize(full)
        except OSError:
            size = -1
        text = None
        if not binary and ntype != "vendor":
            try:
                text = read_text(full)
            except OSError:
                skipped.append((rel, "unreadable"))
                del alias[rel]
                continue
            texts[nid] = text
        lang = lang_for(rel)
        purpose = ""
        if text is not None:
            try:
                purpose = _symbols.purpose(rel, text) or ""
            except Exception:
                purpose = ""
        if ntype in ("asset", "vendor"):
            attrs = {"path": rel, "bytes": size,
                     "purpose": purpose[:120], "lang": lang}
        else:
            attrs = {"path": rel,
                     "lines": text.count("\n") + 1 if text is not None
                     else 0,
                     "purpose": purpose[:120], "lang": lang}
        nodes[nid] = {"id": nid, "type": ntype, "source": rel,
                      "attrs": attrs}
    # Point dupes at their canonical node id.
    for dup_rel, canon_rel in dupes:
        alias[dup_rel] = alias[canon_rel]
    return nodes, alias, texts, skipped, dupes


# --------------------------------------------------------------------------
# Edge extraction (registry dispatch; per-type logic in extractors.py).
# --------------------------------------------------------------------------

def extract_edges(nodes, alias, texts, cfg=None):
    """Return (edges, unresolved). Edges are (src, dst, rel, evidence)."""
    ctx = Ctx(nodes, alias, texts, lua_index(nodes),
              _basenames(alias))
    enabled = set(enabled_extractors(cfg))
    for nid, text in sorted(texts.items()):
        ntype, _, rel = nid.partition(":")
        key = dispatch_key(ntype, rel)
        if key is None or key not in enabled:
            continue
        fn = EXTRACTORS.get(key)
        if fn is None:
            continue
        src_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        fn(ctx, nid, rel, text, text.splitlines(), src_dir)
    return sorted(ctx.edges), sorted(set(ctx.unresolved))


def _basenames(alias):
    basenames = {}
    for rel in alias:
        basenames.setdefault(rel.split("/")[-1], []).append(rel)
    return basenames


# --------------------------------------------------------------------------
# Graph assembly.
# --------------------------------------------------------------------------

def build_graph(root, project, fixed_time=None, cfg=None):
    """Build the graph dict. Deterministic for identical input."""
    root = os.path.abspath(root)
    nodes, alias, texts, skipped, dupes = collect(root, cfg)
    edges, unresolved = extract_edges(nodes, alias, texts, cfg)

    gaps = []
    if not nodes and not skipped and not dupes:
        gaps.append({
            "id": "empty-checkout",
            "item": "checkout has no tracked files (repo has no commits "
            "yet); emitting gaps-only graph",
            "evidence": "HEAD:1"})
    for rel, reason in sorted(skipped):
        gaps.append({
            "id": "skip:%s" % rel,
            "item": "skipped (%s); name only, content not ingested" % reason,
            "evidence": "%s:1" % rel})
    for dup_rel, canon_rel in sorted(dupes):
        gaps.append({
            "id": "dedup:%s" % dup_rel,
            "item": "identical copy of %s; ingested once" % canon_rel,
            "evidence": "%s:1" % dup_rel})
    for rel, ref in sorted(unresolved)[:50]:
        gaps.append({
            "id": "unresolved:%s:%s" % (
                rel, re.sub(r"[^a-z0-9]+", "-", ref.lower()).strip("-")),
            "item": "%s references '%s' which resolves to no tracked "
                    "file" % (rel, ref),
            "evidence": "%s:1" % rel})
    # Secret-shaped identifier names only; values never recorded.
    for nid in sorted(texts):
        ntype, _, rel = nid.partition(":")
        if ntype not in SCANNED_TYPES:
            continue
        names = secret_names_in(texts[nid])[:8]
        for name in names:
            gaps.append({
                "id": "secret-name:%s:%s" % (rel, name.lower()),
                "item": "%s mentions secret-shaped name '%s' "
                        "(value not recorded)" % (rel, name),
                "evidence": "%s:%d" % (
                    rel, first_lineno(texts[nid], name))})
    gaps = sorted(gaps, key=lambda g: g["id"])

    # Committed outline layer (Gate 1 storage C hybrid, S4): one symbol
    # node per outline entry (stdlib graph_symbols outline, no line
    # numbers in the committed graph) + a defines edge file -> symbol.
    # Deterministic: outline() sorts by (line_start, name); symbol ids
    # and edges are sorted below. Secrets: signatures are literal-scrubbed
    # by graph_symbols; values never enter attrs.
    outline_edges = []
    for nid in sorted(texts):
        _, _, rel = nid.partition(":")
        try:
            entries = _symbols.outline(rel, texts[nid])
        except Exception:
            entries = []
        seen = set()
        for entry in entries or []:
            qualname = entry.get("qualname") or entry.get("name") or ""
            if not qualname or qualname in seen:
                continue
            seen.add(qualname)
            sym_id = "symbol:%s#%s" % (rel, qualname)
            if sym_id not in nodes:
                nodes[sym_id] = {
                    "id": sym_id,
                    "type": "symbol",
                    "source": rel,
                    "attrs": {
                        "kind": entry.get("kind", ""),
                        "parent": entry.get("parent", ""),
                        "signature": (entry.get("signature", "") or "")[:120],
                    },
                }
            outline_edges.append((nid, sym_id, "defines", "%s:1" % rel))
    edges = sorted(set(edges) | set(outline_edges))

    node_list = sorted(nodes.values(), key=lambda n: n["id"])
    edge_list = [{"src": s, "dst": d, "rel": r, "evidence": e}
                 for s, d, r, e in edges]
    # Every ingested path (canonical and dupe) is fingerprinted.
    # Symbol nodes share their file's source and are never fingerprinted
    # on their own (their id carries a #qualname fragment, not a path).
    by_node = {}
    for rel, nid in alias.items():
        by_node.setdefault(nid, []).append(rel)
    all_rels = sorted(alias.keys())
    sources = [{"path": rel,
                "sha256": sha256_file(os.path.join(root, rel))}
               for rel in all_rels]

    return {
        "nodes": node_list,
        "edges": edge_list,
        "sources": sources,
        "gaps": gaps,
        "drift": [],
        "generated_at": nowstamp(fixed_time),
        "source_commit": git_commit(root),
    }


def _graph_blob(doc):
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")


def cmd_build(root, project, out, fixed_time, cfg=None,
              manifest_path=None, incremental=False):
    if incremental and manifest_path and os.path.exists(manifest_path):
        try:
            old = load_manifest(manifest_path)
            rebuild, changed = needs_rebuild(old, root)
            if not rebuild:
                if out and os.path.exists(out):
                    print("up-to-date %s (0 changed files; "
                          "graph bytes unchanged)" % out)
                    return 0
        except (ValueError, OSError) as exc:
            print("manifest unreadable (%s); full rebuild" % exc)
    doc = build_graph(root, project, fixed_time, cfg)
    blob = _graph_blob(doc)
    if out:
        with open(out, "wb") as fh:
            fh.write(blob)
        print("built %s (%d nodes, %d edges, %d sources, %d gaps)" %
              (out, len(doc["nodes"]), len(doc["edges"]),
               len(doc["sources"]), len(doc["gaps"])))
    else:
        sys.stdout.write(blob.decode("utf-8"))
    if manifest_path:
        cfg = cfg or {}
        manifest = build_manifest(
            root, project, blob, doc["generated_at"],
            doc["source_commit"],
            {"nodes": len(doc["nodes"]), "edges": len(doc["edges"]),
             "sources": len(doc["sources"]), "gaps": len(doc["gaps"])},
            version=str(cfg.get("version", "")),
            exclude=_output_rels(root, [out, manifest_path]))
        save_manifest(manifest_path, manifest)
        print("wrote manifest %s (%d files, graph %s...)"
              % (manifest_path, len(manifest["files"]),
                 manifest["graph_sha256"][:12]))
    return 0


def _output_rels(root, paths):
    """Relpaths of outputs living inside root (for manifest exclusion)."""
    rels = []
    for path in paths:
        if not path:
            continue
        rel = rel_inside(root, path)
        if rel is not None:
            rels.append(rel)
    return rels


def cmd_manifest(root, project, graph_path, out, fixed_time, version):
    with open(graph_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    blob = _graph_blob(doc)
    manifest = build_manifest(
        root, project, blob,
        fixed_time or nowstamp(None), doc.get("source_commit", "unknown"),
        {"nodes": len(doc.get("nodes", [])),
         "edges": len(doc.get("edges", [])),
         "sources": len(doc.get("sources", [])),
         "gaps": len(doc.get("gaps", []))},
        version=version,
        exclude=_output_rels(root, [graph_path, out]))
    save_manifest(out, manifest)
    print("wrote manifest %s (%d files, graph %s...)"
          % (out, len(manifest["files"]), manifest["graph_sha256"][:12]))
    return 0


def cmd_diagnostics(graph_path, old_path):
    with open(graph_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    old = None
    if old_path:
        with open(old_path, encoding="utf-8") as fh:
            old = json.load(fh)
    sys.stdout.write(render_diagnostics(doc, old,
                                        label=graph_path,
                                        old_label=old_path or "old"))
    return 0


# --------------------------------------------------------------------------
# Selftest (offline fixture + stdlib-only import check + determinism).
# --------------------------------------------------------------------------

FIXTURE = {
    "main.py": "import helper\n",
    "helper.py": "def helper():\n    return 1\n",
    "driver/driver.lua": 'require "lib.util"\n',
    "driver/lib/util.lua": "-- util\nreturn {}\n",
    "scripts/build.py": "print('build')\n",
    ".github/workflows/ci.yml": (
        "name: ci\non: [push]\njobs:\n  b:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: python3 scripts/build.py\n"),
    "README.md": "# demo\n\nSee [guide](docs/guide.md).\n",
    "docs/guide.md": "# guide\n",
    "index.html": (
        '<html><head><script src="assets/app.js"></script></head>\n'
        '<body><a href="about.html">about</a></body></html>\n'),
    "about.html": "<html><body>hi</body></html>\n",
    "assets/app.js": 'fetch("api/data.php");\n',
    "api/data.php": "<?php /* endpoint */ ?>\n",
    "tool/keygen.ps1": (
        'param($HardwareId)\n$LICENSE_SALT = "..."\n'
        'Write-Output $HardwareId\n'),
    ".env": "API_KEY=supersecretvalue\n",
    "build/out.txt": "generated\n",
}

FIXED_TIME = "2026-01-01T00:00:00Z"

SELFTEST_MODULES = ("project_graph.py", "extractors.py", "manifest.py",
                    "diagnostics.py")

# Sibling template-core modules: local first-party imports, not a
# framework and not third-party (the core stays stdlib + itself only).
SELFTEST_LOCAL = {"extractors", "manifest", "diagnostics",
                    "graph_symbols"}


def write_fixture(root):
    for rel, text in FIXTURE.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)


def check(name, cond, failures):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def cmd_selftest():
    failures = []
    # Stdlib-only imports in every generator module.
    here = os.path.dirname(os.path.abspath(__file__))
    for mod in SELFTEST_MODULES:
        with open(os.path.join(here, mod), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        stdlib = set(sys.stdlib_module_names)
        check("stdlib-only imports (%s)" % mod,
              all(i in stdlib or i in SELFTEST_LOCAL for i in imports),
              failures)

    with tempfile.TemporaryDirectory() as tmp:
        write_fixture(tmp)
        doc = build_graph(tmp, "demo", FIXED_TIME)
        check("schema keys", sorted(doc.keys()) == sorted(SCHEMA_KEYS),
              failures)
        ids = [n["id"] for n in doc["nodes"]]
        check("node ids sorted+unique",
              ids == sorted(ids) and len(set(ids)) == len(ids), failures)
        for n in doc["nodes"]:
            if set(n.keys()) != {"id", "type", "source", "attrs"}:
                check("node keys", False, failures)
                break
        else:
            check("node keys", True, failures)
        bad = [n["id"] for n in doc["nodes"]
               if n.get("type") not in ALLOWED_TYPES]
        check("node types allowed", not bad, failures)
        for e in doc["edges"]:
            if set(e.keys()) != {"src", "dst", "rel", "evidence"}:
                check("edge keys", False, failures)
                break
        else:
            check("edge keys", True, failures)
        blob = json.dumps(doc, sort_keys=True)
        check("lua require edge", any(
            e["src"] == "lua:driver/driver.lua"
            and e["dst"] == "lua:driver/lib/util.lua"
            and e["rel"] == "requires" for e in doc["edges"]), failures)
        check("py import edge", any(
            e["src"] == "script:main.py" and e["dst"] == "script:helper.py"
            for e in doc["edges"]), failures)
        check("workflow runs edge", any(
            e["src"] == "workflow:.github/workflows/ci.yml"
            and e["dst"] == "script:scripts/build.py"
            and e["rel"] == "runs" for e in doc["edges"]), failures)
        check("md link edge", any(
            e["src"] == "doc:README.md"
            and e["dst"] == "doc:docs/guide.md"
            for e in doc["edges"]), failures)
        check("page embeds edge", any(
            e["src"] == "page:index.html"
            and e["dst"] == "asset:assets/app.js"
            and e["rel"] == "embeds" for e in doc["edges"]), failures)
        check("js calls edge", any(
            e["src"] == "asset:assets/app.js"
            and e["dst"] == "page:api/data.php"
            for e in doc["edges"]), failures)
        check(".env skipped", not any(
            ".env" == n["attrs"].get("path") for n in doc["nodes"]),
            failures)
        check("build/ skipped", not any(
            n["attrs"].get("path", "").startswith("build/")
            for n in doc["nodes"]), failures)
        check("secret name gap, no value", any(
            "LICENSE_SALT" in g["item"] for g in doc["gaps"])
            and "supersecretvalue" not in blob, failures)
        # Committed outline layer (S4): purpose/lang on file nodes, symbol
        # nodes with kind/parent/signature only, defines edges file->symbol.
        file_nodes = [n for n in doc["nodes"]
                      if not n["id"].startswith("symbol:")]
        sym_nodes = [n for n in doc["nodes"]
                     if n["id"].startswith("symbol:")]
        check("file nodes carry purpose/lang",
              all({"purpose", "lang"} <= set(n["attrs"])
                  for n in file_nodes
                  if n["type"] not in ("asset", "vendor"))
              and all({"purpose", "lang"} <= set(n["attrs"])
                      for n in file_nodes
                      if n["type"] in ("asset", "vendor")), failures)
        check("purpose <= 120 chars",
              all(len(n["attrs"].get("purpose", "")) <= 120
                  for n in file_nodes), failures)
        check("symbol nodes exist", len(sym_nodes) > 0, failures)
        check("symbol attrs kind/parent/signature only",
              all(set(n.get("attrs", {})) == {"kind", "parent", "signature"}
                  for n in sym_nodes), failures)
        check("symbol signatures <= 120 chars",
              all(len(n["attrs"].get("signature", "")) <= 120
                  for n in sym_nodes), failures)
        defines = [e for e in doc["edges"] if e["rel"] == "defines"]
        check("defines edges cover every symbol",
              len(defines) > 0 and
              sorted(e["dst"] for e in defines) ==
              sorted(n["id"] for n in sym_nodes), failures)
        check("py outline symbol present", any(
            n["id"] == "symbol:helper.py#helper" for n in sym_nodes),
            failures)
        # Configurable extractor: disabling lua kills lua requires edges
        # while lua nodes stay.
        nodoc = build_graph(tmp, "demo", FIXED_TIME,
                            {"extractors": {"lua": False}})
        check("extractor off: lua nodes stay",
              sorted(n["id"] for n in nodoc["nodes"]
                     if n["id"].startswith("lua:"))
              == sorted(n["id"] for n in doc["nodes"]
                        if n["id"].startswith("lua:")), failures)
        check("extractor off: lua requires edges gone",
              not [e for e in nodoc["edges"]
                   if e["src"].startswith("lua:")
                   and e["rel"] == "requires"], failures)
        # Determinism: rebuild byte-identical.
        blob1 = json.dumps(build_graph(tmp, "demo", FIXED_TIME),
                           indent=2, sort_keys=True) + "\n"
        blob2 = json.dumps(build_graph(tmp, "demo", FIXED_TIME),
                           indent=2, sort_keys=True) + "\n"
        check("deterministic rebuild", blob1 == blob2, failures)
        # Empty checkout -> gaps-only graph.
        with tempfile.TemporaryDirectory() as empty:
            os.makedirs(os.path.join(empty, ".git"))
            edoc = build_graph(empty, "nothing", FIXED_TIME)
            check("gaps-only empty graph",
                  edoc["nodes"] == [] and edoc["edges"] == []
                  and any(g["id"] == "empty-checkout"
                          for g in edoc["gaps"]), failures)
        # Incremental: manifest round-trip, no-op rebuild, change detect.
        man_path = os.path.join(tmp, "manifest.json")
        out_path = os.path.join(tmp, "graph.json")
        rc = cmd_build(tmp, "demo", out_path, FIXED_TIME,
                       manifest_path=man_path)
        check("build with manifest exit 0", rc == 0
              and os.path.exists(man_path), failures)
        with open(out_path, "rb") as fh:
            first_bytes = fh.read()
        old = load_manifest(man_path)
        rebuild, changed = needs_rebuild(old, tmp)
        check("incremental: no changes after build",
              rebuild is False and changed == [], failures)
        rc = cmd_build(tmp, "demo", out_path, FIXED_TIME,
                       incremental=True, manifest_path=man_path)
        with open(out_path, "rb") as fh:
            check("incremental: up-to-date keeps bytes",
                  rc == 0 and fh.read() == first_bytes, failures)
        with open(os.path.join(tmp, "helper.py"), "a",
                  encoding="utf-8") as fh:
            fh.write("\n# touch\n")
        rebuild, changed = needs_rebuild(load_manifest(man_path), tmp)
        check("incremental: detects changed file",
              rebuild is True and changed == ["helper.py"], failures)
        # Diagnostics renders counts and gap categories.
        report = render_diagnostics(doc)
        check("diagnostics renders",
              "nodes:" in report and "gaps by category:" in report,
              failures)
    print("SELFTEST %s (%d failures)" %
          ("PASS" if not failures else "FAIL", len(failures)))
    return 1 if failures else 0


USAGE = """usage: project_graph.py <verb> [options]

  build --root <checkout> --project <name> [--out FILE]
        [--fixed-time 2026-01-01T00:00:00Z] [--config FILE]
        [--manifest FILE] [--incremental]
  manifest --root <checkout> --project <name> --graph FILE
        --out FILE [--fixed-time ...] [--version V]
  diagnostics --graph FILE [--old FILE]
  --selftest    offline fixture checks
"""


def main(argv):
    if len(argv) < 2 or argv[1] in ("--help", "-h", "help"):
        print(USAGE)
        return 0
    verb = argv[1]
    if verb in ("--selftest", "selftest"):
        return cmd_selftest()
    if verb == "build":
        ap = argparse.ArgumentParser()
        ap.add_argument("--root", default=".")
        ap.add_argument("--project", default="")
        ap.add_argument("--out", default="")
        ap.add_argument("--fixed-time", default="")
        ap.add_argument("--config", default="")
        ap.add_argument("--manifest", default="")
        ap.add_argument("--incremental", action="store_true")
        args = ap.parse_args(argv[2:])
        cfg = load_config(args.config) if args.config else None
        project = (args.project or (cfg or {}).get("project")
                   or os.path.basename(os.path.abspath(args.root)))
        return cmd_build(args.root, project,
                         args.out or None, args.fixed_time or None, cfg,
                         args.manifest or None, args.incremental)
    if verb == "manifest":
        ap = argparse.ArgumentParser()
        ap.add_argument("--root", default=".")
        ap.add_argument("--project", default="")
        ap.add_argument("--graph", default="")
        ap.add_argument("--out", default="")
        ap.add_argument("--fixed-time", default="")
        ap.add_argument("--version", default="")
        args = ap.parse_args(argv[2:])
        if not args.graph or not args.out:
            print("manifest needs --graph and --out\n%s" % USAGE)
            return 2
        project = args.project or os.path.basename(
            os.path.abspath(args.root))
        return cmd_manifest(args.root, project, args.graph, args.out,
                            args.fixed_time or None, args.version)
    if verb == "diagnostics":
        ap = argparse.ArgumentParser()
        ap.add_argument("--graph", default="")
        ap.add_argument("--old", default="")
        args = ap.parse_args(argv[2:])
        if not args.graph:
            print("diagnostics needs --graph\n%s" % USAGE)
            return 2
        return cmd_diagnostics(args.graph, args.old or None)
    print("unknown verb '%s'\n%s" % (verb, USAGE))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
