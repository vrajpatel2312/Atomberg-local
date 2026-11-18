#!/usr/bin/env python3
"""Configurable edge-extractor registry for the project-graph template core.

Each extractor handles one node kind and appends (src, dst, rel,
evidence) edges through the context. The core (project_graph.py)
dispatches to the registry, so a per-project config can disable any
extractor by name without touching code:

    {"extractors": {"lua": false}}

Stdlib only. Secret rule: extractors read structure only; identifier
names may flow to gaps, values never do.
"""

import os
import re
import sys

# --------------------------------------------------------------------------
# Shared matching helpers (also used by the core for gap evidence lines).
# --------------------------------------------------------------------------

LUA_REQUIRE_RE = re.compile(
    r"\brequire\s*(?:\(\s*)?[\"']([\w.]+)[\"']")
LUA_FILE_RE = re.compile(
    r"\b(?:do|load)file\s*\(\s*[\"']([^\"']+)[\"']")
PY_IMPORT_RE = re.compile(
    r"^\s*import\s+([\w.]+)", re.MULTILINE)
PY_FROM_RE = re.compile(
    r"^\s*from\s+([\w.]+)\s+import\s+", re.MULTILINE)
SH_SOURCE_RE = re.compile(
    r"(?:^|[;|&]\s*|\s)(?:source|\.)\s+([^\s;|&\"']+)")
SH_RUN_RE = re.compile(
    r"(?:^|[;|&]\s*|\s)(?:bash|sh|python3?|lua|luajit|perl)\s+"
    r"([^\s;|&\"']+)")
