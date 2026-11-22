#!/usr/bin/env python3
"""W13 I1 symbol extractor: stdlib-only file purpose + outline + edges.

Committed outline uses the Python standard library only (CI and repo
hooks install nothing). Call/test edges use pinned tree-sitter grammars
when importable, else the stdlib fallback; every edges() result records
which backend produced it.

Public surface (frozen by the W13 Wave 2 brief, do not change):
  EXTRACTOR_VERSION = "2" (string; bump on any output change)
  purpose(relpath, text) -> str (<= 120 chars)
  outline(relpath, text) -> list[dict] sorted by (line_start, name)
  edges(relpath, text, outline, known) -> (list[dict], backend)
  CLI: graph_symbols.py outline|edges|purpose <file> [--json], --selftest

Outline entry: {name, qualname, kind, parent, signature, line_start,
line_end, doc_line}. kinds: function, class, method, bash-fn, lua-fn,
ps1-fn, js-fn, php-fn, section, config-key, unit-key, driver-command,
driver-property, cli-verb.
Edge: {src, dst, rel, line}, rel in calls | imports | tests; src/dst
are symbol:<relpath>#<qualname> or file:<relpath> ids. known maps a
qualname or bare name to [symbol ids]; unresolved calls are dropped.
Test files (tests/**, test_*.py, *_test.*, test-*.sh) emit tests edges
for the symbols they exercise instead of calls/imports.

Secrets rule (mirrors project_graph.is_secret_token): identifier NAMES
may flow into outline entries; values and string literals never do.
Signatures are scrubbed of quoted literals, config/unit entries record
key paths with redacted ("...") values, and secret-shaped files are
still outlined by key name only.
"""

import argparse
import ast
import json
import os
import re
import sys

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

try:
    import xml.etree.ElementTree as ET
except ImportError:  # pragma: no cover
    ET = None

EXTRACTOR_VERSION = "2"

MAX_PURPOSE = 120
MAX_SIG = 120
MAX_DEPTH = 3
BIG_FILE = 1024 * 1024

# Identifier tokens shaped like secret names. Mirrors the rule in
# templates/project-graph/generator/project_graph.py (kept as a local
# copy so this module vendors cleanly into other repos): single common
# words only count when all-lower or all-upper, dotted/underscored
# names always count when they contain a secret substring.
SECRET_TOKEN_RE = re.compile(r"\$?([A-Za-z_][A-Za-z0-9_]{1,64})")
SECRET_SUBSTRS = ("password", "passwd", "secret", "salt", "token",
                  "apikey", "api_key", "api-key", "private_key",
                  "privatekey", "bearer")
SECRET_SINGLE = ("password", "passwd", "secret", "salt", "token",
                 "bearer", "apikey")


def is_secret_token(tok):
    """True for code-identifier-shaped secret names (never values)."""
    low = tok.lower()
    if not any(s in low for s in SECRET_SUBSTRS):
        return False
    if "_" in tok or "-" in tok:
        return True
    return low in SECRET_SINGLE and (tok == low or tok == tok.upper())


_LITERAL_RE = re.compile(r"'([^'\\]|\\.)*'|\"([^\"\\]|\\.)*\"")


def _scrub_literals(text):
    """Replace quoted string literals with ... (never store a value)."""
    return _LITERAL_RE.sub("...", text or "")


def _cut(text, limit=MAX_SIG):
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit]
    return text


def _entry(name, qualname, kind, parent, signature, line_start, line_end,
           doc_line):
    return {
        "name": name,
        "qualname": qualname,
        "kind": kind,
        "parent": parent or "",
        "signature": _cut(_scrub_literals(signature), MAX_SIG),
        "line_start": int(line_start),
        "line_end": int(line_end),
        "doc_line": _cut(doc_line, MAX_SIG),
    }


def _sort_entries(entries):
    entries.sort(key=lambda e: (e["line_start"], e["name"]))
    return entries


def _fill_ends(entries, n_lines):
    """Function-like entries missing line_end run to the next entry."""
    ordered = sorted(entries, key=lambda e: (e["line_start"], e["name"]))
    for i, entry in enumerate(ordered):
        if not entry["line_end"]:
            if i + 1 < len(ordered):
                entry["line_end"] = max(
                    entry["line_start"],
                    ordered[i + 1]["line_start"] - 1)
            else:
                entry["line_end"] = max(entry["line_start"], n_lines)
    return entries


def _find_line(text, token, start=1):
    for i, line in enumerate(text.splitlines(), 1):
        if i >= start and token in line:
            return i
    return start


