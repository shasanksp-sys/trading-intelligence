"""
Rough cost estimate for running the AI stages (tagging + reconstruction)
on a course, BEFORE actually spending anything. Shown to the user so
"I don't know how it will be charged" becomes a real number they approve
or decline, per course, before committing.

Deliberately conservative/approximate -- character count / 4 is a standard
rough proxy for token count. Treat this as a ballpark, not a quote; verify
current per-model pricing at platform.claude.com/pricing since rates can
change.
"""

# ~ per-million-token USD rates -- update if pricing changes
RATES = {
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
}


def estimate_course_ai_cost(conn, course_id: str) -> dict:
    """
    Estimates cost for tagging (all untagged evidence) + reconstruction
    (all strategies not yet reconstructed) for one course.
    """
    # tagging: proxy on total transcript + document extraction text length
    transcript_chars = conn.execute(
        """SELECT COALESCE(SUM(LENGTH(ts.text)), 0) FROM transcript_segments ts
           JOIN sessions s ON s.session_id = ts.session_id WHERE s.course_id = ?""",
        (course_id,),
    ).fetchone()[0]

    doc_chars = conn.execute(
        """SELECT COALESCE(SUM(LENGTH(de.content)), 0) FROM document_extractions de
           JOIN files f ON f.file_id = de.file_id WHERE f.course_id = ?""",
        (course_id,),
    ).fetchone()[0]

    total_evidence_chars = transcript_chars + doc_chars
    tagging_input_tokens = total_evidence_chars / 4
    tagging_output_tokens = tagging_input_tokens * 0.05  # tags are short relative to input
    tagging_rate = RATES["claude-haiku-4-5-20251001"]
    tagging_cost = (tagging_input_tokens / 1_000_000 * tagging_rate["input"]) + \
                   (tagging_output_tokens / 1_000_000 * tagging_rate["output"])

    # reconstruction: proxy on estimated number of distinct strategies.
    # Without having tagged yet, assume roughly 1 strategy per ~15-20 minutes
    # of unique-topic transcript content as a rough starting heuristic --
    # this genuinely firms up only after tagging actually runs once.
    est_strategy_count = max(1, int(transcript_chars / 4 / 3000))  # very rough
    avg_evidence_bundle_tokens = 3000  # rough per-strategy evidence bundle size
    reconstruction_output_tokens = 800  # structured JSON spec, roughly
    reconstruction_rate = RATES["claude-sonnet-4-5"]
    reconstruction_cost = est_strategy_count * (
        (avg_evidence_bundle_tokens / 1_000_000 * reconstruction_rate["input"]) +
        (reconstruction_output_tokens / 1_000_000 * reconstruction_rate["output"])
    )

    total = tagging_cost + reconstruction_cost

    return {
        "course_id": course_id,
        "estimated_strategy_count": est_strategy_count,
        "tagging_cost_usd": round(tagging_cost, 3),
        "reconstruction_cost_usd": round(reconstruction_cost, 3),
        "total_estimated_usd": round(total, 2),
        "note": "Rough estimate before tagging runs. The actual tagging stage reports a "
                "real (not estimated) cost once it completes, which is more accurate for "
                "the reconstruction estimate on a second run.",
    }
