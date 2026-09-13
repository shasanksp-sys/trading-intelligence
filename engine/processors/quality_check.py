"""
Stage 5: Quality check.

The insufficient-evidence / open-questions checks are purely deterministic
-- no AI call, just reading what stages 1-4 already produced. Confidence
categorization is also deterministic, rule-based on evidence composition.

Conflict detection (Upgrade 5/8) is the one AI-assisted part of this
stage: telling whether two sessions' evidence genuinely CONTRADICTS each
other (different stop-loss levels, different entry rules) versus is just
complementary detail needs semantic judgment a keyword/string check can't
give reliably. It only runs when a strategy has evidence from 2+ distinct
sessions (no cross-session conflict is possible otherwise) and only when
an api_key is supplied -- callers that don't pass one still get the full
deterministic check, just without conflict detection.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

DEFAULT_CONFLICT_MODEL = "claude-haiku-4-5-20251001"

CONFLICT_SYSTEM_PROMPT = """You are checking evidence from a trading course for genuine contradictions \
between sessions. You'll be given evidence excerpts about ONE strategy, grouped by which session they \
came from. Sessions often build on each other (a later session adding detail is NOT a conflict) -- only \
flag it if two sessions state something that cannot both be true at once (e.g. one session says the stop \
goes below the swing low, another says it's a fixed 20-point stop, with no indication the trainer changed \
their approach on purpose). Explicit revision language ("earlier I used to say X, but now I use Y", "I've \
updated this to...", "forget what I said before, now do...") is the trainer intentionally superseding \
their own earlier statement, NOT a conflict -- treat the newer statement as current and do not flag it.

