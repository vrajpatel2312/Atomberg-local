#!/usr/bin/env bash
# Self-test for .github/workflows/graph-freshness.yml (graph-freshness gate).
#
# Fully read-only: the repo tree is never modified; all scratch lives in /tmp.
# Run from repo root:  bash .github/tests/test-graph-freshness.sh
#
# Mirrors the four workflow steps (artifacts / schema / rebuild / fingerprint)
# plus the workflow-file static contract (read-only permissions, no writes,
# site exclusion). Follows TDD: this test was written before the workflow
# existed and failed on the missing file first.
set -u

PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1"; }

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WF="$ROOT/.github/workflows/graph-freshness.yml"
GRAPH_REL="Documentation/architecture/codebase-graph.json"
PROJECT="$(python3 -c "import json;print(json.load(open('$ROOT/graph.config.json')).get('project',''))")"

tracked_clean() { [ -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; }

# --- check implementations (mirror of the workflow run: blocks) ---
check_artifacts() { # $1 = root
  local r="$1" missing=0 f
  for f in "$GRAPH_REL" graph.config.json manifest.schema.json \
           graph-template-VERSION generator/project_graph.py; do
    if [ ! -f "$r/$f" ]; then echo "MISSING: $f"; missing=1; fi
  done
  if [ "$missing" -eq 0 ]; then
    echo "artifacts present: FRESHNESS_ARTIFACTS_OK"; return 0
  fi
  return 1
}

check_schema() { # $1 = root
  python3 - "$1" <<'EOF'
import json, sys
root = sys.argv[1]
doc = json.load(open(root + '/Documentation/architecture/codebase-graph.json'))
want = ["nodes", "edges", "sources", "gaps", "drift",
        "generated_at", "source_commit"]
assert sorted(doc.keys()) == sorted(want), \
    "top-level keys: %s" % sorted(doc.keys())
for n in doc["nodes"]:
    assert set(("id", "type", "source", "attrs")) <= set(n), \
        "node keys: %s" % sorted(n.keys())
for e in doc["edges"]:
    assert set(("src", "dst", "rel", "evidence")) <= set(e), \
        "edge keys: %s" % sorted(e.keys())
print("schema OK (%d nodes, %d edges, %d sources): FRESHNESS_SCHEMA_OK"
      % (len(doc["nodes"]), len(doc["edges"]), len(doc["sources"])))
EOF
}

check_rebuild() { # $1 = root
  # NOTE: the generator ingests its own output file (the committed graph
  # lists itself under sources[] with its at-build-time sha, plus a self
  # node and self-derived secret-name gaps). That self-reference can never
  # byte-match across rebuilds, so both sides are normalized: volatile keys
  # (generated_at, source_commit) plus every entry derived from the graph
  # file itself are dropped before comparing. Everything describing real
  # sources is still byte-compared. Fix belongs in the template (exclude
  # the output path from collect(), mirroring the manifest exclusion);
  # this normalization stays valid after such a fix (nothing to drop).
  local r="$1" tmp rc
  tmp="$(mktemp -d)"
  rc=0
  if ! python3 "$r/generator/project_graph.py" build --root "$r" \
      --project "$PROJECT" --config "$r/graph.config.json" \
      --out "$tmp/fresh-graph.json" >/dev/null 2>"$tmp/build.err"; then
    echo "rebuild failed:"; head -5 "$tmp/build.err"; rc=1
  fi
  if [ "$rc" -eq 0 ]; then
    GRAPH_REL="$GRAPH_REL" python3 - "$r/$GRAPH_REL" "$tmp/fresh-graph.json" <<'EOF' || rc=1
import json, os, sys
committed = json.load(open(sys.argv[1]))
fresh = json.load(open(sys.argv[2]))
g = os.environ["GRAPH_REL"]
def norm(d):
    d = dict(d); d["generated_at"] = "T"; d["source_commit"] = "C"
    d["nodes"] = [n for n in d["nodes"]
                  if n.get("source") != g and n.get("attrs", {}).get("path") != g]
    d["sources"] = [s for s in d["sources"] if s["path"] != g]
    d["gaps"] = [x for x in d["gaps"]
                 if not x.get("evidence", "").startswith(g + ":")]
    return json.dumps(d, sort_keys=True)
if norm(committed) != norm(fresh):
    print("rebuild differs: committed (%d nodes, %d edges, %d sources)"
          " vs fresh (%d nodes, %d edges, %d sources)"
          % (len(committed.get("nodes", [])), len(committed.get("edges", [])),
             len(committed.get("sources", [])),
             len(fresh.get("nodes", [])), len(fresh.get("edges", [])),
             len(fresh.get("sources", []))))
    sys.exit(1)
print("rebuild identical (volatile keys + self-entries normalized): FRESHNESS_REBUILD_OK")
EOF
  fi
  rm -rf "$tmp"
  return "$rc"
}

check_fingerprint() { # $1 = root
  # NOTE: the graph file cannot fingerprint itself (it records its own
  # at-build-time sha), so its own sources[] entry is skipped here; its
  # freshness is proven by check_rebuild instead.
  python3 - "$1" <<'EOF'
import hashlib, json, sys
root = sys.argv[1]
graph_rel = 'Documentation/architecture/codebase-graph.json'
doc = json.load(open(root + '/' + graph_rel))
bad = []
checked = 0
for s in doc.get("sources", []):
    if s["path"] == graph_rel:
        continue
    checked += 1
    try:
        h = hashlib.sha256(open(root + "/" + s["path"], "rb").read()).hexdigest()
    except OSError:
        bad.append("%s MISSING" % s["path"]); continue
    if h != s.get("sha256"):
        bad.append("%s CHANGED" % s["path"])
if bad:
    print("stale fingerprint (%d):" % len(bad))
    print("\n".join(bad[:20]))
    sys.exit(1)
print("fingerprint OK (%d sources match, graph self-entry excluded): FRESHNESS_FINGERPRINT_OK"
      % checked)
EOF
}

echo "== static: workflow file contract =="
if [ -f "$WF" ]; then ok "workflow file exists"; else bad "workflow file exists ($WF missing)"; fi
if [ -f "$WF" ] && grep -q "contents: read" "$WF"; then
  ok "permissions grant only contents: read"
else
  bad "permissions grant only contents: read"
fi
if [ -f "$WF" ] && grep -qE ":[ ]*write" "$WF"; then
  bad "zero write grants (found ': write')"
else
  ok "zero write grants"
fi
if [ -f "$WF" ] && grep -qE "git (commit|push)|gh api|auto-commit|EndBug/add-and-commit" "$WF"; then
  bad "no commit/push steps"
else
  ok "no commit/push steps"
fi
for s in FRESHNESS_ARTIFACTS_OK FRESHNESS_SCHEMA_OK \
         FRESHNESS_REBUILD_OK FRESHNESS_FINGERPRINT_OK; do
  if [ -f "$WF" ] && grep -q "$s" "$WF"; then
    ok "workflow contains $s"
  else
    bad "workflow contains $s"
  fi
done
# NOTE: the site name is assembled from parts so this file itself never
# contains it literally (the absence check scans .github/ including us).
SITE_PAT="adi-""sri|acha""ry"
if grep -rEi "$SITE_PAT" "$ROOT/.github/" 2>/dev/null; then
  bad "site absent from .github/"
else
  ok "site absent from .github/"
fi

echo "== fresh tree (expect all green) =="
if tracked_clean; then ok "tree has no tracked modifications before checks"; else bad "tree has no tracked modifications before checks"; fi
if check_artifacts "$ROOT"; then ok "fresh: artifacts green"; else bad "fresh: artifacts green"; fi
if check_schema "$ROOT"; then ok "fresh: schema green"; else bad "fresh: schema green"; fi
if check_rebuild "$ROOT"; then ok "fresh: rebuild green"; else bad "fresh: rebuild green"; fi
if check_fingerprint "$ROOT"; then ok "fresh: fingerprint green"; else bad "fresh: fingerprint green"; fi
if python3 "$ROOT/generator/project_graph.py" --selftest >/dev/null 2>&1; then
  ok "fresh: generator --selftest green"
else
  bad "fresh: generator --selftest green"
fi

echo "== staled temp copies (expect red; tree untouched) =="
SRC0="$(python3 -c "import json;d=json.load(open('$ROOT/$GRAPH_REL'));print(next((s['path'] for s in d.get('sources',[]) if s['path']!='$GRAPH_REL'),''))")"
if [ -n "$SRC0" ] && [ -f "$ROOT/$SRC0" ]; then
  S1="$(mktemp -d)"; cp -a "$ROOT/." "$S1/copy"
  echo "# stale-test" >> "$S1/copy/$SRC0"
  if check_schema "$S1/copy" >/dev/null 2>&1; then ok "stale-source: schema stays green"; else bad "stale-source: schema stays green"; fi
  if check_fingerprint "$S1/copy" >/dev/null 2>&1; then bad "stale-source: fingerprint goes red"; else ok "stale-source: fingerprint goes red"; fi
  if check_rebuild "$S1/copy" >/dev/null 2>&1; then bad "stale-source: rebuild goes red"; else ok "stale-source: rebuild goes red"; fi
  rm -rf "$S1"
else
  skip "stale-source variant (no fingerprinted source file)"
fi
S2="$(mktemp -d)"; cp -a "$ROOT/." "$S2/copy"
python3 - "$S2/copy/$GRAPH_REL" <<'EOF'
import json, os, sys
p = sys.argv[1]
g = 'Documentation/architecture/codebase-graph.json'
doc = json.load(open(p))
real = [n for n in doc["nodes"]
        if n.get("source") != g and n.get("attrs", {}).get("path") != g]
assert real, "no non-self node to drop"
doc["nodes"] = [n for n in doc["nodes"] if n != real[0]]
json.dump(doc, open(p, "w"), indent=2, sort_keys=True)
open(p, "a").write("\n")
EOF
if check_schema "$S2/copy" >/dev/null 2>&1; then ok "edited-graph: schema stays green"; else bad "edited-graph: schema stays green"; fi
if check_fingerprint "$S2/copy" >/dev/null 2>&1; then ok "edited-graph: fingerprint stays green"; else bad "edited-graph: fingerprint stays green"; fi
if check_rebuild "$S2/copy" >/dev/null 2>&1; then bad "edited-graph: rebuild goes red"; else ok "edited-graph: rebuild goes red"; fi
rm -rf "$S2"
S3="$(mktemp -d)"; cp -a "$ROOT/." "$S3/copy"
: > "$S3/copy/$GRAPH_REL"
if check_schema "$S3/copy" >/dev/null 2>&1; then bad "corrupt-graph: schema goes red"; else ok "corrupt-graph: schema goes red"; fi
rm -rf "$S3"

echo "== tree still untouched =="
if tracked_clean; then ok "no tracked modifications after red+green cycle"; else bad "no tracked modifications after red+green cycle"; fi
if [ -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
  ok "git status clean (tracked)"
else
  bad "git status clean (tracked)"
fi

echo "---"
echo "selftest: $PASS pass, $FAIL fail, $SKIP skip"
[ "$FAIL" -eq 0 ]
