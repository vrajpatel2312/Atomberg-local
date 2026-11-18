#!/usr/bin/env python3
"""Diagnostics: gap summaries and graph drift for the template core.

Gaps are grouped by id prefix (skip:/dedup:/unresolved:/secret-name:/...);
drift diffs two graphs (added/removed node ids and edge triples).
Stdlib only; output carries paths and names, never secret values.
"""

GAP_CATEGORIES = ("skip", "dedup", "unresolved", "secret-name",
                  "empty-checkout")


def gap_category(gap_id):
    for cat in GAP_CATEGORIES:
        if gap_id == cat or gap_id.startswith(cat + ":"):
            return cat
    return "other"


def summarize_gaps(doc):
    """{category: count} over doc gaps."""
    counts = {}
    for gap in doc.get("gaps", []):
        cat = gap_category(gap.get("id", ""))
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def diff_graphs(old, new):
    """Drift between two graph docs: added/removed nodes and edges."""
    old_nodes = {n["id"] for n in old.get("nodes", [])}
    new_nodes = {n["id"] for n in new.get("nodes", [])}

    def triple(e):
        return (e["src"], e["dst"], e["rel"])

    old_edges = {triple(e) for e in old.get("edges", [])}
    new_edges = {triple(e) for e in new.get("edges", [])}
    return {
        "added_nodes": sorted(new_nodes - old_nodes),
        "removed_nodes": sorted(old_nodes - new_nodes),
        "added_edges": sorted(new_edges - old_edges),
        "removed_edges": sorted(old_edges - new_edges),
    }


def render(doc, old=None, label="graph", old_label="old"):
    """Human-readable diagnostics report."""
    lines = ["diagnostics: %s" % label,
             "nodes: %d  edges: %d  sources: %d  gaps: %d  drift: %d"
             % (len(doc.get("nodes", [])), len(doc.get("edges", [])),
                len(doc.get("sources", [])), len(doc.get("gaps", [])),
                len(doc.get("drift", [])))]
    gaps = summarize_gaps(doc)
    if gaps:
        lines.append("gaps by category:")
        for cat in sorted(gaps):
            lines.append("  %s: %d" % (cat, gaps[cat]))
    else:
        lines.append("gaps: none")
    if old is not None:
        drift = diff_graphs(old, doc)
        total = sum(len(drift[k]) for k in drift)
        lines.append("drift vs %s: %d change(s)" % (old_label, total))
        for key in ("added_nodes", "removed_nodes",
                    "added_edges", "removed_edges"):
            items = drift[key]
            lines.append("  %s: %d" % (key, len(items)))
            for item in items[:20]:
                lines.append("    %s" % (item,))
            if len(items) > 20:
                lines.append("    ... (%d more)" % (len(items) - 20))
    return "\n".join(lines) + "\n"
