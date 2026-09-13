"""
Stage 4: Strategy reconstruction.

For EACH strategy group separately (not the whole course at once), sends
only that group's evidence bundle to a strong model and asks it to produce
a structured strategy spec -- citing the exact source (timestamp or
page/cell) for every rule, and explicitly flagging anything not clearly
supported instead of guessing. This is the one step in the whole pipeline
where "never invent a rule" has to be enforced by the prompt itself, so
the prompt is deliberately strict about citations and about saying
"insufficient evidence" rather than filling gaps.
"""

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

DEFAULT_RECONSTRUCTION_MODEL = "claude-sonnet-4-5"

RECONSTRUCTION_SYSTEM_PROMPT = """You are reconstructing a trading strategy from evidence extracted from a \
trainer's course. You will be given a bundle of evidence items (transcript excerpts with timestamps, \
Excel formulas/values with cell references, PDF text with page numbers, OCR'd screenshot text) that were \
all tagged as relating to the same strategy.

CRITICAL RULES:
1. Every rule, condition, or number in your output MUST cite the exact evidence item(s) it came from \
(timestamp, page number, or cell reference).
2. If the evidence does not clearly support a field (e.g. no explicit stop-loss rule was found), set that \
field to "INSUFFICIENT EVIDENCE" -- do NOT invent a plausible-sounding rule.
3. Do not use outside trading knowledge to fill gaps. Only use what is in the evidence provided.

EVIDENCE LABELS -- each transcript-derived line is prefixed with what kind of moment it was:
  - [TRAINER - EXPLICIT RULE]: the trainer explicitly stating a definitive rule ("so the rule is...").
    Treat these as the most authoritative source for that field.
  - [TRAINER EXPLAINING] / [TRAINER DEMONSTRATION] / [TRAINER - REPEATED EXPLANATION]: the trainer
    teaching, walking through an example, or re-covering something -- solid evidence, but if it
    conflicts with an [EXPLICIT RULE] line elsewhere in the bundle, the explicit rule wins.
  - [EXAMPLE]: a specific worked example, not necessarily the general rule -- use it to illustrate,
    not as the rule itself, unless no general statement exists anywhere in the bundle.
  - [PARTICIPANT QUESTION]: a participant asking something. This is NOT the trainer's rule -- never
    cite a participant question as the source for a strategy field. It exists only as context for the
    [TRAINER ANSWER] that follows it.
  - [TRAINER ANSWER]: the trainer directly responding to a participant. Can legitimately be the source
    for a field (the trainer is still speaking authoritatively), but note in your citation that it came
    from an answer, not the main explanation, if that distinction matters.
Lines with no label are from course materials (PDF/Excel/PPTX/OCR/Word), not spoken -- treat as reference
data, not as the trainer's live explanation.

EXCEL DEPENDENCY -- an Excel workbook rarely explains on its own when or why it should be used; that \
relationship only exists in what the trainer says about it. So "excel_dependency" is NOT a free-text \
summary -- it MUST be a JSON object with exactly these keys:
  - "required": true if the evidence shows this strategy depends on an Excel workbook, false if it doesn't.
  - "workbook": the workbook/file name the trainer refers to, or null if not required.
  - "sheet": the worksheet name, or null if not stated or not required.
  - "required_inputs": list of {"name": ..., "source": ...} -- each input the sheet needs (e.g. high/low, \
timeframe) and where the trainer says it comes from. Empty list if not required.
  - "formula_output": what the workbook's formula/cell actually calculates or outputs, or null.
  - "output_interpretation": how the trainer says to USE that output -- e.g. which output value means \
buy / sell / exit / target / no-trade. This is the field most likely to be missing evidence even when \
the formula itself is clear -- set it to "INSUFFICIENT EVIDENCE" rather than guessing if the trainer \
never explains what the output means for trading.
  - "source_references": list of exact citations (cell refs and/or transcript timestamps) backing the \
fields above. Empty list if not required.
If the evidence shows no Excel dependency at all, respond with {"required": false, "workbook": null, \
"sheet": null, "required_inputs": [], "formula_output": null, "output_interpretation": null, \
"source_references": []} -- do not omit the object or replace it with a plain string.

INDICATOR DEPENDENCY -- trainers often name an indicator (RSI, a custom oscillator, a proprietary tool) \
without ever explaining its settings or how to read it; that gap matters exactly as much as an unexplained \
Excel output. So "indicator_dependency" is NOT free text either -- it MUST be a JSON object with exactly \
these keys:
  - "required": true if the evidence shows this strategy depends on a specific indicator/tool, else false.
  - "name": the indicator or tool's name as the trainer refers to it, or null.
  - "platform_or_source": where it comes from if stated -- e.g. "TradingView built-in", "custom, trainer's \
own", "MetaTrader default", or null if not stated.
  - "parameters": list of {"name": ..., "value": ...} for any settings the trainer gives (period, \
threshold, etc). Empty list if not stated or not required.
  - "calculation_logic": how the indicator is actually calculated, ONLY if the trainer explains it -- \
"INSUFFICIENT EVIDENCE" if it's named but never explained (this is expected and common for standard \
indicators; do not treat a well-known indicator's absence of explanation as unusual).
  - "output_interpretation": how the trainer says to READ the indicator's output for this strategy -- \
which value/crossover/level means buy / sell / exit / no-trade. Set to "INSUFFICIENT EVIDENCE" if the \
indicator is named as required but the evidence never explains how its reading translates into a trading \
decision -- this is the field most likely to be silently missing even when the indicator's name is clear.
  - "source_references": list of exact citations backing the fields above. Empty list if not required.
If the evidence shows no indicator dependency at all, respond with {"required": false, "name": null, \
"platform_or_source": null, "parameters": [], "calculation_logic": null, "output_interpretation": null, \
"source_references": []} -- do not omit the object or replace it with a plain string.

EXTERNAL REFERENCES -- trainers sometimes point to something outside anything provided as course material: \
a website, a third-party tool, another trainer's content, a book, a broker's platform feature, or "look \
this up online". This is fundamentally different from missing evidence -- the trainer DID explain where to \
look, it's just not something this pipeline has access to. Capture every such reference in \
"external_reference_dependency" as a list of objects, each with exactly these keys:
  - "reference": what the trainer pointed to, in their words (site name, tool name, book title, etc).
  - "purpose": what it's needed for in this strategy (e.g. "confirms trend direction before entry").
  - "essential": true if the strategy cannot be correctly followed without consulting it, false if it's \
supplementary/optional context.
  - "source_reference": the citation (timestamp/page/cell) where the trainer mentions it.
Empty list if the evidence contains no such reference. Do not put external references in open_questions \
instead -- they belong here specifically, because unlike a genuine gap, a human reviewer CAN resolve these \
by going to look, and the review process needs to know exactly where to look.

Respond with ONLY a JSON object with these fields: name, purpose, instrument, timeframe, entry_conditions, \
exit_conditions, stop_loss, target, excel_dependency (the structured object described above), \
indicator_dependency (the structured object described above), external_reference_dependency (the list \
described above), no_trade_conditions, \
examples, open_questions (list of anything flagged as unclear/insufficient), source_citations \
(list of {evidence_summary, source_reference} pairs). Nothing else -- no preamble, no markdown fences."""

