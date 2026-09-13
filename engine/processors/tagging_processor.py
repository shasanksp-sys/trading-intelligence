"""
Stage 2: Topic tagging.

Reads transcript segments and document extractions for a course that
haven't been tagged yet, and asks a cheap/fast model to assign each one a
candidate topic label (e.g. "POB entry rule", "TTS calculation", "risk
management"). This is classification, not writing -- doesn't need a top-
tier model, which is why this stage is deliberately cheap.

Every evidence row keeps its exact source (segment timestamp, or document
page/cell) -- tagging only ADDS a topic_tags column, it never rewrites or
summarizes the underlying evidence.
"""

import json
import time
import uuid
from datetime import datetime, timezone

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

DEFAULT_TAGGING_MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 20  # evidence items per API call -- keeps each call small and cheap

# Upgrade 4: what kind of spoken moment a transcript segment actually is.
# Nothing downstream (tagging, reconstruction) previously distinguished the
# trainer stating a rule from a participant asking about it or a tangent --
# all three fed into strategy reconstruction as equally valid evidence.
CONTENT_TYPES = [
    "trainer_explanation",     # trainer actively teaching/describing a rule or concept
    "trainer_demonstration",   # trainer walking through a live/example chart or the Excel sheet
    "explicit_final_rule",     # trainer explicitly stating "the rule is..." / "so to summarize..."
    "repeated_explanation",    # trainer re-explaining something already covered (later, often clearer)
    "participant_question",    # someone other than the trainer asking something
    "trainer_answer",          # trainer responding to a participant's question
    "example_illustration",    # a worked example, not the general rule itself
    "off_topic_discussion",    # small talk, technical issues, unrelated chat
]
MATERIAL_CONTENT_TYPE = "material_reference"  # fixed value for document-derived evidence -- not spoken, no AI call needed

TAGGING_SYSTEM_PROMPT = """You are tagging evidence from a trading course for later grouping into strategies.
For each numbered item below, output an object with two fields:
  - "topic": a short label (2-5 words) identifying which trading strategy, concept, or indicator it
    relates to (e.g. "POB entry rule", "TTS calculation", "risk management", "Fortune Signals setup").
    If off-topic or unclear, use "unclear/off-topic".
  - "content_type": exactly one of """ + json.dumps(CONTENT_TYPES) + """ for items marked [SPOKEN],
    classifying what KIND of moment this is -- is the trainer actually teaching a rule, giving an
    example, answering a question, or is this a participant asking something or off-topic chat?
    Distinguishing "explicit_final_rule" (trainer explicitly states the definitive rule, e.g. "so the
    rule is...") from "trainer_explanation" (still explaining/thinking out loud) matters -- prefer
    explicit_final_rule whenever the trainer is clearly stating a conclusion, not just discussing it.
    For items marked [MATERIAL] (not spoken), always use "material_reference" -- don't guess.

Respond with ONLY a JSON array of objects (one per item, in order, same length as the input), like:
[{"topic": "...", "content_type": "..."}, ...]. Nothing else -- no preamble, no markdown fences."""


def _backfill_evidence_rows(conn, course_id: str) -> int:
    """
    The 'evidence' table is the unified layer tagging/linking operate on.
    This populates it from transcript_segments and document_extractions
    for the given course, skipping anything already backfilled. Returns
    the count of newly created evidence rows.
    """
    created = 0

    rows = conn.execute(
        """SELECT ts.segment_id, ts.text, ts.confidence FROM transcript_segments ts
           JOIN sessions s ON s.session_id = ts.session_id
           WHERE s.course_id = ?
             AND ts.segment_id NOT IN (
                 SELECT source_ref_id FROM evidence WHERE source_kind = 'transcript_segment'
             )""",
        (course_id,),
    ).fetchall()
    for segment_id, text, confidence in rows:
        conn.execute(
            """INSERT INTO evidence (evidence_id, course_id, source_kind, source_ref_id,
               confidence, review_status, created_at)
               VALUES (?, ?, 'transcript_segment', ?, ?, 'UNREVIEWED', ?)""",
            (str(uuid.uuid4()), course_id, segment_id, confidence, datetime.now(timezone.utc).isoformat()),
        )
        created += 1

    rows = conn.execute(
        """SELECT de.extraction_id, de.content, de.confidence FROM document_extractions de
           JOIN files f ON f.file_id = de.file_id
           WHERE f.course_id = ?
             AND de.extraction_id NOT IN (
                 SELECT source_ref_id FROM evidence WHERE source_kind = 'document_extraction'
             )""",
        (course_id,),
    ).fetchall()
    for extraction_id, content, confidence in rows:
        conn.execute(
            """INSERT INTO evidence (evidence_id, course_id, source_kind, source_ref_id,
               confidence, review_status, created_at, content_type)
               VALUES (?, ?, 'document_extraction', ?, ?, 'UNREVIEWED', ?, ?)""",
            (str(uuid.uuid4()), course_id, extraction_id, confidence, datetime.now(timezone.utc).isoformat(),
             MATERIAL_CONTENT_TYPE),
        )
        created += 1

    conn.commit()
    return created


