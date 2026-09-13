"""
Shared helper: every processor writes its findings to the database (for
querying, linking, AI processing) AND to a plain, human-readable .txt
file in the matching output folder (for you to actually open and read).

This was a real gap -- extraction was succeeding, but nothing readable
ever landed in 01_VIDEO_EVIDENCE/ or 02_MATERIAL_EVIDENCE/, only the
database. This module fixes that for every processor at once.
"""

import re
from pathlib import Path

# Only characters that are ACTUALLY illegal on a filesystem (path
# separators, colons, Windows-reserved characters, control characters).
# The previous version used c.isalnum(), which returns False for Unicode
# COMBINING MARKS -- these aren't decorative, they're structurally
# required to render many scripts correctly (Devanagari/Hindi vowel
# signs, Thai tone marks, Vietnamese diacritics, etc). Stripping them
# corrupted real video titles into unreadable fragments (confirmed:
# a real Hindi YouTube title came out as "क_ड__ग क_ अ_त_..."). This
# version only touches characters that would actually break on disk.
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_filename(name: str, max_len: int = 80) -> str:
    cleaned = _FORBIDDEN_FILENAME_CHARS.sub("_", name)
    cleaned = cleaned.strip().strip(".")  # avoid trailing dots/spaces (problematic on some filesystems)
    return cleaned[:max_len] or "untitled"


def write_text_export(output_dir: str, filename: str, content: str) -> str:
    """Writes content to output_dir/filename (creating the folder if
    needed). Returns the full path written, for logging."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(output_dir) / filename
    out_path.write_text(content, encoding="utf-8")
    return str(out_path)


def format_seconds(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
