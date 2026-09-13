"""
Video processor.

Pipeline: extract audio -> transcribe (timestamped) -> [optional] diarize
-> detect key frames -> OCR each key frame.

v3 changes (merging in Video Notes Studio's transcription engine, now that
the Mac is confirmed Apple Silicon / M2):
  1. mlx-whisper is now the PRIMARY transcription engine (large-v3-turbo
     model) -- Apple Silicon-optimized, runs on the Neural Engine, and per
     Video Notes Studio's own findings gives much better accuracy on
     numbers/tickers/jargon than the 'small' CPU model we fell back to.
     faster-whisper stays wired as an automatic fallback if mlx-whisper
     isn't installed (e.g. testing on non-Apple-Silicon).
  2. mlx-whisper returns its full result in one blocking call rather than
     a streaming generator (unlike faster-whisper), so true per-segment
     progress isn't available the same way -- a background heartbeat
     thread prints elapsed time every 20s instead so it never looks frozen.

v2 changes (based on the real-world diagnostic run on a 115-minute WMV on
a MacBook Air, still relevant to the faster-whisper fallback path):
  - Default faster-whisper model is 'small', not 'medium' -- CPU-only,
    this trade-off made sense; on Apple Silicon, mlx-whisper above is
    strongly preferred instead of tuning this further.
  - Diarization is OFF by default (ENABLE_DIARIZATION = False). Every
    segment's speaker_role stays 'unknown' until turned on -- matches the
    plan's stated priority: what the trainer said and when, first.
  - Checkpoint/resume is actually implemented: a file's stage_completed
    is checked before redoing any stage, so a stop-and-restart doesn't
    repeat finished work.
"""

import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from processors.image_processor import ocr_image
from processors.text_export import safe_filename, write_text_export, format_seconds

try:
    import mlx_whisper
    HAS_MLX_WHISPER = True
except ImportError:
    HAS_MLX_WHISPER = False

try:
    from faster_whisper import WhisperModel
    HAS_FASTER_WHISPER = True
except ImportError:
    HAS_FASTER_WHISPER = False

try:
    from pyannote.audio import Pipeline as DiarizationPipeline
    HAS_PYANNOTE = True
except ImportError:
    HAS_PYANNOTE = False


# ---------- Tunable defaults (edit these directly, no need to touch the functions below) ----------

MLX_MODEL = "mlx-community/whisper-large-v3-turbo"   # used when mlx-whisper is available (Apple Silicon)
FASTER_WHISPER_MODEL_SIZE = "small"                  # fallback engine, CPU only
ENABLE_DIARIZATION = False       # True once you want trainer/participant separation -- much slower
CONFIDENCE_REVIEW_THRESHOLD = 0.55

# Phrases that usually mark the trainer pointing at something on screen or
# stating a concrete rule -- when a transcript segment contains one of
# these, an extra frame is captured at that exact moment even if it falls
# between scene-change and periodic samples. This is what actually catches
# "look at this crossover" or "the stop goes here" moments that a fixed
# sampling interval can miss entirely. Case-insensitive substring match.
TRANSCRIPT_TRIGGER_KEYWORDS = [
    "entry", "exit", "target", "stop loss", "stop-loss", " stop ", "buy", "sell",
    "formula", "excel", "look at this", "important", "no trade", "invalidation",
    "take profit", "risk reward", "position size",
]
TRANSCRIPT_TRIGGER_MIN_GAP_SECONDS = 4.0  # skip a trigger frame if one already
                                           # exists within this many seconds --
                                           # avoids near-duplicate frames/OCR cost
                                           # when scene-change already caught the moment


# ---------- Stage A: audio extraction ----------

def extract_audio(video_path: str, audio_out_path: str) -> None:
    Path(audio_out_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            audio_out_path,
        ],
        check=True,
        capture_output=True,
    )