def _get_evidence_text(conn, source_kind: str, source_ref_id: str) -> str:
    if source_kind == "transcript_segment":
        row = conn.execute("SELECT text FROM transcript_segments WHERE segment_id = ?", (source_ref_id,)).fetchone()
    else:
        row = conn.execute("SELECT content FROM document_extractions WHERE extraction_id = ?", (source_ref_id,)).fetchone()
    return row[0] if row else ""


def tag_evidence_for_course(conn, course_id: str, api_key: str, model: str = DEFAULT_TAGGING_MODEL) -> dict:
    """
    Tags every untagged evidence row for a course. Returns a summary dict.
    Requires an Anthropic API key -- this is the first AI-dependent stage.
    """
    if not HAS_ANTHROPIC:
        return {"error": "anthropic package not installed -- pip install anthropic", "tagged": 0}

    backfilled = _backfill_evidence_rows(conn, course_id)
    print(f"  [tagging] {backfilled} new evidence row(s) registered from extraction results")

    untagged = conn.execute(
        "SELECT evidence_id, source_kind, source_ref_id FROM evidence "
        "WHERE course_id = ? AND (topic_tags IS NULL OR topic_tags = '')",
        (course_id,),
    ).fetchall()

    if not untagged:
        print("  [tagging] nothing untagged -- skipping")
        return {"tagged": 0, "batches": 0, "cost_estimate_usd": 0.0}

    client = anthropic.Anthropic(api_key=api_key)
    tagged_count = 0
    total_input_chars = 0
    total_output_chars = 0

    for i in range(0, len(untagged), BATCH_SIZE):
        batch = untagged[i:i + BATCH_SIZE]
        items_text = "\n".join(
            f"{j+1}. [{'SPOKEN' if kind == 'transcript_segment' else 'MATERIAL'}] "
            f"{_get_evidence_text(conn, kind, ref)[:300]}"
            for j, (eid, kind, ref) in enumerate(batch)
        )
        total_input_chars += len(items_text)

        print(f"  [tagging] batch {i // BATCH_SIZE + 1}/{(len(untagged) - 1) // BATCH_SIZE + 1} "
              f"({len(batch)} items)")

        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=TAGGING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": items_text}],
        )
        response_text = response.content[0].text
        total_output_chars += len(response_text)

        try:
            results = json.loads(response_text)
        except json.JSONDecodeError:
            # model didn't return clean JSON -- log and skip this batch rather than crash
            print(f"  [tagging] WARNING: could not parse response for this batch, skipping: {response_text[:200]}")
            continue

        for (eid, kind, ref), result in zip(batch, results):
            # be defensive about shape -- an older/odd model response might
            # still be a plain string; don't let that crash the whole batch
            if isinstance(result, dict):
                topic = result.get("topic", "unclear/off-topic")
                content_type = result.get("content_type")
            else:
                topic, content_type = str(result), None
            if kind != "transcript_segment":
                content_type = MATERIAL_CONTENT_TYPE  # never trust the model's guess for material rows
            elif content_type not in CONTENT_TYPES:
                content_type = None  # unrecognized value -- leave unclassified rather than store garbage
            conn.execute(
                "UPDATE evidence SET topic_tags = ?, content_type = ? WHERE evidence_id = ?",
                (topic, content_type, eid),
            )
            tagged_count += 1
        conn.commit()
        time.sleep(0.5)  # gentle pacing between calls

    # rough cost estimate -- ~4 chars/token is a standard approximation
    input_tokens_est = total_input_chars / 4
    output_tokens_est = total_output_chars / 4
    # Haiku pricing ballpark (verify current rate at platform.claude.com/pricing before relying on this)
    cost_estimate = (input_tokens_est / 1_000_000 * 1.0) + (output_tokens_est / 1_000_000 * 5.0)

    print(f"  [tagging] done -- {tagged_count} evidence rows tagged")
    return {"tagged": tagged_count, "batches": (len(untagged) - 1) // BATCH_SIZE + 1,
            "cost_estimate_usd": round(cost_estimate, 4)}