_QUOTED_KEY_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _first_lines(text, tokens):
    """Map each wanted key to its first `"key"` line in a single pass.

    Linear-time replacement for one _find_line() call per outline entry
    (S1b: the per-entry scan was quadratic — 8897 entries x 13k lines =
    12.5 s on a 374 KB generated graph). Missing keys map to nothing;
    callers fall back to line 1 (deterministic).
    """
    wanted = set(tokens)
    found = {}
    if not wanted:
        return found
    for i, line in enumerate(text.splitlines(), 1):
        if len(found) == len(wanted):
            break
        if '"' not in line:
            continue
        for match in _QUOTED_KEY_RE.finditer(line):
            key = match.group(1)
            if key in wanted and key not in found:
                found[key] = i
                if len(found) == len(wanted):
                    break
    return found


def purpose(relpath, text):
    """One-line purpose (<= 120 chars) for a file, or "" when none."""
    if not text or not text.strip():
        return ""
    base = relpath.rsplit("/", 1)[-1]
    low = base.lower()
    lines = text.splitlines()
    if low.endswith(".md") or low.endswith(".markdown"):
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                return _cut(stripped.lstrip("#").strip(), MAX_PURPOSE)
        return ""
    if low.endswith((".service", ".timer", ".socket", ".target",
                     ".path", ".mount")):
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("Description="):
                return _cut(stripped.split("=", 1)[1].strip(),
                            MAX_PURPOSE)
        return ""
    if low.endswith(".py"):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            doc = ast.get_docstring(tree)
            if doc:
                for doc_line in doc.splitlines():
                    if doc_line.strip():
                        return _cut(doc_line.strip(), MAX_PURPOSE)
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#!"):
                continue
            if stripped.startswith("#"):
                return _cut(stripped.lstrip("#").strip(), MAX_PURPOSE)
            if stripped:
                return ""
        return ""
    if base == "driver.xml":
        if ET is not None:
            try:
                root = ET.fromstring(text)
                for tag in ("description", "name"):
                    for elem in root.iter(tag):
                        if elem.text and elem.text.strip():
                            first = elem.text.strip().splitlines()[0]
                            return _cut(first.strip(), MAX_PURPOSE)
            except ET.ParseError:
                pass
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#!"):
            continue
        for marker in ("#", "--", "//", "<!--", "/*", "*", '"', "'",
                       "REM ", "::"):
            if stripped.startswith(marker):
                return _cut(stripped[len(marker):].strip(" *-/"),
                            MAX_PURPOSE)
        return ""
    return ""

_ADD_PARSER_RE = re.compile(r"\.add_parser\(\s*[\"']([\w-]+)[\"']")


def _py_outline(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    lines = text.splitlines()
    total = len(lines)

    def sig_at(lineno):
        if 1 <= lineno <= total:
            return lines[lineno - 1].strip()
        return ""

    def doc_of(node):
        try:
            doc = ast.get_docstring(node)
        except Exception:
            return ""
        if not doc:
            return ""
        for doc_line in doc.splitlines():
            if doc_line.strip():
                return doc_line.strip()
        return ""

    out = []

    def walk(node, qualprefix, classctx):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qualname = (qualprefix + "." + child.name if qualprefix
                            else child.name)
                out.append(_entry(
                    child.name, qualname, "class", qualprefix,
                    sig_at(child.lineno), child.lineno,
                    child.end_lineno or child.lineno, doc_of(child)))
                walk(child, qualname, child.name)
            elif isinstance(child, (ast.FunctionDef,
                                    ast.AsyncFunctionDef)):
                qualname = (qualprefix + "." + child.name if qualprefix
                            else child.name)
                kind = "method" if classctx else "function"
                out.append(_entry(
                    child.name, qualname, kind, qualprefix,
                    sig_at(child.lineno), child.lineno,
                    child.end_lineno or child.lineno, doc_of(child)))
                walk(child, qualname, None)

    walk(tree, "", None)
    seen_verbs = set()
    for i, line in enumerate(lines, 1):
        for match in _ADD_PARSER_RE.finditer(line):
            verb = match.group(1)
            if verb not in seen_verbs:
                seen_verbs.add(verb)
                out.append(_entry(verb, verb, "cli-verb", "",
                                  "cli verb: " + verb, i, i, ""))
    return _sort_entries(out)


_SH_DEF_RE = re.compile(
    r"^\s*function\s+([A-Za-z_][A-Za-z0-9_:\-]*)\s*(?:\(\s*\))?\s*"
    r"(?:\{|\}|$|#)")
_SH_PLAIN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)")
_SH_CASE_RE = re.compile(r"^\s*case\s+(.+?)\s+in\s*$")
_SH_ARM_RE = re.compile(r"^\s*([^#\s][^)]*)\)")
_SH_DISPATCH_RE = re.compile(
    r"\$1|\$2|\$@|\$#|\bcmd\b|\bverb\b|\baction\b|\bsubcommand\b")