# Human-readable prefixes for each content_type -- off_topic_discussion is
# deliberately absent because it's excluded from the bundle entirely (see
# _build_evidence_bundle), not just relabeled.
CONTENT_TYPE_LABELS = {
    "explicit_final_rule": "[TRAINER - EXPLICIT RULE] ",
    "trainer_explanation": "[TRAINER EXPLAINING] ",
    "trainer_demonstration": "[TRAINER DEMONSTRATION] ",
    "repeated_explanation": "[TRAINER - REPEATED EXPLANATION] ",
    "example_illustration": "[EXAMPLE] ",
    "participant_question": "[PARTICIPANT QUESTION] ",
    "trainer_answer": "[TRAINER ANSWER] ",
}


def _build_evidence_bundle(conn, strategy_id: str) -> str:
    rows = conn.execute(
        """SELECT e.source_kind, e.source_ref_id, e.content_type FROM evidence e
           WHERE e.related_strategy_id = ?""",
        (strategy_id,),
    ).fetchall()

    bundle_lines = []
    excluded_off_topic = 0
    for source_kind, ref_id, content_type in rows:
        if content_type == "off_topic_discussion":
            # Small talk / unrelated chat adds no strategy value and only
            # risks the model latching onto an irrelevant sentence -- drop
            # it from the bundle entirely rather than just labeling it.
            excluded_off_topic += 1
            continue
        label = CONTENT_TYPE_LABELS.get(content_type, "")
        if source_kind == "transcript_segment":
            row = conn.execute(
                "SELECT start_seconds, end_seconds, text FROM transcript_segments WHERE segment_id = ?",
                (ref_id,),
            ).fetchone()
            if row:
                start, end, text = row
                bundle_lines.append(f"{label}[Transcript {start:.0f}s-{end:.0f}s] {text}")
        else:
            row = conn.execute(
                "SELECT location_ref, content FROM document_extractions WHERE extraction_id = ?",
                (ref_id,),
            ).fetchone()
            if row:
                loc, content = row
                bundle_lines.append(f"[{loc}] {content}")

    if excluded_off_topic:
        print(f"    -- excluded {excluded_off_topic} off-topic transcript segment(s) from the bundle")
    return "\n".join(bundle_lines)


