"""
Course-level summary.

Everything built so far produces evidence and per-strategy files, but
nothing that answers "what's actually in this course, overall?" This
generates ONE document per course covering every session combined --
total strategy count, concepts/theory covered, key points -- not instead
of the per-strategy files, but as the overview on top of them.

Deliberately NOT a new AI call -- it aggregates what tagging (stage 2)
and reconstruction (stage 4) already produced, using a heuristic to split
"Strategy" (has real entry/exit/stop rules) from "Concept / Theory"
(everything else -- market philosophy, indicator explanations, general
principles) since real course material genuinely contains both, and
forcing a walking-exercise or a market-mindset note into an entry/exit
template doesn't fit. Near-zero extra cost since no new API calls happen.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

TRADE_RULE_FIELDS = ["entry_conditions", "exit_conditions", "stop_loss", "target"]


def classify_strategy_type(spec: dict) -> str:
    """Heuristic, not an AI call: if at least 2 of the 4 core trade-rule
    fields have real content (not 'INSUFFICIENT EVIDENCE' or empty), this
    is a tradeable Strategy. Otherwise it's Concept/Theory material --
    still valuable, just not something with its own entry/exit rules."""
    real_count = sum(
        1 for f in TRADE_RULE_FIELDS
        if spec.get(f) and str(spec[f]).strip().upper() != "INSUFFICIENT EVIDENCE"
    )
    return "Strategy" if real_count >= 2 else "Concept / Theory"


def generate_course_summary(conn, course_id: str, output_course_root: str) -> dict:
    strategies = conn.execute(
        """SELECT strategy_id, name, maturity, approved, spec_json, confidence_category, version
           FROM strategies WHERE course_id = ?""",
        (course_id,),
    ).fetchall()

    # course-wide evidence stats, for the overview section
    session_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE course_id = ?", (course_id,)
    ).fetchone()[0]
    transcript_count = conn.execute(
        """SELECT COUNT(*) FROM transcript_segments ts
           JOIN sessions s ON s.session_id = ts.session_id WHERE s.course_id = ?""",
        (course_id,),
    ).fetchone()[0]
    flagged_count = conn.execute(
        """SELECT COUNT(*) FROM transcript_segments ts
           JOIN sessions s ON s.session_id = ts.session_id
           WHERE s.course_id = ? AND ts.needs_review = 1""",
        (course_id,),
    ).fetchone()[0]
    material_counts = dict(conn.execute(
        """SELECT file_type, COUNT(*) FROM files
           WHERE course_id = ? AND file_type IN ('excel','csv','pdf','pptx','image','doc')
           GROUP BY file_type""",
        (course_id,),
    ).fetchall())

    strategy_items = []
    concept_items = []
    for strategy_id, name, maturity, approved, spec_json, confidence_category, version in strategies:
        spec = json.loads(spec_json) if spec_json else {}
        excel_dep = spec.get("excel_dependency")
        indicator_dep = spec.get("indicator_dependency")
        external_refs = spec.get("external_reference_dependency") or []
        item = {
            "name": name,
            "purpose": spec.get("purpose", ""),
            "instrument": spec.get("instrument", ""),
            "approved": bool(approved),
            "open_questions": spec.get("open_questions", []),
            "citation_count": len(spec.get("source_citations", [])),
            "excel_required": bool(isinstance(excel_dep, dict) and excel_dep.get("required")),
            "excel_workbook": excel_dep.get("workbook") if isinstance(excel_dep, dict) else None,
            "indicator_required": bool(isinstance(indicator_dep, dict) and indicator_dep.get("required")),
            "indicator_name": indicator_dep.get("name") if isinstance(indicator_dep, dict) else None,
            "external_refs": external_refs,
            "confidence": confidence_category,
            "version": version,
            "numeric_check": any(
                isinstance(spec.get(f), str) and any(ch.isdigit() for ch in spec.get(f))
                for f in ("entry_conditions", "exit_conditions", "stop_loss", "target")
            ),
        }
        if classify_strategy_type(spec) == "Strategy":
            strategy_items.append(item)
        else:
            concept_items.append(item)

    lines = [
        f"# Course Summary — {course_id}",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Overview",
        f"- Sessions processed: {session_count}",
        f"- Transcript segments: {transcript_count} ({flagged_count} flagged for review)",
        f"- Materials processed: "
        + ", ".join(f"{v} {k}" for k, v in material_counts.items()) or "none",
        f"- **Total strategies identified: {len(strategy_items)}**",
        f"- **Total concepts / theory topics identified: {len(concept_items)}**",
        "",
    ]

    lines.append("## Strategies")
    lines.append("")
    if strategy_items:
        for s in strategy_items:
            status = "✅ Approved" if s["approved"] else "⚠️ Needs review" if s["open_questions"] else "Pending review"
            lines.append(f"### {s['name']}")
            lines.append(f"- Status: {status}")
            if s["confidence"]:
                conflict_note = "  ⚠️ **contradictory evidence across sessions -- resolve before trusting this strategy**" if s["confidence"] == "CONFLICT" else ""
                lines.append(f"- Evidence confidence: {s['confidence']}{conflict_note}")
            if s["version"] and s["version"] > 1:
                lines.append(f"- Version: {s['version']} (revised since first reconstructed -- see strategy_versions for history)")
            if s.get("numeric_check"):
                lines.append("- ⚠️ Contains specific numbers (points/percent/levels) in its rules — recommend a quick spot-check against the original audio or document, since transcription can occasionally mishear a number without it looking wrong.")
            if s["instrument"]:
                lines.append(f"- Instrument: {s['instrument']}")
            if s["purpose"]:
                lines.append(f"- Purpose: {s['purpose']}")
            if s["excel_required"]:
                lines.append(f"- Excel dependency: {s['excel_workbook'] or 'required (workbook not confirmed)'}")
            if s["indicator_required"]:
                lines.append(f"- Indicator dependency: {s['indicator_name'] or 'required (name not confirmed)'}")
            if s["external_refs"]:
                for ref in s["external_refs"]:
                    if not isinstance(ref, dict):
                        continue
                    marker = "🔗 **essential**" if ref.get("essential") else "🔗 supplementary"
                    lines.append(f"- External reference ({marker}): {ref.get('reference', '?')} — {ref.get('purpose', '')}")
            lines.append(f"- Evidence citations: {s['citation_count']}")
            if s["open_questions"]:
                lines.append(f"- Open questions: {'; '.join(s['open_questions'])}")
            lines.append(f"- Full details: `04_STRATEGY_RECONSTRUCTION/{s['name']}.json`")
            lines.append("")
    else:
        lines.append("_None identified yet -- run the AI stages if extraction is complete._")
        lines.append("")

    lines.append("## Concepts / Theory")
    lines.append("")
    if concept_items:
        for c in concept_items:
            lines.append(f"### {c['name']}")
            if c["purpose"]:
                lines.append(f"- {c['purpose']}")
            if c["confidence"] == "CONFLICT":
                lines.append(f"- Evidence confidence: CONFLICT  ⚠️ **contradictory evidence across sessions**")
            lines.append(f"- Evidence citations: {c['citation_count']}")
            lines.append("")
    else:
        lines.append("_None identified separately from strategies._")
        lines.append("")

    content = "\n".join(lines)
    out_dir = Path(output_course_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "Course_Summary.md"
    out_path.write_text(content, encoding="utf-8")

    return {
        "strategies": len(strategy_items),
        "concepts": len(concept_items),
        "path": str(out_path),
    }