_SH_DISPATCH_FN = {"main", "main_", "dispatch", "usage", "cli", "run"}


def _sh_outline(text):
    lines = text.splitlines()
    total = len(lines)
    out = []
    current_fn = ""
    for i, line in enumerate(lines, 1):
        match = _SH_DEF_RE.match(line)
        if match:
            current_fn = match.group(1)
            doc = ""
            if i > 1 and lines[i - 2].strip().startswith("#"):
                doc = lines[i - 2].strip().lstrip("#").strip()
            out.append(_entry(current_fn, current_fn, "bash-fn", "",
                              line.strip(), i, 0, doc))
            continue
        match = _SH_PLAIN_RE.match(line)
        if match and not line.strip().startswith("#"):
            current_fn = match.group(1)
            doc = ""
            if i > 1 and lines[i - 2].strip().startswith("#"):
                doc = lines[i - 2].strip().lstrip("#").strip()
            out.append(_entry(current_fn, current_fn, "bash-fn", "",
                              line.strip(), i, 0, doc))
    in_case = False
    case_subject = ""
    case_fn = ""
    current_fn = ""
    for i, line in enumerate(lines, 1):
        def_match = _SH_DEF_RE.match(line) or _SH_PLAIN_RE.match(line)
        if def_match:
            current_fn = def_match.group(1)
            continue
        case_match = _SH_CASE_RE.match(line)
        if case_match:
            in_case = True
            case_subject = case_match.group(1)
            case_fn = current_fn
            continue
        if in_case and line.strip() == "esac":
            in_case = False
            case_subject = ""
            continue
        if in_case:
            arm = _SH_ARM_RE.match(line)
            if arm and (_SH_DISPATCH_RE.search(case_subject)
                        or case_fn in _SH_DISPATCH_FN):
                for part in arm.group(1).split("|"):
                    verb = part.strip().strip("\"'")
                    if (verb and not verb.startswith("*")
                            and re.fullmatch(r"[\w+.\-]+", verb)):
                        out.append(_entry(verb, verb, "cli-verb", "",
                                          "case arm: " + verb, i, i, ""))
    return _sort_entries(_fill_ends(out, total))


_LUA_LOCAL_FUNC_RE = re.compile(
    r"^\s*local\s+function\s+([A-Za-z_]\w*)\s*\(")
_LUA_LOCAL_ASSIGN_RE = re.compile(
    r"^\s*local\s+([A-Za-z_][\w.]*)\s*=\s*function\b")
_LUA_PLAIN_RE = re.compile(
    r"^\s*function\s+([A-Za-z_][\w.:]*)\s*\(")
_LUA_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z_][\w.:]*)\s*=\s*function\b")


def _lua_parent(name):
    for sep in (":", "."):
        if sep in name:
            return name.rsplit(sep, 1)[0]
    return ""


def _lua_outline(text):
    lines = text.splitlines()
    total = len(lines)
    out = []
    for i, line in enumerate(lines, 1):
        name = None
        match = _LUA_LOCAL_FUNC_RE.match(line)
        if match:
            name = match.group(1)
        elif _LUA_LOCAL_ASSIGN_RE.match(line):
            name = _LUA_LOCAL_ASSIGN_RE.match(line).group(1)
        elif _LUA_PLAIN_RE.match(line):
            name = _LUA_PLAIN_RE.match(line).group(1)
        elif _LUA_ASSIGN_RE.match(line):
            name = _LUA_ASSIGN_RE.match(line).group(1)
        if name:
            doc = ""
            if i > 1:
                prev = lines[i - 2].strip()
                if prev.startswith("--"):
                    doc = prev.lstrip("-").strip()
            out.append(_entry(name, name, "lua-fn", _lua_parent(name),
                              line.strip(), i, 0, doc))
    return _sort_entries(_fill_ends(out, total))


_PS1_DEF_RE = re.compile(
    r"^\s*function\s+(?:(global|script|local|private):)?"
    r"([A-Za-z_][\w\-]*)\s*(?:\{|\(|$)")


