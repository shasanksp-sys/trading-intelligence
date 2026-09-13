"""
Storage policy automation.

Per your decisions:
  - Local raw video: auto-deleted once ITS OWN extraction is fully confirmed
    successful (stage_completed = 'frames_done') -- not before, so a
    partial/failed extraction never loses its source material.
  - YouTube video: deleted by default; kept + moved to a chosen folder only
    if "keep" was selected when the URL was queued.
  - Key frames never cited by an APPROVED strategy: pruned, but only after
    human review/approval (stage 6) -- never before, so nothing needed
    during review gets deleted prematurely.
"""

import shutil
from pathlib import Path


def delete_video_if_processed(conn, file_id: str, dry_run: bool = False) -> dict:
    """
    Deletes the raw video file for file_id IF its extraction fully
    completed (stage_completed = 'frames_done'). Returns what happened.
    """
    row = conn.execute(
        "SELECT original_path, stage_completed, source_type FROM files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if not row:
        return {"deleted": False, "reason": "file not found in database"}

    original_path, stage_completed, source_type = row

    if stage_completed != "frames_done":
        return {"deleted": False, "reason": f"extraction not fully complete (stage={stage_completed})"}

    path = Path(original_path)
    if not path.exists():
        return {"deleted": False, "reason": "file already absent on disk"}

    if dry_run:
        return {"deleted": False, "reason": "dry_run=True, would have deleted", "path": str(path)}

    path.unlink()
    return {"deleted": True, "path": str(path)}


def delete_intermediate_audio_if_processed(conn, file_id: str, work_dir: str, dry_run: bool = False) -> dict:
    """
    Deletes the extracted intermediate .wav audio file (in
    <work_dir>/audio/<file_id>.wav) once its video's extraction fully
    completed. This was a real storage gap: the raw source video was
    already being auto-deleted, but the extracted audio copy -- which can
    be 150-220MB for a long session -- was never cleaned up at all, since
    it serves no further purpose once transcription has succeeded (the
    transcript itself, not the raw audio, is what everything downstream
    actually uses).
    """
    row = conn.execute(
        "SELECT stage_completed FROM files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if not row:
        return {"deleted": False, "reason": "file not found in database"}

    stage_completed = row[0]
    if stage_completed != "frames_done":
        return {"deleted": False, "reason": f"extraction not fully complete (stage={stage_completed})"}

    audio_path = Path(work_dir) / "audio" / f"{file_id}.wav"
    if not audio_path.exists():
        return {"deleted": False, "reason": "audio file already absent"}

    if dry_run:
        return {"deleted": False, "reason": "dry_run=True, would have deleted", "path": str(audio_path)}

    audio_path.unlink()

    # the audio/ folder itself is now empty leftover clutter -- remove it
    # too. A stray .gitkeep placeholder (invisible in Finder, but a real
    # file) can be the only thing left behind and would otherwise block
    # this -- clear that first, then rmdir only succeeds if genuinely
    # empty, so this can never delete real content by accident.
    try:
        gitkeep = audio_path.parent / ".gitkeep"
        gitkeep.unlink(missing_ok=True)
        audio_path.parent.rmdir()
    except OSError:
        pass  # not empty (e.g. other files still processing) -- leave it alone

    return {"deleted": True, "path": str(audio_path)}


def cleanup_course_videos(conn, course_id: str, work_dir: str = None, dry_run: bool = False) -> dict:
    """Runs delete_video_if_processed for every LOCAL video in a course,
    plus delete_intermediate_audio_if_processed if work_dir is given."""
    rows = conn.execute(
        "SELECT file_id FROM files WHERE course_id = ? AND file_type = 'video' AND source_type = 'local'",
        (course_id,),
    ).fetchall()
    results = [delete_video_if_processed(conn, fid, dry_run=dry_run) for (fid,) in rows]
    deleted = sum(1 for r in results if r["deleted"])
    print(f"  [cleanup] {deleted}/{len(results)} local video(s) deleted for {course_id}")

    audio_deleted = 0
    if work_dir:
        audio_results = [delete_intermediate_audio_if_processed(conn, fid, work_dir, dry_run=dry_run) for (fid,) in rows]
        audio_deleted = sum(1 for r in audio_results if r["deleted"])
        print(f"  [cleanup] {audio_deleted}/{len(results)} intermediate audio file(s) deleted for {course_id}")

    return {"checked": len(results), "deleted": deleted, "audio_deleted": audio_deleted}


def handle_youtube_video_retention(conn, file_id: str, keep: bool, keep_folder: str = None) -> dict:
    """
    Called once a YouTube video's extraction is fully complete.
    If keep=False: delete the downloaded video (default).
    If keep=True: move it to keep_folder instead of deleting.
    """
    row = conn.execute(
        "SELECT original_path, stage_completed FROM files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if not row:
        return {"action": "none", "reason": "file not found"}

    original_path, stage_completed = row
    if stage_completed != "frames_done":
        return {"action": "none", "reason": f"extraction not complete (stage={stage_completed})"}

    path = Path(original_path)
    if not path.exists():
        return {"action": "none", "reason": "file already absent"}

    if keep and keep_folder:
        Path(keep_folder).mkdir(parents=True, exist_ok=True)
        dest = Path(keep_folder) / path.name
        shutil.move(str(path), str(dest))
        _cleanup_empty_parent(path.parent)
        return {"action": "kept", "moved_to": str(dest)}
    else:
        path.unlink()
        _cleanup_empty_parent(path.parent)
        return {"action": "deleted", "path": str(path)}


def _cleanup_empty_parent(folder: Path) -> None:
    """Removes a folder (like _downloads/) once it's genuinely empty --
    clears a stray .gitkeep placeholder first (invisible in Finder, but a
    real file that would otherwise block this), same pattern already used
    for the audio/ and mlx_chunks/ cleanup."""
    try:
        (folder / ".gitkeep").unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        pass  # not empty (something else still in there) -- leave it alone


def prune_uncited_frames(conn, course_id: str, dry_run: bool = False) -> dict:
    """
    Deletes key_frame image files that are NOT referenced as a source
    citation by any APPROVED strategy for this course. Only call this
    AFTER human review/approval (stage 6) -- pruning before review risks
    deleting a frame you still needed to look at.
    """
    approved_strategy_ids = [
        r[0] for r in conn.execute(
            "SELECT strategy_id FROM strategies WHERE course_id = ? AND approved = 1", (course_id,)
        ).fetchall()
    ]

    if not approved_strategy_ids:
        return {"pruned": 0, "reason": "no approved strategies yet -- nothing pruned"}

    # cited frame paths -- referenced via key_frames tied to sessions in this course
    # (a full implementation would trace spec_json source_citations back to specific
    # frame_ids; this conservative version keeps any frame belonging to a session
    # that contributed evidence to an approved strategy)
    session_ids = [r[0] for r in conn.execute(
        """SELECT DISTINCT s.session_id FROM sessions s
           JOIN transcript_segments ts ON ts.session_id = s.session_id
           JOIN evidence e ON e.source_ref_id = ts.segment_id
           WHERE e.related_strategy_id IN ({})""".format(",".join("?" * len(approved_strategy_ids))),
        approved_strategy_ids,
    ).fetchall()]

    all_frames = conn.execute(
        """SELECT frame_id, session_id, image_path FROM key_frames
           WHERE session_id IN (SELECT session_id FROM sessions WHERE course_id = ?)""",
        (course_id,),
    ).fetchall()

    pruned = 0
    for frame_id, session_id, image_path in all_frames:
        if session_id not in session_ids:
            if not dry_run and Path(image_path).exists():
                Path(image_path).unlink()
            conn.execute("DELETE FROM key_frames WHERE frame_id = ?", (frame_id,))
            pruned += 1

    conn.commit()
    print(f"  [cleanup] {pruned}/{len(all_frames)} uncited frame(s) pruned for {course_id}")
    return {"pruned": pruned, "total_frames_checked": len(all_frames)}