def get_audio_duration_seconds(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


# ---------- Stage B: transcription (timestamped, with live progress) ----------

MLX_CHUNK_SECONDS = 10 * 60   # 10-minute chunks -- matches Video Notes Studio's approach,
                              # specifically to avoid memory pressure on 8GB RAM (confirmed
                              # your M2 has 8GB). Transcribing 115 minutes in one call risks
                              # swapping/slowdown; chunking keeps peak memory bounded regardless
                              # of total file length.


def _split_audio_into_chunks(audio_path: str, chunk_dir: str, chunk_seconds: int = MLX_CHUNK_SECONDS) -> list:
    """Splits a WAV file into fixed-length chunks via ffmpeg segmenting.
    Returns [(chunk_path, offset_seconds), ...] in order -- offset is added
    back to each chunk's segment timestamps so the final transcript's
    timestamps are still relative to the FULL original video, not the chunk."""
    Path(chunk_dir).mkdir(parents=True, exist_ok=True)
    pattern = str(Path(chunk_dir) / "chunk_%04d.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path, "-f", "segment",
         "-segment_time", str(chunk_seconds), "-c", "copy", pattern],
        check=True, capture_output=True,
    )
    chunk_files = sorted(Path(chunk_dir).glob("chunk_*.wav"))
    return [(str(f), i * chunk_seconds) for i, f in enumerate(chunk_files)]


SILENCE_THRESHOLD_DB = -35       # quieter than this counts as "silent"
SILENCE_CHUNK_SKIP_RATIO = 0.85  # if a chunk is at least this silent, skip transcribing it