def _ps1_outline(text):
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        match = _PS1_DEF_RE.match(line)
        if match:
            scope, name = match.group(1) or "", match.group(2)
            out.append(_entry(name, name, "ps1-fn", scope,
                              line.strip(), i, 0, ""))
    return _sort_entries(_fill_ends(out, len(lines)))


_JS_FUNC_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
_JS_CONST_FN_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")
_JS_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")


def _js_outline(text):
    lines = text.splitlines()
    out = []
    seen = set()
    for i, line in enumerate(lines, 1):
        for match in _JS_FUNC_RE.finditer(line):
            name = match.group(1)
            if (name, i) not in seen:
                seen.add((name, i))
                out.append(_entry(name, name, "js-fn", "",
                                  line.strip(), i, 0, ""))
        match = _JS_CONST_FN_RE.search(line)
        if match and (match.group(1), i) not in seen:
            seen.add((match.group(1), i))
            out.append(_entry(match.group(1), match.group(1), "js-fn",
                              "", line.strip(), i, 0, ""))
        match = _JS_CLASS_RE.search(line)
        if match and (match.group(1), i) not in seen:
            seen.add((match.group(1), i))
            out.append(_entry(match.group(1), match.group(1), "js-fn",
                              "", line.strip(), i, 0, ""))
    return _sort_entries(_fill_ends(out, len(lines)))


_PHP_FUNC_RE = re.compile(r"\bfunction\s+([A-Za-z_]\w*)\s*\(")
_PHP_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_]\w*)")


def _php_outline(text):
    lines = text.splitlines()
    out = []
    seen = set()
    for i, line in enumerate(lines, 1):
        for regex in (_PHP_FUNC_RE, _PHP_CLASS_RE):
            for match in regex.finditer(line):
                key = (match.group(1), i)
                if key not in seen:
                    seen.add(key)
                    out.append(_entry(match.group(1), match.group(1),
                                      "php-fn", "", line.strip(), i, 0,
                                      ""))
    return _sort_entries(_fill_ends(out, len(lines)))

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _md_outline(text):
    lines = text.splitlines()
    total = len(lines)
    headings = []
    for i, line in enumerate(lines, 1):
        match = _MD_HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            doc = ""
            for follow in lines[i:]:
                stripped = follow.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    break
                doc = stripped
                break
            headings.append((title, level, i, line.strip(), doc))
    out = []
    for idx, (title, level, start, sig, doc) in enumerate(headings):
        end = total
        for (_, other_level, other_start, _, _) in headings[idx + 1:]:
            if other_level <= level:
                end = other_start - 1
                break
        parent = ""
        for (prev_title, prev_level, _, _, _) in reversed(
                headings[:idx]):
            if prev_level < level:
                parent = prev_title
                break
        out.append(_entry(title, title, "section", parent, sig, start,
                          max(start, end), doc))
    return _sort_entries(out)


_TOML_SECTION_RE = re.compile(r"^\s*\[\[?\s*([^\]#]+?)\s*\]?\s*(?:#.*)?$")
_TOML_KEY_RE = re.compile(r"^\s*(?:\"([^\"]+)\"|'([^']+)'|([\w.\-]+))"
                           r"\s*=")


def _toml_outline(text):
    lines = text.splitlines()
    section = []
    out = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        section_match = _TOML_SECTION_RE.match(line)
        if section_match:
            section = [p for p in section_match.group(1).split(".")
                       if p]
            continue
        key_match = _TOML_KEY_RE.match(line)
        if key_match:
            key = (key_match.group(1) or key_match.group(2)
                   or key_match.group(3))
            path = section + [key]
            if len(path) <= MAX_DEPTH:
                dotted = ".".join(path)
                parent = ".".join(path[:-1])
                out.append(_entry(dotted, dotted, "config-key",
                                  parent, dotted + " = ...", i, i, ""))
    return _sort_entries(out)