Respond with ONLY a JSON object: {"conflict": true/false, "field": "which aspect conflicts (e.g. \
'stop_loss') or null", "session_a_statement": "the first conflicting statement, or null", \
"session_b_statement": "the second conflicting statement, or null", "explanation": "one sentence, or null"}. \
Nothing else -- no preamble, no markdown fences."""

# Trading-strategy fields where a wrong NUMBER (not a wrong word) is the
# highest-stakes, hardest-to-catch error in this whole pipeline: ASR
# transcription can mishear "20 points" as "20 percent" or drop a digit,
# and nothing downstream can detect that automatically -- the number is
# internally consistent, just wrong. These fields get a mandatory
# human-spot-check flag whenever they contain a digit, regardless of how
# high the confidence category otherwise is.
NUMERIC_CHECK_FIELDS = ("entry_conditions", "exit_conditions", "stop_loss", "target")


def _contains_digit(value) -> bool:
    if not isinstance(value, str):
        return False
    return any(ch.isdigit() for ch in value)


def _is_insufficient_evidence(value) -> bool:
    """
    Was an exact-string match against the literal marker -- found via real
    testing that this silently misses any field where the marker is
    followed by explanation text (e.g. "INSUFFICIENT EVIDENCE -- the
    trainer never states an exact threshold"), which is a plausible way
    for a model to comply with the reconstruction prompt's instruction
    without reproducing the bare literal. A field like that has exactly
    the same "don't trust this without a human look" status as a bare
    marker, so it must be caught the same way -- silently dropping it
    from insufficient_fields is exactly the kind of gap this whole stage
    exists to catch.
    """
    return isinstance(value, str) and value.startswith("INSUFFICIENT EVIDENCE")


def _compute_confidence_category(conn, strategy_id: str, insufficient_fields: list, has_conflict: bool) -> str:
    """
    Upgrade 5: HIGH / MEDIUM / LOW / CONFLICT instead of a bare numeric
    score. Deterministic and evidence-composition-based:
      - CONFLICT overrides everything else -- a contradiction matters more
        than how much evidence exists.
      - HIGH: no missing fields, at least one explicit/repeated trainer
        rule statement backing it, no low-ASR-confidence evidence involved.
      - MEDIUM: no missing fields, but the strongest evidence is only an
        explanation/example/answer rather than an explicit stated rule, or
        some low-confidence transcript is involved.
      - LOW: missing fields, or nothing but weak evidence (a single
        low-confidence segment, or evidence only from a participant Q&A
        exchange with no explicit trainer rule anywhere).
    """
    if has_conflict:
        return "CONFLICT"

    evidence_rows = conn.execute(
        """SELECT e.source_kind, e.source_ref_id, e.content_type FROM evidence e
           WHERE e.related_strategy_id = ?""",
        (strategy_id,),
    ).fetchall()

    strong_types = {"explicit_final_rule", "repeated_explanation"}
    has_strong_evidence = any(ct in strong_types for _, _, ct in evidence_rows)

    low_conf_count = 0
    for source_kind, ref_id, _ in evidence_rows:
        if source_kind == "transcript_segment":
            row = conn.execute(
                "SELECT needs_review FROM transcript_segments WHERE segment_id = ?", (ref_id,)
            ).fetchone()
            if row and row[0]:
                low_conf_count += 1

    if insufficient_fields:
        return "LOW"
    if not evidence_rows:
        return "LOW"
    if has_strong_evidence and low_conf_count == 0:
        return "HIGH"
    if low_conf_count >= max(1, len(evidence_rows) // 2):
        return "LOW"  # at least half the backing evidence has shaky transcription
    return "MEDIUM"


def _detect_conflict_for_strategy(client, model: str, name: str, evidence_by_session: dict) -> dict:
    if len(evidence_by_session) < 2:
        return {"conflict": False}

    bundle_lines = []
    for session_id, texts in evidence_by_session.items():
        bundle_lines.append(f"--- Session {session_id} ---")
        bundle_lines.extend(f"  {t}" for t in texts)
    bundle = "\n".join(bundle_lines)

    response = client.messages.create(
        model=model,
        max_tokens=400,
        system=CONFLICT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Strategy: {name}\n\n{bundle}"}],
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return {"conflict": False, "error": "could not parse conflict-check response"}


def run_quality_check(conn, course_id: str, output_dir: str, api_key: str = None,
                       model: str = DEFAULT_CONFLICT_MODEL) -> dict:
    strategies = conn.execute(
        "SELECT strategy_id, name, spec_json FROM strategies WHERE course_id = ? AND maturity = 'S2_RECONSTRUCTED'",
        (course_id,),
    ).fetchall()

    conflict_client = anthropic.Anthropic(api_key=api_key) if (api_key and HAS_ANTHROPIC) else None
    if api_key and not HAS_ANTHROPIC:
        print("  [quality] anthropic package not installed -- skipping conflict detection, "
              "everything else still runs")

    flagged = []
    clean = []

    for strategy_id, name, spec_json in strategies:
        spec = json.loads(spec_json) if spec_json else {}
        open_questions = spec.get("open_questions", [])
        insufficient_fields = [k for k, v in spec.items() if _is_insufficient_evidence(v)]

        # excel_dependency is a structured object (see reconstruction_processor.py),
        # not a plain field, so its own "INSUFFICIENT EVIDENCE" values (most often
        # output_interpretation -- the formula is clear but what it MEANS for a
        # trade isn't) would never be caught by the flat check above. A strategy
        # that depends on Excel but doesn't explain what the output means is not
        # a usable strategy, so this must surface the same way any other gap does.
        excel_dep = spec.get("excel_dependency")
        if isinstance(excel_dep, dict) and excel_dep.get("required"):
            for field_name, value in excel_dep.items():
                if _is_insufficient_evidence(value):
                    insufficient_fields.append(f"excel_dependency.{field_name}")

        # Same principle as excel_dependency: a named indicator whose
        # output was never explained is not a usable rule, so its gaps
        # must surface exactly like a missing top-level field would.
        indicator_dep = spec.get("indicator_dependency")
        if isinstance(indicator_dep, dict) and indicator_dep.get("required"):
            for field_name, value in indicator_dep.items():
                if _is_insufficient_evidence(value):
                    insufficient_fields.append(f"indicator_dependency.{field_name}")

        # External references (a site/tool/book the trainer pointed to)
        # are NOT a gap in the evidence -- the trainer did explain where
        # to look. But an ESSENTIAL one means the strategy genuinely
        # cannot be followed correctly without a human going to check it,
        # so it still needs to surface for review, just as its own thing
        # rather than being lumped in with insufficient_fields.
        external_refs = spec.get("external_reference_dependency") or []
        essential_external_refs = [
            r for r in external_refs if isinstance(r, dict) and r.get("essential")
        ]

        # Numeric spot-check flag: a wrong number in a stop-loss/target/
        # entry/exit field is the single highest-stakes error type this
        # pipeline cannot self-detect (ASR can mishear "20 points" as
        # "20 percent" and nothing downstream would notice, since the
        # result is still internally consistent). Flagged independently
        # of confidence category -- a HIGH-confidence strategy can still
        # carry a mistranscribed number.
        numeric_fields_present = [
            f for f in NUMERIC_CHECK_FIELDS if _contains_digit(spec.get(f))
        ]

        # also check for any low-confidence transcript evidence backing this strategy
        low_conf_evidence = conn.execute(
            """SELECT COUNT(*) FROM evidence e
               JOIN transcript_segments ts ON ts.segment_id = e.source_ref_id
               WHERE e.related_strategy_id = ? AND e.source_kind = 'transcript_segment'
                 AND ts.needs_review = 1""",
            (strategy_id,),
        ).fetchone()[0]

        # ---- conflict detection (Upgrade 8) ----
        conflict_info = {"conflict": False}
        evidence_by_session = {}
        session_rows = conn.execute(
            """SELECT ts.session_id, ts.text FROM evidence e
               JOIN transcript_segments ts ON ts.segment_id = e.source_ref_id
               WHERE e.related_strategy_id = ? AND e.source_kind = 'transcript_segment'
                 AND (e.content_type IS NULL OR e.content_type != 'off_topic_discussion')""",
            (strategy_id,),
        ).fetchall()
        for session_id, text in session_rows:
            evidence_by_session.setdefault(session_id, []).append(text)

        if conflict_client and len(evidence_by_session) >= 2:
            conflict_info = _detect_conflict_for_strategy(conflict_client, model, name, evidence_by_session)
            if conflict_info.get("conflict"):
                conflicting_ids = [eid for (eid,) in conn.execute(
                    """SELECT e.evidence_id FROM evidence e JOIN transcript_segments ts
                       ON ts.segment_id = e.source_ref_id
                       WHERE e.related_strategy_id = ? AND e.source_kind = 'transcript_segment'""",
                    (strategy_id,),
                ).fetchall()]
                group_id = f"conflict_{strategy_id[:8]}"
                for eid in conflicting_ids:
                    conn.execute("UPDATE evidence SET conflict_group_id = ? WHERE evidence_id = ?", (group_id, eid))
            time.sleep(0.3)

        confidence_category = _compute_confidence_category(
            conn, strategy_id, insufficient_fields, has_conflict=bool(conflict_info.get("conflict"))
        )

        needs_review = (
            bool(open_questions) or bool(insufficient_fields) or low_conf_evidence > 0
            or confidence_category in ("LOW", "CONFLICT") or bool(essential_external_refs)
        )

        item = {
            "strategy_id": strategy_id,
            "name": name,
            "confidence": confidence_category,
            "open_questions": open_questions,
            "insufficient_fields": insufficient_fields,
            "low_confidence_evidence_count": low_conf_evidence,
            "external_references": external_refs,
            "numeric_fields_recommend_audio_spot_check": numeric_fields_present,
        }
        if conflict_info.get("conflict"):
            item["conflict_detail"] = {
                "field": conflict_info.get("field"),
                "session_a_statement": conflict_info.get("session_a_statement"),
                "session_b_statement": conflict_info.get("session_b_statement"),
                "explanation": conflict_info.get("explanation"),
            }

        new_maturity = "S3_NEEDS_REVIEW" if needs_review else "S3_CLEAN"
        conn.execute(
            "UPDATE strategies SET maturity = ?, confidence_category = ? WHERE strategy_id = ?",
            (new_maturity, confidence_category, strategy_id),
        )

        if needs_review:
            flagged.append(item)
        else:
            clean.append(item)

    conn.commit()

    report = {
        "course_id": course_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_strategies": len(strategies),
        "clean_count": len(clean),
        "flagged_count": len(flagged),
        "conflict_detection_ran": conflict_client is not None,
        "flagged": flagged,
        "clean": [{"name": c["name"], "confidence": c["confidence"],
                   "numeric_fields_recommend_audio_spot_check": c["numeric_fields_recommend_audio_spot_check"]}
                  for c in clean],
    }

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    report_path = Path(output_dir) / "review_report.json"
    report_path.write_text(json.dumps(report, indent=2))

    conflict_count = sum(1 for f in flagged if f.get("confidence") == "CONFLICT")
    print(f"  [quality] {len(clean)} clean, {len(flagged)} flagged for review "
          f"({conflict_count} with detected conflicts) -> {report_path}")
    return report