SH_TOKEN_RE = re.compile(r"([A-Za-z0-9_./-]+\.(?:sh|py|ps1))")
PHP_INCLUDE_RE = re.compile(
    r"\b(?:require|include)(?:_once)?\s*\(?\s*[\"']([^\"']+)[\"']")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HTML_REF_RE = re.compile(
    r"(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
CSS_URL_RE = re.compile(r"url\(\s*[\"']?([^)\"']+)")
JS_STR_RE = re.compile(r"[\"']([^\"']{2,160})[\"']")
SCRIPT_ATTR_RE = re.compile(r"<script\s+[^>]*file\s*=\s*[\"']([^\"']+)")
TEXTFILE_ATTR_RE = re.compile(r"textfile\s*=\s*[\"']([^\"']+)")
C4Z_ITEM_RE = re.compile(
    r"<Item\s+(?:type\s*=\s*\"(file|dir)\"\s+name\s*=\s*\"([^\"]+)\"|"
    r"name\s*=\s*\"([^\"]+)\"\s+type\s*=\s*\"(file|dir)\")")
DOCKER_COPY_RE = re.compile(r"^COPY\s+(\S+)\s+\S+", re.MULTILINE)
DOCKER_IMAGE_RE = re.compile(r"^\s*image\s*:\s*[\"']?(\S+?)[\"']?\s*$",
                             re.MULTILINE)
JS_CALL_SUFFIXES = (".php", ".json", ".lua", ".py", ".sh", ".js",
                    ".css", ".xml")


def strip_qs(ref):
    return ref.split("?", 1)[0].split("#", 1)[0]


def is_external(ref):
    low = ref.lower()
    return (not ref or ref.startswith(("#", "{", "$", "%"))
            or "://" in ref or low.startswith(
                ("tel:", "mailto:", "data:", "javascript:")))


def first_lineno(text, token):
    for i, line in enumerate(text.splitlines(), 1):
        if token in line:
            return i
    return 1


def resolve(ref, src_dir, alias):
    """Resolve a reference to a node id, or None."""
    cand = os.path.normpath(os.path.join(src_dir, ref)).replace(
        os.sep, "/")
    if cand in alias:
        return alias[cand]
    norm = ref.strip("./")
    if norm in alias:
        return alias[norm]
    base = norm.split("/")[-1]
    if not base:
        return None
    matches = sorted(set(nid for rel, nid in alias.items()
                         if rel.split("/")[-1] == base))
    return matches[0] if matches else None


def lua_index(nodes):
    """Map dotted module paths (and suffixes) to lua node relpaths."""
    index = {}
    for nid in nodes:
        ntype, _, rel = nid.partition(":")
        if ntype != "lua":
            continue
        dotted = rel[:-4].replace("/", ".")
        parts = dotted.split(".")
        for i in range(len(parts)):
            index.setdefault(".".join(parts[i:]), []).append(rel)
    return index


def resolve_lua(mod, src_rel, index, alias):
    cands = index.get(mod, [])
    if not cands:
        return None
    if len(cands) == 1:
        return alias[cands[0]]
    src_dir = src_rel.rsplit("/", 1)[0] if "/" in src_rel else ""

    def score(rel):
        cand_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        common = 0
        a, b = src_dir.split("/"), cand_dir.split("/")
        for x, y in zip(a, b):
            if x == y:
                common += 1
            else:
                break
        return (-common, rel)
    return alias[sorted(cands, key=score)[0]]


def resolve_py(mod, src_rel, alias):
    """Resolve a python module to a tracked .py node, else None."""
    first = mod.split(".")[0]
    rest = mod.split(".")[1:]
    src_dir = src_rel.rsplit("/", 1)[0] if "/" in src_rel else ""
    cands = []
    if rest:
        cands.append(src_dir + "/" + first + "/" + "/".join(rest)
                     + ".py" if src_dir else first + "/" + "/".join(
                         rest) + ".py")
        cands.append(first + "/" + "/".join(rest) + ".py")
    cands.append((src_dir + "/" + first + ".py").lstrip("/")
                 if src_dir else first + ".py")
    cands.append(first + ".py")
    for cand in cands:
        cand = os.path.normpath(cand).replace(os.sep, "/")
        if cand in alias:
            return alias[cand]
    return None


# --------------------------------------------------------------------------
# Registry.
# --------------------------------------------------------------------------

class Ctx:
    """Shared edge-extraction state passed to every extractor.

    nodes/alias/texts/index/basenames are read-only inputs; add() and
    unresolved collect the outputs.
    """

    def __init__(self, nodes, alias, texts, index, basenames):
        self.nodes = nodes
        self.alias = alias
        self.texts = texts
        self.index = index
        self.basenames = basenames
        self.unresolved = []
        self.edges = set()

    def add(self, src, dst, rel, evidence):
        if dst is not None and dst != src:
            self.edges.add((src, dst, rel, evidence))


EXTRACTORS = {}


def register(name, fn=None):
    """Register (or replace) an extractor under name.

    Usable as ``register("mine", fn)`` or as a decorator::

        @register("mine")
        def ex_mine(ctx, nid, rel, text, lines, src_dir): ...
    """
    def wrap(fn):
        EXTRACTORS[name] = fn
        return fn
    return wrap(fn) if fn else wrap


def enabled_extractors(cfg):
    """Sorted registry names enabled by cfg (``{"extractors": {...}}``).

    Unknown names in cfg are ignored; everything registered is on
    unless explicitly set to False.
    """
    wanted = (cfg or {}).get("extractors", {}) or {}
    return sorted(n for n in EXTRACTORS if wanted.get(n, True) is not False)


def dispatch_key(ntype, rel):
    """Registry key for a (node type, relpath) pair, or None."""
    if ntype == "lua":
        return "lua"
    if ntype == "script":
        if rel.endswith(".py"):
            return "script-py"
        if rel.endswith((".sh", ".ps1")):
            return "script-sh"
        if rel.endswith(".go"):
            return "script-go"
        return None
    if ntype == "driver-xml":
        return "driver-xml"
    if ntype == "manifest":
        return "manifest"
    if ntype == "workflow":
        return "workflow"
    if ntype == "doc":
        return "doc"
    if ntype == "page":
        return "page"
    if ntype in ("asset", "config"):
        return "asset"
    return None


# --------------------------------------------------------------------------
# Extractors (one per registry key; logic mirrors the reference
# scripts/py/project_graph.py edge extraction verbatim).
# --------------------------------------------------------------------------

@register("lua")
def ex_lua(ctx, nid, rel, text, lines, src_dir):
    for m in LUA_REQUIRE_RE.finditer(text):
        mod = m.group(1)
        dst = resolve_lua(mod, rel, ctx.index, ctx.alias)
        if dst is not None:
            ctx.add(nid, dst, "requires",
                    "%s:%d" % (rel, first_lineno(text, m.group(0))))
        else:
            ctx.unresolved.append((rel, mod))
    for m in LUA_FILE_RE.finditer(text):
        dst = resolve(m.group(1), src_dir, ctx.alias)
        if dst is not None:
            ctx.add(nid, dst, "requires",
                    "%s:%d" % (rel, first_lineno(text, m.group(0))))
        else:
            ctx.unresolved.append((rel, m.group(1)))


@register("script-py")
def ex_script_py(ctx, nid, rel, text, lines, src_dir):
    mods = set(PY_IMPORT_RE.findall(text)) | set(PY_FROM_RE.findall(text))
    for mod in sorted(mods):
        if mod.split(".")[0] in sys.stdlib_module_names:
            continue
        dst = resolve_py(mod, rel, ctx.alias)
        if dst is not None:
            ctx.add(nid, dst, "requires",
                    "%s:%d" % (rel, first_lineno(text, mod)))
        # Unresolved py imports are usually stdlib/third-party.


@register("script-sh")
def ex_script_sh(ctx, nid, rel, text, lines, src_dir):
    refs = set(SH_SOURCE_RE.findall(text))
    refs |= set(SH_RUN_RE.findall(text))
    refs |= set(SH_TOKEN_RE.findall(text))
    if rel.endswith(".sh") and os.path.basename(rel) == "entrypoint.sh":
        refs |= set(JS_STR_RE.findall(text))
    for ref in sorted(refs):
        ref = strip_qs(ref)
        if is_external(ref) or ref.startswith("-"):
            continue
        dst = resolve(ref, src_dir, ctx.alias)
        if dst is not None:
            ctx.add(nid, dst, "runs",
                    "%s:%d" % (rel, first_lineno(text, ref)))


@register("script-go")
def ex_script_go(ctx, nid, rel, text, lines, src_dir):
    for m in re.finditer(r'"([^"]+)"', text):
        dst = resolve(m.group(1), src_dir, ctx.alias)
        if dst is not None:
            ctx.add(nid, dst, "requires",
                    "%s:%d" % (rel, first_lineno(text, m.group(0))))


@register("driver-xml")
def ex_driver_xml(ctx, nid, rel, text, lines, src_dir):
    for m in SCRIPT_ATTR_RE.finditer(text):
        dst = resolve(m.group(1), src_dir, ctx.alias)
        if dst is not None:
            ctx.add(nid, dst, "loads",
                    "%s:%d" % (rel, first_lineno(text, m.group(1))))
        else:
            ctx.unresolved.append((rel, m.group(1)))
    for m in TEXTFILE_ATTR_RE.finditer(text):
        dst = resolve(m.group(1), src_dir, ctx.alias)
        if dst is not None:
            ctx.add(nid, dst, "includes",
                    "%s:%d" % (rel, first_lineno(text, m.group(1))))


@register("manifest")
def ex_manifest(ctx, nid, rel, text, lines, src_dir):
    if rel.endswith(".c4zproj"):
        for m in C4Z_ITEM_RE.finditer(text):
            kind = m.group(1) or m.group(4)
            name = m.group(2) or m.group(3)
            base = os.path.normpath(
                os.path.join(src_dir, name)).replace(os.sep, "/")
            if kind == "file":
                ctx.add(nid, ctx.alias.get(base), "bundles",
                        "%s:%d" % (rel, first_lineno(text, name)))
            else:
                prefix = base.rstrip("/") + "/"
                for target_rel in sorted(ctx.alias):
                    if target_rel.startswith(prefix):
                        ctx.add(nid, ctx.alias[target_rel], "bundles",
                                "%s:%d" % (rel, first_lineno(text, name)))
    if os.path.basename(rel) == "Dockerfile":
        for m in DOCKER_COPY_RE.finditer(text):
            base = os.path.normpath(
                os.path.join(src_dir, m.group(1))).replace(os.sep, "/")
            prefix = base.rstrip("/") + "/"
            hit = False
            for target_rel in sorted(ctx.alias):
                if target_rel == base or target_rel.startswith(prefix):
                    ctx.add(nid, ctx.alias[target_rel], "bundles",
                            "%s:%d" % (rel, first_lineno(text, m.group(1))))
                    hit = True
            if not hit:
                ctx.unresolved.append((rel, m.group(1)))
    if os.path.basename(rel) in ("action.yml", "action.yaml"):
        for m in DOCKER_IMAGE_RE.finditer(text):
            if "ockerfile" in m.group(1):
                dst = resolve(m.group(1), src_dir, ctx.alias)
                if dst is not None:
                    ctx.add(nid, dst, "uses",
                            "%s:%d" % (rel, first_lineno(text, m.group(1))))
    # Basename mentions of tracked scripts (container paths,
    # action args, RUN lines) become runs edges.
    for m in SH_TOKEN_RE.finditer(text):
        dst = resolve(m.group(1), src_dir, ctx.alias)
        if dst is not None and dst != nid:
            ctx.add(nid, dst, "runs",
                    "%s:%d" % (rel, first_lineno(text, m.group(1))))


@register("workflow")
def ex_workflow(ctx, nid, rel, text, lines, src_dir):
    for lineno, line in enumerate(lines, 1):
        for tok in SH_TOKEN_RE.findall(line):
            dst = resolve(strip_qs(tok), src_dir, ctx.alias)
            if dst is not None and dst != nid:
                ctx.add(nid, dst, "runs", "%s:%d" % (rel, lineno))
        m = re.match(r"\s*uses\s*:\s*\./(\S+)", line)
        if m:
            dst = resolve(m.group(1), src_dir, ctx.alias)
            if dst is not None:
                ctx.add(nid, dst, "uses", "%s:%d" % (rel, lineno))


@register("doc")
def ex_doc(ctx, nid, rel, text, lines, src_dir):
    for m in MD_LINK_RE.finditer(text):
        target = strip_qs(m.group(1).strip())
        if is_external(target):
            continue
        dst = resolve(target, src_dir, ctx.alias)
        if dst is not None and dst != nid:
            ctx.add(nid, dst, "links",
                    "%s:%d" % (rel, first_lineno(text, m.group(1))))


@register("page")
def ex_page(ctx, nid, rel, text, lines, src_dir):
    for m in HTML_REF_RE.finditer(text):
        target = strip_qs(m.group(1).strip())
        if is_external(target):
            continue
        dst = resolve(target, src_dir, ctx.alias)
        if dst is None or dst == nid:
            continue
        attr = m.group(0).split("=")[0].strip().lower()
        ctx.add(nid, dst, "embeds" if attr == "src" else "links",
                "%s:%d" % (rel, first_lineno(text, m.group(1))))
    if rel.endswith(".php"):
        for m in PHP_INCLUDE_RE.finditer(text):
            dst = resolve(m.group(1), src_dir, ctx.alias)
            if dst is not None:
                ctx.add(nid, dst, "requires",
                        "%s:%d" % (rel, first_lineno(text, m.group(1))))


@register("asset")
def ex_asset(ctx, nid, rel, text, lines, src_dir):
    if "/vendor/" in rel:
        return  # third-party blob: hashed, never parsed
    if rel.endswith(".css"):
        for m in CSS_URL_RE.finditer(text):
            target = strip_qs(m.group(1).strip().strip("\"'"))
            if is_external(target):
                continue
            dst = resolve(target, src_dir, ctx.alias)
            if dst is not None and dst != nid:
                ctx.add(nid, dst, "embeds",
                        "%s:%d" % (rel, first_lineno(text, m.group(1))))
    elif rel.endswith(".js"):
        for m in JS_STR_RE.finditer(text):
            lit = strip_qs(m.group(1))
            if is_external(lit):
                continue
            base = lit.split("/")[-1]
            if "/" not in lit and not lit.endswith(JS_CALL_SUFFIXES):
                continue
            dst = resolve(lit, src_dir, ctx.alias)
            if dst is None and "/" not in lit:
                cands = sorted(set(
                    ctx.alias[r] for r in ctx.basenames.get(base, [])))
                dst = cands[0] if cands else None
            if dst is not None and dst != nid:
                ctx.add(nid, dst, "calls",
                        "%s:%d" % (rel, first_lineno(text, m.group(0))))