def _yaml_outline(text):
    lines = text.splitlines()
    out = []
    stack = []
    for i, line in enumerate(lines, 1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        expanded = line.expandtabs(8)
        indent = len(expanded) - len(expanded.lstrip(" "))
        body = expanded.strip()
        if body in ("---", "..."):
            continue
        if body.startswith("- "):
            body = body[2:].strip()
        match = re.match(r"^(?:\"([^\"]+)\"|'([^']+)'|([\w.\-]+))\s*:",
                         body)
        if not match:
            continue
        key = match.group(1) or match.group(2) or match.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = [k for _, k in stack] + [key]
        stack.append((indent, key))
        if len(path) <= MAX_DEPTH:
            dotted = ".".join(path)
            parent = ".".join(path[:-1])
            out.append(_entry(dotted, dotted, "config-key", parent,
                              dotted + " = ...", i, i, ""))
    return _sort_entries(out)


def _json_walk(node, path, acc):
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            child = path + [key]
            if len(child) <= MAX_DEPTH:
                acc.append(".".join(child))
            _json_walk(value, child, acc)
    elif isinstance(node, list):
        for item in node:
            _json_walk(item, path, acc)


def _json_outline(text):
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return []
    paths = []
    _json_walk(doc, [], paths)
    lasts = [dotted.rsplit(".", 1)[-1] for dotted in paths]
    first = _first_lines(text, lasts)
    out = []
    for dotted, last in zip(paths, lasts):
        line = first.get(last, 1)
        parent = dotted.rsplit(".", 1)[0] if "." in dotted else ""
        out.append(_entry(dotted, dotted, "config-key", parent,
                          dotted + " = ...", line, line, ""))
    return _sort_entries(out)


_UNIT_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_UNIT_KEY_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*=(.*)$")
_UNIT_TARGET_KEYS = {"ExecStart", "ExecStartPre", "ExecStartPost",
                     "ExecReload", "ExecStop", "ExecCondition",
                     "ExecSearchPath"}


def _unit_outline(text):
    lines = text.splitlines()
    section = ""
    out = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        section_match = _UNIT_SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1).strip()
            continue
        key_match = _UNIT_KEY_RE.match(line)
        if key_match and section:
            key, value = key_match.group(1), key_match.group(2).strip()
            name = section + "." + key
            if key in _UNIT_TARGET_KEYS:
                target = value.strip().strip("\"'").lstrip("-")
                target = target.strip().split()[0] if target.split() \
                    else key
                signature = target
            else:
                signature = name + " = ..."
            out.append(_entry(name, name, "unit-key", section,
                              signature, i, i, ""))
    return _sort_entries(out)


def _driver_outline(text):
    found = []
    if ET is not None:
        try:
            root = ET.fromstring(text)
            for elem in root.iter("command"):
                ident = (elem.get("id") or elem.get("name") or "").strip()
                if ident:
                    found.append(("driver-command", ident))
            for elem in root.iter("property"):
                ident = (elem.get("id") or elem.get("name") or "").strip()
                if ident:
                    found.append(("driver-property", ident))
        except ET.ParseError:
            found = []
    if not found:
        for kind, tag in (("driver-command", "command"),
                          ("driver-property", "property")):
            for match in re.finditer(
                    r"<%s\b[^>]*(?:\bid|\bname)\s*=\s*\"([^\"]+)\"" % tag,
                    text):
                found.append((kind, match.group(1)))
    out = []
    seen = set()
    for kind, ident in found:
        if ident in seen:
            continue
        seen.add(ident)
        line = _find_line(text, ident)
        out.append(_entry(ident, ident, kind, "",
                          kind + ": " + ident, line, line, ""))
    return _sort_entries(out)


_GENERIC_CONFIG_RE = re.compile(r"^\s*([\w.\-]+)\s*[:=]")
_GENERIC_CONFIG_EXTS = {".conf", ".cfg", ".ini", ".env", ".properties"}


def _generic_config_outline(text):
    lines = text.splitlines()
    out = []
    seen = set()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if (not stripped or stripped.startswith(("#", ";", "[", "//"))
                or stripped.startswith("--") or "=" not in line
                and ":" not in line):
            continue
        match = _GENERIC_CONFIG_RE.match(line)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            out.append(_entry(match.group(1), match.group(1),
                              "config-key", "",
                              match.group(1) + " = ...", i, i, ""))
    return _sort_entries(out)


_CONFIG_EXTS = {".toml", ".yaml", ".yml", ".json"}
_UNIT_EXTS = {".service", ".timer", ".socket", ".target", ".path",
              ".mount", ".automount", ".swap"}


# Committed generated-graph artefacts: outlining them cost 12.5 s per
# cache warm for zero signal (S1b). No content outline for these paths;
# purpose() still answers (usually "").
_GENERATED_OUT = "documentation/architecture/codebase-graph"
_DATA_CONFIG_EXTS = (".json", ".yaml", ".yml")
_DATA_CONFIG_MAX = 256 * 1024


def _skip_outline(relpath, text):
    low = relpath.lower()
    if (low.startswith("graphs/") and low.endswith((".json", ".html"))):
        return True
    if low.endswith((_GENERATED_OUT + ".json",
                     _GENERATED_OUT + ".html")):
        return True
    if low.endswith((".min.js", ".min.css")):
        return True
    if low.endswith(_DATA_CONFIG_EXTS) and len(text) > _DATA_CONFIG_MAX:
        return True
    return False