def _chunk_silence_ratio(chunk_path: str) -> float:
    """
    Returns the fraction (0-1) of a chunk that's silence, via ffmpeg's
    silencedetect. Used to skip chunks that are almost entirely a break
    (per your note: sessions have 10-20+ min silent breaks) -- this avoids
    both wasted transcription time AND the hallucination loops Whisper
    produces on long silence (the repeated "no, no, no..." / "Okay.
    Okay..." runs seen in the real Course_12 transcript were exactly this).
    Does NOT touch timestamps -- the chunk is just skipped, not removed,
    so every other segment's timing stays exactly correct.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", chunk_path, "-af",
             f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d=1", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        silence_durations = [float(m) for m in re.findall(r"silence_duration:\s*([\d.]+)", result.stderr)]
        total_silence = sum(silence_durations)
        chunk_duration = get_audio_duration_seconds(chunk_path)
        return (total_silence / chunk_duration) if chunk_duration > 0 else 0.0
    except Exception:
        return 0.0  # if silence detection itself fails, just transcribe normally -- don't block on this


def _transcribe_with_mlx(audio_path: str, total_duration: float, work_dir: str = None) -> list:
    """mlx-whisper: Apple Silicon Neural Engine. Splits into 10-minute chunks
    (memory safety on 8GB RAM) and transcribes each in turn, printing a real
    per-chunk progress line -- so this DOES show incremental % now, unlike
    the single-call version."""
    print(f"  [transcribe] engine=mlx-whisper model={MLX_MODEL} "
          f"duration={total_duration/60:.1f} min -- splitting into "
          f"{MLX_CHUNK_SECONDS//60}-min chunks (memory safety on 8GB RAM)")

    chunk_dir = str(Path(work_dir or ".") / "mlx_chunks")
    chunks = _split_audio_into_chunks(audio_path, chunk_dir)
    print(f"  [transcribe] {len(chunks)} chunk(s) to process")

    all_segments = []
    skipped_silent_chunks = 0
    start_time = time.time()
    for i, (chunk_path, offset) in enumerate(chunks, start=1):
        silence_ratio = _chunk_silence_ratio(chunk_path)
        if silence_ratio >= SILENCE_CHUNK_SKIP_RATIO:
            skipped_silent_chunks += 1
            print(f"  [transcribe] chunk {i}/{len(chunks)} is {silence_ratio*100:.0f}% silence -- "
                  f"skipping (likely a break, not transcribing avoids hallucinated text)")
            continue

        result = mlx_whisper.transcribe(chunk_path, path_or_hf_repo=MLX_MODEL)
        for seg in result.get("segments", []):
            avg_logprob = seg.get("avg_logprob", -0.3)
            confidence = max(0.0, min(1.0, 1.0 + (avg_logprob / 2)))
            all_segments.append({
                "start": seg["start"] + offset,
                "end": seg["end"] + offset,
                "text": seg["text"].strip(),
                "confidence": confidence,
            })
        elapsed_min = (time.time() - start_time) / 60
        pct = i / len(chunks) * 100
        processed_min = min(i * MLX_CHUNK_SECONDS, total_duration) / 60
        print(f"  [transcribe] chunk {i}/{len(chunks)} done -- "
              f"{processed_min:.1f} / {total_duration/60:.1f} min ({pct:.0f}%) -- "
              f"{elapsed_min:.1f} min elapsed")
        print(f"[progress] transcription {pct:.0f}%")

    # chunk files were temporary working copies -- clean them up now
    for chunk_path, _ in chunks:
        Path(chunk_path).unlink(missing_ok=True)
    if chunks:
        try:
            chunks_dir = Path(chunks[0][0]).parent
            (chunks_dir / ".gitkeep").unlink(missing_ok=True)
            chunks_dir.rmdir()  # remove mlx_chunks/ itself once empty
        except OSError:
            pass  # not empty yet -- leave it alone

    elapsed_min = (time.time() - start_time) / 60
    print(f"  [transcribe] done -- {len(all_segments)} segments in {elapsed_min:.1f} min"
          + (f" ({total_duration/60/elapsed_min:.1f}x realtime)" if elapsed_min > 0 else "")
          + (f" -- {skipped_silent_chunks} silent chunk(s) skipped" if skipped_silent_chunks else ""))
    return all_segments


def _transcribe_with_faster_whisper(audio_path: str, total_duration: float, model_size: str) -> list:
    """CPU fallback -- used automatically if mlx-whisper isn't installed
    (e.g. testing on non-Apple-Silicon hardware)."""
    print(f"  [transcribe] engine=faster-whisper model={model_size} "
          f"duration={total_duration/60:.1f} min -- starting")

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments_gen, _info = model.transcribe(audio_path, language=None)

    results = []
    start_time = time.time()
    for i, seg in enumerate(segments_gen, start=1):
        confidence = max(0.0, min(1.0, 1.0 + (seg.avg_logprob / 2)))
        results.append(
            {"start": seg.start, "end": seg.end, "text": seg.text.strip(), "confidence": confidence}
        )
        if i % 5 == 0 or seg.end >= total_duration - 1:
            pct = min(100, seg.end / total_duration * 100) if total_duration else 0
            elapsed_min = (time.time() - start_time) / 60
            print(f"  [transcribe] {seg.end/60:.1f} / {total_duration/60:.1f} min "
                  f"({pct:.0f}%) -- {elapsed_min:.1f} min elapsed")

    print(f"  [transcribe] done -- {len(results)} segments")
    return results


def transcribe_audio(audio_path: str, model_size: str = FASTER_WHISPER_MODEL_SIZE, work_dir: str = None) -> list:
    """
    Returns a list of dicts: {start, end, text, confidence}.
    Uses mlx-whisper if available (Apple Silicon -- preferred, chunked for
    memory safety), otherwise falls back to faster-whisper (CPU), otherwise
    a labeled stub so the rest of the pipeline can still be exercised
    end-to-end.
    """
    if not HAS_MLX_WHISPER and not HAS_FASTER_WHISPER:
        return [
            {
                "start": 0.0, "end": 6.0,
                "text": "[TRANSCRIPTION STUB -- on Apple Silicon: pip install mlx-whisper "
                        "| otherwise: pip install faster-whisper]",
                "confidence": 0.0,
            }
        ]

    total_duration = get_audio_duration_seconds(audio_path)

    if HAS_MLX_WHISPER:
        return _transcribe_with_mlx(audio_path, total_duration, work_dir=work_dir)
    else:
        return _transcribe_with_faster_whisper(audio_path, total_duration, model_size)


# ---------- Stage C: diarization (trainer vs participant) — OPTIONAL ----------

def diarize_speakers(audio_path: str) -> list:
    """
    Returns a list of dicts: {start, end, speaker_cluster}.
    Only called when ENABLE_DIARIZATION is True -- this is the heaviest
    single stage on CPU, per the diagnostic report. Requires
    pyannote.audio + a HuggingFace login on your Mac.
    """
    if not HAS_PYANNOTE:
        print("  [diarize] pyannote.audio not installed -- skipping")
        return []
    print("  [diarize] running speaker diarization -- this is the slowest stage")
    start_time = time.time()
    pipeline = DiarizationPipeline.from_pretrained("pyannote/speaker-diarization-3.1")
    diarization = pipeline(audio_path)
    results = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        results.append({"start": turn.start, "end": turn.end, "speaker_cluster": speaker})
    print(f"  [diarize] done -- {(time.time()-start_time)/60:.1f} min, {len(results)} turns")
    return results


def assign_speaker_roles(transcript_segments: list, diarization_turns: list) -> list:
    for seg in transcript_segments:
        seg["speaker_cluster"] = None
        seg["speaker_role"] = "unknown"
        for turn in diarization_turns:
            if turn["start"] <= seg["start"] < turn["end"]:
                seg["speaker_cluster"] = turn["speaker_cluster"]
                break
    return transcript_segments


# ---------- Stage D: key-frame extraction ----------

def _run_ffmpeg_frame_select(video_path: str, vf: str, out_pattern: str):
    """
    Runs ffmpeg with a select filter, writing one frame per match in
    variable-frame-rate mode (no duplicate/skipped frames). The flag for
    this changed between ffmpeg versions -- older builds only understand
    "-vsync vfr", while ffmpeg 5.1+ deprecated it in favor of "-fps_mode
    vfr", and very recent builds (confirmed against a real run on ffmpeg
    9.0.1 via Homebrew) have dropped "-vsync" entirely, failing with
    "Unrecognized option 'vsync'" and silently producing zero scene-change
    frames for every video -- a real, previously undetected gap where
    every course would fall back to periodic-only sampling without any
    visible error in the dashboard. Tries the modern flag first (covers
    all currently-supported ffmpeg releases) and falls back to the legacy
    flag only for older installs that predate it.
    """
    base_cmd = ["ffmpeg", "-y", "-i", video_path, "-vf", vf]
    try:
        return subprocess.run(base_cmd + ["-fps_mode", "vfr", out_pattern],
                               check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if "Unrecognized option" in (e.stderr or "") or "fps_mode" in (e.stderr or ""):
            return subprocess.run(base_cmd + ["-vsync", "vfr", out_pattern],
                                   check=True, capture_output=True, text=True)
        raise


def extract_key_frames(video_path: str, frames_out_dir: str, scene_threshold: float = 0.3,
                        periodic_interval_seconds: int = 60) -> list:
    """Returns [{image_path, trigger_reason, timestamp_seconds}, ...] with
    REAL timestamps -- fixed from an earlier version that hardcoded 0.0 for
    every frame, which made screenshot evidence untraceable back to the
    video. Scene-change timestamps come from ffmpeg's showinfo filter log;
    periodic timestamps are simply index * interval (deterministic)."""
    Path(frames_out_dir).mkdir(parents=True, exist_ok=True)
    out_pattern = str(Path(frames_out_dir) / "frame_%04d.jpg")

    print("  [frames] extracting scene-change frames")
    try:
        result = _run_ffmpeg_frame_select(
            video_path, f"select='gt(scene,{scene_threshold})',showinfo", out_pattern
        )
        # showinfo prints one line per selected frame to stderr, containing
        # "pts_time:123.45" -- extract these in order to match frame_0001, 0002...
        scene_timestamps = [float(m) for m in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    except subprocess.CalledProcessError as e:
        # This happens most often when the video has few/no visual scene
        # changes above the threshold (e.g. a mostly-static slide/talking-
        # head recording) -- ffmpeg's image-sequence output errors if ZERO
        # frames matched the select filter, rather than just writing
        # nothing. That's not a real failure of this video's processing --
        # it just means there are no scene-change frames to capture. Log
        # it and continue with periodic frames only, instead of crashing
        # the whole pipeline (which previously lost frame/OCR evidence
        # entirely, even though transcription had already succeeded).
        print(f"  [frames] no scene-change frames captured (likely a low-motion video) -- "
              f"continuing with periodic sampling only. ffmpeg detail: {e.stderr[-300:] if e.stderr else e}")
        scene_timestamps = []

    print("  [frames] extracting periodic baseline frames")
    periodic_pattern = str(Path(frames_out_dir) / "periodic_%04d.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vf", f"fps=1/{periodic_interval_seconds}", periodic_pattern],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        # Same reasoning -- a very short video might not even have one
        # full periodic interval. Don't let this crash the whole course.
        print(f"  [frames] periodic sampling produced no frames -- continuing anyway. "
              f"ffmpeg detail: {e.stderr[-300:] if e.stderr else e}")

    frames = []
    scene_files = sorted(Path(frames_out_dir).glob("frame_*.jpg"))
    for i, f in enumerate(scene_files):
        ts = scene_timestamps[i] if i < len(scene_timestamps) else 0.0
        frames.append({"image_path": str(f), "trigger_reason": "scene_change", "timestamp_seconds": ts})

    periodic_files = sorted(Path(frames_out_dir).glob("periodic_*.jpg"))
    for i, f in enumerate(periodic_files):
        frames.append({"image_path": str(f), "trigger_reason": "periodic",
                        "timestamp_seconds": i * periodic_interval_seconds})

    print(f"  [frames] {len(frames)} frames extracted "
          f"({len(scene_timestamps)} with confirmed scene-change timestamps)")
    return frames


def extract_keyword_triggered_frames(video_path: str, transcript_segments: list, frames_out_dir: str,
                                      existing_timestamps: list, keywords: list = None,
                                      min_gap_seconds: float = TRANSCRIPT_TRIGGER_MIN_GAP_SECONDS) -> list:
    """
    Captures one extra frame at the midpoint of any transcript segment
    containing a trigger keyword (see TRANSCRIPT_TRIGGER_KEYWORDS) --
    this is Upgrade 3 from the architecture doc: fixed scene-change +
    periodic sampling alone can miss the exact moment the trainer says
    "the stop goes here" if nothing visually changes on screen right then.

    Skips a trigger if a frame already exists within min_gap_seconds of it
    (scene-change or periodic) -- no point paying for a near-duplicate
    frame and OCR call. existing_timestamps should be every timestamp
    already captured by extract_key_frames for this same video.

    Returns frames in the same shape as extract_key_frames, with
    trigger_reason='rule_keyword' so they're distinguishable in the DB.
    """
    keywords = keywords or TRANSCRIPT_TRIGGER_KEYWORDS
    Path(frames_out_dir).mkdir(parents=True, exist_ok=True)

    candidate_timestamps = []
    for seg in transcript_segments:
        text_lower = f" {seg['text'].lower()} "
        if any(kw in text_lower for kw in keywords):
            midpoint = (seg["start"] + seg["end"]) / 2
            candidate_timestamps.append(midpoint)

    covered = list(existing_timestamps)
    frames = []
    for i, ts in enumerate(candidate_timestamps):
        if any(abs(ts - c) < min_gap_seconds for c in covered):
            continue  # already covered by a nearby scene-change/periodic/earlier trigger frame
        out_path = str(Path(frames_out_dir) / f"trigger_{i:04d}.jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", video_path, "-vframes", "1", out_path],
                check=True, capture_output=True,
            )
            if Path(out_path).exists():
                frames.append({"image_path": out_path, "trigger_reason": "rule_keyword", "timestamp_seconds": ts})
                covered.append(ts)
        except subprocess.CalledProcessError as e:
            print(f"  [frames] keyword-triggered frame at {ts:.1f}s failed, skipping: "
                  f"{e.stderr[-200:] if e.stderr else e}")

    if frames:
        print(f"  [frames] {len(frames)} keyword-triggered frame(s) captured "
              f"({len(candidate_timestamps) - len(frames)} skipped as already covered)")
    return frames


# ---------- Orchestration with REAL checkpoint/resume ----------

# stage order -- used to decide what to skip based on files.stage_completed
STAGE_ORDER = [None, "audio_extracted", "transcript_done", "frames_done"]


def _stage_index(stage_completed: str) -> int:
    try:
        return STAGE_ORDER.index(stage_completed)
    except ValueError:
        return 0


def process_video_file(conn, file_id: str, session_id: str, video_path: str, work_dir: str,
                        periodic_interval_seconds: int = 60,
                        model_size: str = FASTER_WHISPER_MODEL_SIZE,
                        enable_diarization: bool = ENABLE_DIARIZATION) -> dict:
    work = Path(work_dir)
    audio_path = str(work / "audio" / f"{file_id}.wav")
    frames_dir = str(work / "frames" / file_id)

    row = conn.execute("SELECT stage_completed FROM files WHERE file_id = ?", (file_id,)).fetchone()
    current_stage = row[0] if row else None
    idx = _stage_index(current_stage)
    print(f"  [resume] current checkpoint: {current_stage or 'none'} -- resuming from there")

    # ---- Stage A: audio extraction ----
    if idx < 1:
        print("  [stage] extracting audio")
        extract_audio(video_path, audio_path)
        conn.execute("UPDATE files SET stage_completed = 'audio_extracted' WHERE file_id = ?", (file_id,))
        conn.commit()
    else:
        print("  [stage] audio already extracted -- skipping")
        if not Path(audio_path).exists():
            print("  [stage] audio file missing on disk despite checkpoint -- re-extracting")
            extract_audio(video_path, audio_path)

    # ---- Stage B (+C): transcription and optional diarization ----
    segments_transcribed = 0
    low_conf_count = 0
    if idx < 2:
        conn.execute("DELETE FROM transcript_segments WHERE session_id = ?", (session_id,))
        conn.commit()

        segments = transcribe_audio(audio_path, model_size=model_size, work_dir=work_dir)
        diar_turns = diarize_speakers(audio_path) if enable_diarization else []
        segments = assign_speaker_roles(segments, diar_turns)

        for seg in segments:
            needs_review = 1 if seg["confidence"] < CONFIDENCE_REVIEW_THRESHOLD else 0
            low_conf_count += needs_review
            conn.execute(
                """INSERT INTO transcript_segments
                   (segment_id, session_id, start_seconds, end_seconds, speaker_cluster,
                    speaker_role, text, confidence, needs_review)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), session_id, seg["start"], seg["end"],
                 seg.get("speaker_cluster"), seg.get("speaker_role", "unknown"),
                 seg["text"], seg["confidence"], needs_review),
            )
        segments_transcribed = len(segments)
        conn.execute("UPDATE files SET stage_completed = 'transcript_done' WHERE file_id = ?", (file_id,))
        conn.commit()

        # write a readable transcript .txt -- this is what was missing before:
        # extraction was succeeding but nothing human-readable ever landed
        # in the output folder, only the database.
        session_title_row = conn.execute("SELECT title FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        session_title = session_title_row[0] if session_title_row else file_id
        transcript_lines = [f"Transcript -- {session_title}", "=" * 60, ""]
        for seg in segments:
            flag = "  [LOW CONFIDENCE]" if seg["confidence"] < CONFIDENCE_REVIEW_THRESHOLD else ""
            transcript_lines.append(
                f"[{format_seconds(seg['start'])} - {format_seconds(seg['end'])}]{flag} {seg['text']}"
            )
        transcript_path = write_text_export(
            str(work / "Transcripts"), f"{safe_filename(session_title)}.txt", "\n".join(transcript_lines)
        )
        print(f"  [export] readable transcript written -> {transcript_path}")
    else:
        print("  [stage] transcript already done -- skipping")
        existing = conn.execute(
            "SELECT COUNT(*), SUM(needs_review) FROM transcript_segments WHERE session_id = ?", (session_id,)
        ).fetchone()
        segments_transcribed = existing[0] or 0
        low_conf_count = existing[1] or 0

    # ---- Stage D: key frames + OCR ----
    frames_count = 0
    if idx < 3:
        conn.execute("DELETE FROM key_frames WHERE session_id = ?", (session_id,))
        conn.commit()

        frames = extract_key_frames(video_path, frames_dir, periodic_interval_seconds=periodic_interval_seconds)

        # transcript-triggered frames (Upgrade 3) -- needs the transcript, so
        # it runs after scene-change/periodic and reads back from the DB
        # (works whether transcription just ran above or was already done
        # in an earlier, resumed run).
        transcript_rows = conn.execute(
            "SELECT start_seconds AS start, end_seconds AS end, text FROM transcript_segments WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        transcript_segments_for_trigger = [
            {"start": r[0], "end": r[1], "text": r[2]} for r in transcript_rows
        ]
        existing_timestamps = [f["timestamp_seconds"] for f in frames]
        trigger_frames = extract_keyword_triggered_frames(
            video_path, transcript_segments_for_trigger, frames_dir, existing_timestamps
        )
        frames.extend(trigger_frames)

        for frame in frames:
            ocr_text = ocr_image(frame["image_path"])
            conn.execute(
                """INSERT INTO key_frames
                   (frame_id, session_id, timestamp_seconds, image_path, ocr_text, trigger_reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), session_id, frame["timestamp_seconds"], frame["image_path"],
                 ocr_text, frame["trigger_reason"]),
            )
        frames_count = len(frames)
        conn.execute(
            "UPDATE files SET status = 'SUCCESS', stage_completed = 'frames_done', processed_at = ? WHERE file_id = ?",
            (datetime.now(timezone.utc).isoformat(), file_id),
        )
        conn.commit()

        # readable summary of what OCR found on-screen, timestamped
        session_title_row = conn.execute("SELECT title FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        session_title = session_title_row[0] if session_title_row else file_id
        ocr_lines = [f"On-screen text (OCR) -- {session_title}", "=" * 60, ""]
        ocr_rows = conn.execute(
            "SELECT timestamp_seconds, trigger_reason, ocr_text FROM key_frames WHERE session_id = ? ORDER BY timestamp_seconds",
            (session_id,),
        ).fetchall()
        for ts, trigger, ocr_text in ocr_rows:
            if ocr_text and ocr_text.strip():
                ocr_lines.append(f"[{format_seconds(ts)}] ({trigger})\n{ocr_text.strip()}\n")
        ocr_path = write_text_export(
            str(work / "OCR"), f"{safe_filename(session_title)}_frames.txt", "\n".join(ocr_lines)
        )
        print(f"  [export] readable frame-OCR summary written -> {ocr_path}")
    else:
        print("  [stage] frames already done -- skipping")
        frames_count = conn.execute(
            "SELECT COUNT(*) FROM key_frames WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        conn.execute("UPDATE files SET status = 'SUCCESS' WHERE file_id = ?", (file_id,))
        conn.commit()

    return {
        "segments_transcribed": segments_transcribed,
        "segments_needing_review": low_conf_count,
        "key_frames_extracted": frames_count,
        "whisper_available": HAS_MLX_WHISPER or HAS_FASTER_WHISPER,
        "engine_used": "mlx-whisper" if HAS_MLX_WHISPER else ("faster-whisper" if HAS_FASTER_WHISPER else "stub"),
        "pyannote_available": HAS_PYANNOTE,
        "diarization_enabled": enable_diarization,
        "model_size": model_size,
        "resumed_from_stage": current_stage,
    }


if __name__ == "__main__":
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.schema import get_connection

    conn = get_connection("TradingIntelligence.db")
    row = conn.execute(
        """SELECT f.file_id, s.session_id, f.original_path FROM files f
           JOIN sessions s ON s.file_id = f.file_id
           WHERE f.file_type = 'video' LIMIT 1"""
    ).fetchone()
    file_id, session_id, path = row
    result = process_video_file(conn, file_id, session_id, path, work_dir="extracted",
                                 periodic_interval_seconds=2)
    print("\nVideo processing result:", json.dumps(result, indent=2))
    conn.close()