def _archive_and_write_spec(conn, strategy_id: str, new_spec: dict, change_reason: str) -> int:
    """
    Upgrade 7: never silently overwrite a strategy's spec. Every version,
    including the very first one, gets its own row in strategy_versions at
    the moment it's created -- so by the time a later call replaces
    strategies.spec_json, the version being replaced was already recorded
    and nothing needs a separate "archive" copy. Returns the new version
    number (1 for the first real spec a strategy ever gets).
    """
    row = conn.execute(
        "SELECT spec_json, version FROM strategies WHERE strategy_id = ?", (strategy_id,)
    ).fetchone()
    current_spec_json, current_version = (row[0], row[1]) if row else (None, 0)
    # strategies.version defaults to 1 even before any spec exists (see
    # schema.py) -- that default describes "no spec yet", not "version 1",
    # so the first real spec is always version 1, not current_version + 1.
    new_version = (current_version + 1) if current_spec_json else 1

    conn.execute(
        """UPDATE strategies SET spec_json = ?, maturity = 'S2_RECONSTRUCTED', version = ?, updated_at = ?
           WHERE strategy_id = ?""",
        (json.dumps(new_spec), new_version, datetime.now(timezone.utc).isoformat(), strategy_id),
    )
    conn.execute(
        """INSERT INTO strategy_versions (version_id, strategy_id, version, spec_json, change_reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), strategy_id, new_version, json.dumps(new_spec), change_reason,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return new_version


def get_strategy_version_history(conn, strategy_id: str) -> list:
    """Returns every recorded version of a strategy, oldest first, for display/audit purposes."""
    rows = conn.execute(
        """SELECT version, spec_json, change_reason, created_at FROM strategy_versions
           WHERE strategy_id = ? ORDER BY version ASC""",
        (strategy_id,),
    ).fetchall()
    return [
        {"version": v, "spec": json.loads(s), "change_reason": reason, "created_at": ts}
        for v, s, reason, ts in rows
    ]


def _reconstruct_one_strategy(conn, client, model: str, strategy_id: str, name: str,
                               output_dir: str, change_reason: str) -> dict:
    """Shared by both the normal pipeline pass and explicit single-strategy re-runs."""
    bundle = _build_evidence_bundle(conn, strategy_id)
    if not bundle.strip():
        return {"skipped": True, "reason": "no evidence text found"}

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=RECONSTRUCTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Strategy topic tag: {name}\n\nEvidence:\n{bundle}"}],
    )
    response_text = response.content[0].text

    try:
        spec = json.loads(response_text)
    except json.JSONDecodeError:
        return {"skipped": True, "reason": f"could not parse response as JSON: {response_text[:200]}"}

    new_version = _archive_and_write_spec(conn, strategy_id, spec, change_reason)

    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)[:60]
    out_file = Path(output_dir) / f"{safe_name}_v{new_version}.json"
    out_file.write_text(json.dumps(spec, indent=2))

    return {"skipped": False, "version": new_version, "input_chars": len(bundle), "output_chars": len(response_text)}


def rerun_strategy_reconstruction(conn, strategy_id: str, output_dir: str, api_key: str,
                                   change_reason: str, model: str = DEFAULT_RECONSTRUCTION_MODEL) -> dict:
    """
    Explicit re-run for ONE strategy, regardless of its current maturity --
    e.g. after new evidence gets linked to it, or after a conflict flagged
    by quality_check.py gets manually resolved and you want the spec
    regenerated. The previous spec is archived, not lost (see
    _archive_and_write_spec). change_reason is required and gets stored
    with the new version, since "why did this change" is exactly what a
    version history is for -- pass something like "added Session 4
    evidence" or "resolved stop-loss conflict between sessions 2 and 5".
    """
    if not HAS_ANTHROPIC:
        return {"error": "anthropic package not installed -- pip install anthropic"}
    row = conn.execute("SELECT name FROM strategies WHERE strategy_id = ?", (strategy_id,)).fetchone()
    if not row:
        return {"error": f"no strategy found with id {strategy_id}"}
    name = row[0]
    client = anthropic.Anthropic(api_key=api_key)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    result = _reconstruct_one_strategy(conn, client, model, strategy_id, name, output_dir, change_reason)
    print(f"  [reconstruct] re-ran '{name}': {result}")
    return result


def reconstruct_strategies_for_course(conn, course_id: str, output_dir: str, api_key: str,
                                       model: str = DEFAULT_RECONSTRUCTION_MODEL) -> dict:
    if not HAS_ANTHROPIC:
        return {"error": "anthropic package not installed -- pip install anthropic", "reconstructed": 0}

    strategies = conn.execute(
        "SELECT strategy_id, name FROM strategies WHERE course_id = ? AND maturity = 'S1_LINKED'",
        (course_id,),
    ).fetchall()

    if not strategies:
        print("  [reconstruct] no linked strategies pending -- run cross-linking first")
        return {"reconstructed": 0}

    client = anthropic.Anthropic(api_key=api_key)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    reconstructed = 0
    total_input_chars = 0
    total_output_chars = 0

    for i, (strategy_id, name) in enumerate(strategies, start=1):
        print(f"  [reconstruct] {i}/{len(strategies)}: {name}")
        result = _reconstruct_one_strategy(
            conn, client, model, strategy_id, name, output_dir,
            change_reason="initial reconstruction",
        )
        if result.get("skipped"):
            print(f"    -- skipped: {result.get('reason')}")
            continue
        total_input_chars += result["input_chars"]
        total_output_chars += result["output_chars"]
        reconstructed += 1
        time.sleep(0.5)

    input_tokens_est = total_input_chars / 4
    output_tokens_est = total_output_chars / 4
    # Sonnet pricing ballpark (verify current rate at platform.claude.com/pricing before relying on this)
    cost_estimate = (input_tokens_est / 1_000_000 * 3.0) + (output_tokens_est / 1_000_000 * 15.0)

    print(f"  [reconstruct] done -- {reconstructed} strategies reconstructed")
    return {"reconstructed": reconstructed, "cost_estimate_usd": round(cost_estimate, 4)}