def outline(relpath, text):
    """Stdlib-only committed outline for one file (deterministic)."""
    if not text or len(text) > BIG_FILE:
        return []
    if _skip_outline(relpath, text):
        return []
    base = relpath.rsplit("/", 1)[-1]
    low = base.lower()
    if base == "driver.xml":
        return _driver_outline(text)
    _, dot, ext = low.rpartition(".")
    ext = ("." + ext) if dot else ""
    if ext == ".py":
        return _py_outline(text)
    if ext == ".sh":
        return _sh_outline(text)
    if ext == ".lua":
        return _lua_outline(text)
    if ext in (".ps1", ".psm1"):
        return _ps1_outline(text)
    if ext in (".js", ".mjs", ".cjs"):
        return _js_outline(text)
    if ext == ".php":
        return _php_outline(text)
    if low.endswith(".md") or low.endswith(".markdown"):
        return _md_outline(text)
    if ext == ".toml":
        return _toml_outline(text)
    if ext in (".yaml", ".yml"):
        return _yaml_outline(text)
    if ext == ".json":
        return _json_outline(text)
    if ext in _UNIT_EXTS:
        return _unit_outline(text)
    if ext in _GENERIC_CONFIG_EXTS:
        return _generic_config_outline(text)
    return []

_TEST_BASENAMES = ("test_", "_test.")
_TEST_SUFFIXES = (".py", ".js", ".go", ".lua", ".sh", ".ps1", ".php")


def _is_test_file(relpath):
    parts = relpath.split("/")
    if "tests" in parts or "test" in parts:
        return True
    base = parts[-1]
    if base.startswith("test_") or base.startswith("test-"):
        return True
    stem, _, _ = base.rpartition(".")
    return stem.endswith("_test") or base.endswith(".test.js")


_PY_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+)")
_PY_FROM_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+")
_SH_SOURCE_RE = re.compile(
    r"(?:^|[;|&]\s*|\s)(?:source|\.)\s+([^\s;|&\"]+)")
_SH_RUN_RE = re.compile(
    r"(?:^|[;|&]\s*|\s)(?:bash|sh|python3?|lua|luajit|perl)\s+"
    r"([^\s;|&\"]+)")
_LUA_REQUIRE_RE = re.compile(
    r"\brequire\s*(?:\(\s*)?[\"']([\w.]+)[\"']")
_PHP_INCLUDE_RE = re.compile(
    r"\b(?:require|include)(?:_once)?\s*\(?\s*[\"']([^\"']+)[\"']")
_JS_IMPORT_RE = re.compile(
    r"\b(?:import\s+(?:.*?\s+from\s+)?|require\()\s*[\"']([^\"']+)[\"']")


def _mod_to_file(mod):
    return "file:" + mod.replace(".", "/") + ".py"


def _enclosing(relpath, outline, line, file_id):
    best = None
    for entry in outline:
        if entry["kind"] in ("section", "config-key", "unit-key",
                             "cli-verb"):
            continue
        if entry["line_start"] <= line <= entry["line_end"]:
            best = entry
    if best is None:
        return file_id
    return "file:" + relpath + "#" + best["qualname"]


