"""
Stage 3: Cross-linking.

Groups tagged evidence into candidate strategies. Mostly deterministic --
the only "fuzzy" part is merging near-duplicate tags (e.g. "POB entry" vs
"POB Entry Rule" vs a mis-transcribed variant) using simple text
similarity, NOT a full AI call -- this keeps the stage almost free.
"""

import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.6  # tags scoring above this are merged into one group


def _tag_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _cluster_tags(tags: list) -> dict:
    """
    Groups similar tag strings together. Returns {canonical_tag: [original_tags]}.
    Deterministic -- same input always produces the same grouping, no AI call.
    """
    unique_tags = sorted(set(t for t in tags if t and t.lower() != "unclear/off-topic"))
    clusters = {}  # canonical -> [members]

    for tag in unique_tags:
        placed = False
        for canonical in list(clusters.keys()):
            if _tag_similarity(tag, canonical) >= SIMILARITY_THRESHOLD:
                clusters[canonical].append(tag)
                placed = True
                break
        if not placed:
            clusters[tag] = [tag]

    return clusters


def cross_link_course(conn, course_id: str) -> dict:
    """
    Reads all tagged evidence for a course, clusters similar tags into
    strategy groups, creates/updates rows in the strategies table, and
    links each evidence row to its strategy via related_strategy_id.
    """
    rows = conn.execute(
        "SELECT evidence_id, topic_tags FROM evidence WHERE course_id = ? AND topic_tags IS NOT NULL",
        (course_id,),
    ).fetchall()

    if not rows:
        print("  [linking] no tagged evidence found -- run tagging first")
        return {"strategies_created": 0, "evidence_linked": 0}

    all_tags = [tag for _, tag in rows]
    clusters = _cluster_tags(all_tags)
    print(f"  [linking] {len(set(all_tags))} distinct tags clustered into {len(clusters)} candidate strategies")

    tag_to_canonical = {}
    for canonical, members in clusters.items():
        for m in members:
            tag_to_canonical[m] = canonical

    canonical_to_strategy_id = {}
    strategies_created = 0
    for canonical in clusters:
        existing = conn.execute(
            "SELECT strategy_id FROM strategies WHERE course_id = ? AND name = ?",
            (course_id, canonical),
        ).fetchone()
        if existing:
            canonical_to_strategy_id[canonical] = existing[0]
        else:
            sid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO strategies (strategy_id, course_id, name, maturity, approved, created_at)
                   VALUES (?, ?, ?, 'S1_LINKED', 0, ?)""",
                (sid, course_id, canonical, datetime.now(timezone.utc).isoformat()),
            )
            canonical_to_strategy_id[canonical] = sid
            strategies_created += 1

    evidence_linked = 0
    for evidence_id, tag in rows:
        canonical = tag_to_canonical.get(tag)
        if canonical:
            strategy_id = canonical_to_strategy_id[canonical]
            conn.execute(
                "UPDATE evidence SET related_strategy_id = ? WHERE evidence_id = ?",
                (strategy_id, evidence_id),
            )
            evidence_linked += 1

    conn.commit()
    print(f"  [linking] {strategies_created} new strategy group(s), {evidence_linked} evidence row(s) linked")
    return {"strategies_created": strategies_created, "evidence_linked": evidence_linked,
            "total_strategy_groups": len(clusters)}