def _py_calls_imports(text):
    imports = []
    calls = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return imports, calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append((node.lineno, node.module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                calls.append((node.lineno, func.id))
            elif isinstance(func, ast.Attribute):
                calls.append((node.lineno, func.attr))
    return imports, calls


def _sh_calls_imports(text, outline):
    lines = text.splitlines()
    defined = {e["name"] for e in outline if e["kind"] == "bash-fn"}
    imports = []
    calls = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for match in _SH_SOURCE_RE.finditer(line):
            target = match.group(1)
            if not target.startswith(("$", "<", ">")):
                imports.append((i, target))
        for match in _SH_RUN_RE.finditer(line):
            imports.append((i, match.group(1)))
        if _SH_DEF_RE.match(line) or _SH_PLAIN_RE.match(line):
            continue
        first = re.match(r"^([A-Za-z_][A-Za-z0-9_\-:]*)", stripped)
        if first and first.group(1) in defined:
            calls.append((i, first.group(1)))
    return imports, calls


def _lua_calls_imports(text):
    lines = text.splitlines()
    imports = []
    calls = []
    for i, line in enumerate(lines, 1):
        for match in _LUA_REQUIRE_RE.finditer(line):
            imports.append((i, match.group(1).replace(".", "/") + ".lua"))
        for match in re.finditer(
                r"\b([A-Za-z_]\w*(?:[.:][A-Za-z_]\w*)*)\s*\(", line):
            if _LUA_LOCAL_FUNC_RE.match(line) or _LUA_PLAIN_RE.match(
                    line):
                continue
            full = match.group(1)
            calls.append((i, full))
    return imports, calls


def _js_calls_imports(text):
    lines = text.splitlines()
    imports = []
    calls = []
    for i, line in enumerate(lines, 1):
        for match in _JS_IMPORT_RE.finditer(line):
            imports.append((i, match.group(1)))
        for match in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*\(", line):
            if _JS_FUNC_RE.search(line) or _JS_CLASS_RE.search(line):
                continue
            calls.append((i, match.group(1)))
    return imports, calls


def _php_calls_imports(text):
    lines = text.splitlines()
    imports = []
    calls = []
    for i, line in enumerate(lines, 1):
        for match in _PHP_INCLUDE_RE.finditer(line):
            imports.append((i, match.group(1)))
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", line):
            if _PHP_FUNC_RE.search(line) or _PHP_CLASS_RE.search(line):
                continue
            calls.append((i, match.group(1)))
    return imports, calls


def _try_ts_calls(relpath, text):
    """Best-effort tree-sitter call names; ([(line, name)], ok).

    Imported lazily so plain `import graph_symbols` stays stdlib-only.
    Never raises: any failure means the stdlib result stands alone.
    """
    try:
        import tree_sitter  # noqa: F401
        if relpath.endswith(".py"):
            import tree_sitter_python
            lang = tree_sitter.Language(tree_sitter_python.language())
        elif relpath.endswith(".sh"):
            import tree_sitter_bash
            lang = tree_sitter.Language(tree_sitter_bash.language())
        elif relpath.endswith(".lua"):
            import tree_sitter_lua
            lang = tree_sitter.Language(tree_sitter_lua.language())
        else:
            return [], True
    except Exception:
        return [], False
    try:
        from tree_sitter import Parser
        parser = Parser(lang)
        tree = parser.parse(text.encode("utf-8", errors="replace"))
    except Exception:
        return [], True
    found = []

    def walk(node):
        yield node
        for child in node.children:
            yield from walk(child)

    try:
        for node in walk(tree.root_node):
            if node.type == "call":
                fn = node.child_by_field_name("function")
                if fn is None:
                    continue
                raw = text.encode("utf-8", errors="replace")[
                    fn.byte_range[0]:fn.byte_range[1]].decode(
                        "utf-8", errors="replace")
                name = raw.split(".")[-1].strip("() ")
                if re.fullmatch(r"[A-Za-z_]\w*", name):
                    found.append(
                        (fn.start_point[0] + 1, name))
            elif node.type == "command" and relpath.endswith(".sh"):
                if node.children:
                    first = node.children[0]
                    raw = text.encode("utf-8", errors="replace")[
                        first.byte_range[0]:first.byte_range[1]].decode(
                            "utf-8", errors="replace").split()[0:1]
                    if raw and re.fullmatch(r"[A-Za-z_][\w\-:]*", raw[0]):
                        found.append((first.start_point[0] + 1, raw[0]))
    except Exception:
        return [], True
    return found, True


def edges(relpath, text, outline, known):
    """Per-file cached edges; returns (edges, backend).

    backend is "tree-sitter" when the pinned grammars import (their
    calls augment the stdlib set), else "stdlib".
    """
    file_id = "file:" + relpath
    known = known or {}
    low = relpath.lower()
    raw_imports = []
    raw_calls = []
    if low.endswith(".py"):
        raw_imports, raw_calls = _py_calls_imports(text)
    elif low.endswith(".sh"):
        raw_imports, raw_calls = _sh_calls_imports(text, outline or [])
    elif low.endswith(".lua"):
        raw_imports, raw_calls = _lua_calls_imports(text)
    elif low.endswith((".js", ".mjs", ".cjs")):
        raw_imports, raw_calls = _js_calls_imports(text)
    elif low.endswith(".php"):
        raw_imports, raw_calls = _php_calls_imports(text)
    ts_extra, ts_ok = _try_ts_calls(relpath, text or "")
    backend = "tree-sitter" if ts_ok else "stdlib"
    if ts_ok:
        seen_calls = set(raw_calls)
        for item in ts_extra:
            if item not in seen_calls:
                seen_calls.add(item)
                raw_calls.append(item)
    is_test = _is_test_file(relpath)

    def resolve(name):
        targets = known.get(name)
        if targets:
            return sorted(set(targets))
        short = name.split(".")[-1].split(":")[-1]
        if short != name:
            targets = known.get(short)
            if targets:
                return sorted(set(targets))
        return []

    out = []
    for line, name in raw_calls:
        for dst in resolve(name):
            rel = "tests" if is_test else "calls"
            out.append({"src": _enclosing(relpath, outline or [], line,
                                     file_id),
                        "dst": dst, "rel": rel, "line": int(line)})
    if not is_test:
        for line, mod in raw_imports:
            if low.endswith(".py") and "." in mod:
                dst = _mod_to_file(mod)
            elif low.endswith(".py"):
                dst = "file:" + mod
            else:
                dst = "file:" + mod
            out.append({"src": file_id, "dst": dst, "rel": "imports",
                        "line": int(line)})
    else:
        for line, mod in raw_imports:
            short = mod.split(".")[-1].split("/")[-1]
            for dst in resolve(mod) or resolve(short):
                out.append({"src": file_id, "dst": dst, "rel": "tests",
                            "line": int(line)})
    out.sort(key=lambda e: (e["src"], e["dst"], e["rel"], e["line"]))
    return out, backend


def _read_file(path):
    if not os.path.isfile(path):
        return None
    try:
        if os.path.getsize(path) > BIG_FILE:
            return None
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(BIG_FILE + 1)
    except OSError:
        return None


def _selftest():
    failures = []

    def check(name, cond, detail=""):
        if not cond:
            failures.append(name)
            print("FAIL " + name + (" -- " + detail if detail else ""))

    check("version", EXTRACTOR_VERSION == "2")
    py = '"""Mod purpose."""\nimport os\n\n\ndef f(a, b="lit"):\n' \
        '    """Do f."""\n    return g(a)\n'
    out = outline("m.py", py)
    by_name = {e["name"]: e for e in out}
    check("py-fn", by_name.get("f", {}).get("kind") == "function")
    check("no-literals", "lit" not in json.dumps(out))
    check("sorted", [(e["line_start"], e["name"]) for e in out]
          == sorted([(e["line_start"], e["name"]) for e in out]))
    lua = ("local function h(x)\nreturn x\nend\n"
           "stop = function()\nreturn 1\nend\n")
    lua_out = outline("m.lua", lua)
    check("lua", sorted(e["name"] for e in lua_out) == ["h", "stop"])
    check("lua-twice", outline("m.lua", lua)
          == outline("m.lua", lua))
    sec = outline("s.conf", 'api_key = "supersecret"\nport = 1\n')
    check("secret-names", any(e["name"] == "api_key" for e in sec)
          and "supersecret" not in json.dumps(sec))
    known = {"g": ["symbol:m.py#g"]}
    py_edges, backend = edges("m.py", py, out, known)
    check("backend", backend in ("stdlib", "tree-sitter"), backend)
    check("call", any(e["dst"] == "symbol:m.py#g"
                      and e["rel"] == "calls" for e in py_edges),
          json.dumps(py_edges))
    check("drop", all("nosuch" not in e["dst"] for e in py_edges))
    if failures:
        print("SELFTEST FAIL (%d)" % len(failures))
        return 1
    print("SELFTEST PASS backend=%s version=%s"
          % (backend, EXTRACTOR_VERSION))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="graph_symbols.py")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("verb", nargs="?")
    parser.add_argument("path", nargs="?")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.verb not in ("outline", "edges", "purpose") or not args.path:
        parser.print_usage(sys.stderr)
        return 2
    text = _read_file(args.path)
    if text is None:
        print("not found: %s" % args.path, file=sys.stderr)
        return 1
    rel = args.path
    if args.verb == "purpose":
        result = purpose(rel, text)
        if args.json:
            print(json.dumps(result))
        else:
            print(result)
        return 0
    if args.verb == "outline":
        result = outline(rel, text)
        if args.json:
            print(json.dumps(result, indent=1, sort_keys=True))
        else:
            print(json.dumps(result, sort_keys=True))
        return 0
    out = outline(rel, text)
    known = {}
    for entry in out:
        sid = "symbol:%s#%s" % (rel, entry["qualname"])
        known.setdefault(entry["qualname"], []).append(sid)
        known.setdefault(entry["name"], []).append(sid)
    edge_list, backend = edges(rel, text, out, known)
    print(json.dumps({"edges": edge_list, "backend": backend},
                     indent=1 if args.json else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
