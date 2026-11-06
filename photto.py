#!/usr/bin/env python3
"""SIFT — locally review folders and find text in images and videos.

Usage:  sift [DIR]          open the folder in the SIFT GUI
        sift                open SIFT and choose a folder

Runs a private local web app (127.0.0.1 only, per-run token) and shows it in a
native window (WebKitGTK), a browser app window, or your default browser.
Nothing is deleted until you confirm in Review, and even then files go to the
freedesktop Trash, so they can be restored.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import secrets
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sift_ocr import TextJobs, IMAGE_EXTENSIONS as OCR_IMAGES, VIDEO_EXTENSIONS as OCR_VIDEOS

VERSION = "1.2.0"
MAX_FILES = 100_000
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 200
SCAN_BATCH = 250
INDEX_SCHEMA_VERSION = 1
IS_WIN = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)  # PyInstaller build
APP_DIR = Path(__file__).resolve().parent
STATIC = (Path(getattr(sys, "_MEIPASS", APP_DIR)) / "static").resolve()  # resolve: _MEIPASS may be an 8.3 path
if IS_WIN:
    _local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "SIFT"
    CACHE, STATE_DIR = _local / "cache", _local / "state"
else:
    CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "sift"
    STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "sift"
_legacy_state = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "photto" / "state"
                 if IS_WIN else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "photto")
def migrate_state_if_absent(source: Path, destination: Path) -> bool:
    """Copy legacy state once; if a SIFT store exists, leave both stores alone."""
    if destination.exists() or not source.is_dir():
        return False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        return True
    except OSError:
        return False


migrate_state_if_absent(_legacy_state, STATE_DIR)

# Windows' registry-based guesses are unreliable (e.g. .js as text/plain), so pin the important ones.
for _ext, _type in {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml",
                    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
                    ".mov": "video/quicktime", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac",
                    ".opus": "audio/ogg", ".ogg": "audio/ogg", ".wav": "audio/wav", ".webp": "image/webp",
                    ".avif": "image/avif", ".pdf": "application/pdf", ".json": "application/json"}.items():
    mimetypes.add_type(_type, _ext)

# No console window flashes for helper tools on Windows.
NOWIN = {"creationflags": 0x08000000} if IS_WIN else {}


def run_quiet(cmd, **kw):
    return subprocess.run(cmd, **NOWIN, **kw)


def open_path(path: Path, reveal: bool = False) -> None:
    """Open a file with its default app, or show it in the file manager."""
    if IS_WIN:
        if reveal:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # noqa: S606
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R" if reveal else "--", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path.parent if reveal else path)], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)

IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg", ".ico", ".jfif"}
IMAGE_CONVERT = {".heic", ".heif", ".tif", ".tiff", ".cr2", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".psd", ".jxl"}
VIDEO = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ogv", ".3gp", ".wmv", ".flv", ".mpg", ".mpeg", ".ts"}
AUDIO = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma"}
PDF = {".pdf"}
TEXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".rs", ".go",
    ".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".rb", ".php", ".sh", ".bash", ".zsh", ".fish", ".lua",
    ".sql", ".tex", ".srt", ".vtt", ".env", ".gitignore", ".dockerfile", ".nix", ".vue", ".svelte",
}
DOC = {".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp", ".epub"}
ARCHIVE = {".zip", ".tar", ".gz", ".tgz", ".xz", ".bz2", ".zst", ".7z", ".rar", ".iso", ".deb", ".rpm", ".appimage"}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".cache", ".Trash", ".Trash-1000", "SIFT_docs",
             "$RECYCLE.BIN", "System Volume Information", "$Recycle.Bin", "AppData"}


def is_hidden(path: Path, name: str | None = None) -> bool:
    name = name or path.name
    if name.startswith("."):
        return True
    if IS_WIN:
        try:
            return bool(os.stat(path, follow_symlinks=False).st_file_attributes & 0x6)  # HIDDEN | SYSTEM
        except (OSError, AttributeError):
            return False
    return False


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE:
        return "image"
    if ext in IMAGE_CONVERT:
        return "image-convert"
    if ext in VIDEO:
        return "video"
    if ext in AUDIO:
        return "audio"
    if ext in PDF:
        return "pdf"
    if ext in TEXT or (not ext and path.stat().st_size < 512_000 and is_probably_text(path)):
        return "text"
    if ext in DOC:
        return "doc"
    if ext in ARCHIVE:
        return "archive"
    return "other"


def is_probably_text(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(2048)
        return b"\0" not in chunk
    except OSError:
        return False


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------- trash ----

def trash_windows(path: Path) -> None:
    """Send to the Recycle Bin via SHFileOperationW (undoable)."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(path.resolve()) + "\0",
                         fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted or path.exists():
        raise OSError(f"Recycle Bin refused the file (code {rc})")


def trash(path: Path) -> None:
    """Move to the system trash: Recycle Bin on Windows; freedesktop Trash elsewhere."""
    if IS_WIN:
        return trash_windows(path)
    if shutil.which("gio"):
        r = run_quiet(["gio", "trash", "--", str(path)], capture_output=True, text=True)
        if r.returncode == 0:
            return
    if shutil.which("kioclient"):
        r = run_quiet(["kioclient", "move", str(path), "trash:/"], capture_output=True, text=True)
        if r.returncode == 0:
            return
    # Manual implementation of the freedesktop.org trash spec (home trash).
    trash_dir = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "Trash"
    files, info = trash_dir / "files", trash_dir / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    name, i = path.name, 1
    while (files / name).exists() or (info / f"{name}.trashinfo").exists():
        name = f"{path.stem}.{i}{path.suffix}"
        i += 1
    (info / f"{name}.trashinfo").write_text(
        "[Trash Info]\n"
        f"Path={urllib.parse.quote(str(path.resolve()))}\n"
        f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
    )
    shutil.move(str(path), files / name)


# --------------------------------------------------------------- state -----

def recent_file() -> Path:
    return STATE_DIR / "recent.json"


def load_recent() -> list[str]:
    try:
        return [p for p in json.loads(recent_file().read_text()) if Path(p).is_dir()]
    except (OSError, ValueError):
        return []


def add_recent(path: Path) -> None:
    items = [str(path)] + [p for p in load_recent() if p != str(path)]
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        recent_file().write_text(json.dumps(items[:10]))
    except OSError:
        pass


GROUPS = {
    "all": None,
    "photos": ("image", "image-convert"),
    "videos": ("video",),
    "audio": ("audio",),
    "docs": ("pdf", "text", "doc"),
    "other": ("archive", "other"),
}
SORTS = ("shuffle", "newest", "oldest", "largest", "name")

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  rel       TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  dir       TEXT NOT NULL,
  ext       TEXT NOT NULL,
  kind      TEXT NOT NULL,
  size      INTEGER NOT NULL,
  mtime     REAL NOT NULL,
  sort_name TEXT NOT NULL,
  shuffle   REAL NOT NULL,
  decision  TEXT,
  seen      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_mtime   ON files(mtime, rel);
CREATE INDEX IF NOT EXISTS files_size    ON files(size, rel);
CREATE INDEX IF NOT EXISTS files_name    ON files(sort_name, rel);
CREATE INDEX IF NOT EXISTS files_shuffle ON files(shuffle, rel);
CREATE INDEX IF NOT EXISTS files_kind    ON files(kind, decision);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def sort_name(name: str) -> str:
    """Natural-sort key: lowercase with digit runs padded to 20 digits."""
    return re.sub(r"\d+", lambda m: m.group(0).zfill(20), name.lower())


def encode_cursor(sort: str, key: list) -> str:
    raw = json.dumps({"v": 1, "s": sort, "k": key}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, sort: str) -> list | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if data.get("v") != 1 or data.get("s") != sort:
            return None
        key = data.get("k")
        return key if isinstance(key, list) and len(key) == 2 else None
    except (ValueError, TypeError):
        return None


def row_file(row: sqlite3.Row) -> dict:
    return {
        "path": row["rel"],
        "name": row["name"],
        "dir": row["dir"],
        "ext": row["ext"],
        "kind": row["kind"],
        "size": row["size"],
        "sizeH": human(row["size"]),
        "mtime": row["mtime"],
        "decision": row["decision"],
    }


class FolderIndex:
    """A disposable SQLite cache of one folder's file metadata.

    ``decisions.json`` stays authoritative; the ``decision`` column is only a
    query cache reapplied from that file on every open.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._open()
        except sqlite3.DatabaseError:
            # A truncated or corrupt cache is disposable: rebuild it. The
            # decisions JSON is a separate file and is never touched here.
            self._discard_files()
            self._open()

    def _discard_files(self) -> None:
        try:
            self._conn.close()
        except (AttributeError, sqlite3.Error):
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.path) + suffix).unlink()
            except OSError:
                pass

    def _open(self) -> None:
        self._conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self.lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._ensure_schema()

    # -------------------------------------------------------- lifecycle --
    def _meta(self, key: str, default=None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, **values) -> None:
        for key, value in values.items():
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def _ensure_schema(self) -> None:
        self._conn.executescript(INDEX_SCHEMA)
        version = self._meta("schema_version")
        try:
            mismatch = version is not None and int(version) != INDEX_SCHEMA_VERSION
        except (TypeError, ValueError):
            mismatch = True
        if mismatch:
            self._conn.executescript("DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS meta;")
            self._conn.executescript(INDEX_SCHEMA)
            version = None
        self._conn.commit()
        if version is None:
            self._set_meta(schema_version=INDEX_SCHEMA_VERSION, revision=0, generation=0,
                           scan_state="idle", scanned=0, started_at=0, error="", truncated=0)
            self._conn.commit()

    def close(self) -> None:
        with self.lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def file_count(self) -> int:
        with self.lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def revision(self) -> int:
        with self.lock:
            return int(self._meta("revision", "0") or 0)

    def current_generation(self) -> int:
        with self.lock:
            return int(self._meta("generation", "0") or 0)

    def scan_info(self) -> dict:
        with self.lock:
            error = self._meta("error", "") or ""
            return {
                "state": self._meta("scan_state", "idle") or "idle",
                "scanned": int(self._meta("scanned", "0") or 0),
                "startedAt": int(self._meta("started_at", "0") or 0),
                "error": error or None,
            }

    def truncated(self) -> bool:
        with self.lock:
            return bool(int(self._meta("truncated", "0") or 0))

    # ---------------------------------------------------------- scanning --
    def begin_scan(self) -> int:
        with self.lock:
            generation = int(self._meta("generation", "0") or 0) + 1
            self._set_meta(generation=generation, scan_state="scanning", scanned=0,
                           started_at=int(time.time()), error="", truncated=0,
                           revision=self.revision() + 1)
            self._conn.commit()
            return generation

    def upsert_batch(self, rows: list[tuple], generation: int, scanned: int) -> bool:
        """Write one batch. Stale generations are ignored, not mixed in."""
        with self.lock:
            if int(self._meta("generation", "0") or 0) != generation:
                return False
            self._conn.executemany(
                "INSERT INTO files(rel, name, dir, ext, kind, size, mtime, sort_name, shuffle, decision, seen) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(rel) DO UPDATE SET name=excluded.name, dir=excluded.dir, ext=excluded.ext, "
                "kind=excluded.kind, size=excluded.size, mtime=excluded.mtime, "
                "sort_name=excluded.sort_name, seen=excluded.seen",
                rows,
            )
            self._set_meta(scanned=scanned, revision=self.revision() + 1)
            self._conn.commit()
            return True

    def finish_scan(self, generation: int, scanned: int, truncated: bool) -> list[str]:
        """Delete vanished rows and return their relative paths."""
        with self.lock:
            if int(self._meta("generation", "0") or 0) != generation:
                return []
            gone = [r["rel"] for r in self._conn.execute("SELECT rel FROM files WHERE seen != ?", (generation,))]
            self._conn.execute("DELETE FROM files WHERE seen != ?", (generation,))
            self._set_meta(scan_state="complete", scanned=scanned, truncated=int(truncated),
                           error="", revision=self.revision() + 1)
            self._conn.commit()
            return gone

    def fail_scan(self, generation: int, error: str, scanned: int = 0) -> None:
        with self.lock:
            if int(self._meta("generation", "0") or 0) != generation:
                return
            self._set_meta(scan_state="error", error=error, scanned=scanned,
                           revision=self.revision() + 1)
            self._conn.commit()

    def set_scan_error(self, error: str) -> None:
        with self.lock:
            self._set_meta(scan_state="error", error=error, revision=self.revision() + 1)
            self._conn.commit()

    # --------------------------------------------------------- decisions --
    def apply_decisions(self, decisions: dict[str, str]) -> None:
        with self.lock:
            self._conn.execute("UPDATE files SET decision=NULL")
            if decisions:
                self._conn.executemany("UPDATE files SET decision=? WHERE rel=?",
                                       [(d, rel) for rel, d in decisions.items()])
            self._conn.commit()

    def set_decision(self, rel: str, decision: str | None) -> None:
        with self.lock:
            self._conn.execute("UPDATE files SET decision=? WHERE rel=?", (decision, rel))
            self._conn.commit()

    def remove_rows(self, rels: list[str]) -> None:
        if not rels:
            return
        with self.lock:
            self._conn.executemany("DELETE FROM files WHERE rel=?", [(rel,) for rel in rels])
            self._conn.commit()

    # ------------------------------------------------------------ queries --
    def counts(self) -> dict:
        with self.lock:
            row = self._conn.execute(
                "SELECT COUNT(*) total, "
                "SUM(CASE WHEN decision IS NULL THEN 1 ELSE 0 END) undecided, "
                "SUM(CASE WHEN decision='keep' THEN 1 ELSE 0 END) kept, "
                "SUM(CASE WHEN decision='delete' THEN 1 ELSE 0 END) deleted, "
                "SUM(CASE WHEN decision='favorite' THEN 1 ELSE 0 END) favorited, "
                "SUM(CASE WHEN kind IN ('image','image-convert') THEN 1 ELSE 0 END) photos, "
                "SUM(CASE WHEN kind='video' THEN 1 ELSE 0 END) videos, "
                "SUM(CASE WHEN kind='audio' THEN 1 ELSE 0 END) audio, "
                "SUM(CASE WHEN kind IN ('pdf','text','doc') THEN 1 ELSE 0 END) docs, "
                "SUM(CASE WHEN kind IN ('archive','other') THEN 1 ELSE 0 END) other, "
                "SUM(CASE WHEN kind IN ('image','image-convert') AND decision IS NULL THEN 1 ELSE 0 END) photos_left, "
                "SUM(CASE WHEN kind='video' AND decision IS NULL THEN 1 ELSE 0 END) videos_left, "
                "SUM(CASE WHEN kind='audio' AND decision IS NULL THEN 1 ELSE 0 END) audio_left, "
                "SUM(CASE WHEN kind IN ('pdf','text','doc') AND decision IS NULL THEN 1 ELSE 0 END) docs_left, "
                "SUM(CASE WHEN kind IN ('archive','other') AND decision IS NULL THEN 1 ELSE 0 END) other_left, "
                "SUM(CASE WHEN decision='keep' THEN size ELSE 0 END) keep_bytes, "
                "SUM(CASE WHEN decision='delete' THEN size ELSE 0 END) delete_bytes, "
                "SUM(CASE WHEN decision='favorite' THEN size ELSE 0 END) fav_bytes "
                "FROM files"
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "undecided": int(row["undecided"] or 0),
            "byDecision": {"keep": int(row["kept"] or 0), "delete": int(row["deleted"] or 0),
                           "favorite": int(row["favorited"] or 0)},
            "byGroup": {"photos": int(row["photos"] or 0), "videos": int(row["videos"] or 0),
                        "audio": int(row["audio"] or 0), "docs": int(row["docs"] or 0),
                        "other": int(row["other"] or 0)},
            "undecidedByGroup": {"photos": int(row["photos_left"] or 0),
                                 "videos": int(row["videos_left"] or 0),
                                 "audio": int(row["audio_left"] or 0),
                                 "docs": int(row["docs_left"] or 0),
                                 "other": int(row["other_left"] or 0)},
            "bytesByDecision": {"keep": int(row["keep_bytes"] or 0),
                                "delete": int(row["delete_bytes"] or 0),
                                "favorite": int(row["fav_bytes"] or 0)},
        }

    def page(self, cursor: str = "", offset: int = 0, limit: int = DEFAULT_PAGE_SIZE,
             sort: str = "shuffle", group: str = "all", decision: str = "none") -> dict:
        sort = sort if sort in SORTS else "shuffle"
        group = group if group in GROUPS else "all"
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        offset = max(0, int(offset))
        where, params = [], []

        kinds = GROUPS[group]
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if decision == "none":
            where.append("decision IS NULL")
        elif decision in ("keep", "delete", "favorite"):
            where.append("decision=?")
            params.append(decision)

        key = decode_cursor(cursor, sort)
        if key is not None:
            if sort == "shuffle":
                where.append("(shuffle > ? OR (shuffle = ? AND rel > ?))")
            elif sort == "newest":
                where.append("(mtime < ? OR (mtime = ? AND rel > ?))")
            elif sort == "oldest":
                where.append("(mtime > ? OR (mtime = ? AND rel > ?))")
            elif sort == "largest":
                where.append("(size < ? OR (size = ? AND rel > ?))")
            else:
                where.append("(sort_name > ? OR (sort_name = ? AND rel > ?))")
            params.extend([key[0], key[0], key[1]])

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order = {
            "shuffle": "shuffle ASC, rel ASC",
            "newest": "mtime DESC, rel ASC",
            "oldest": "mtime ASC, rel ASC",
            "largest": "size DESC, rel ASC",
            "name": "sort_name ASC, rel ASC",
        }[sort]
        if key is not None:
            tail = " LIMIT ?"
            query_params = list(params) + [limit + 1]
        else:
            tail = " LIMIT ? OFFSET ?"
            query_params = list(params) + [limit + 1, offset]
        with self.lock:
            rows = self._conn.execute(
                f"SELECT * FROM files{clause} ORDER BY {order}{tail}", query_params
            ).fetchall()

        files = [row_file(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            last_key = {
                "shuffle": [last["shuffle"], last["rel"]],
                "newest": [last["mtime"], last["rel"]],
                "oldest": [last["mtime"], last["rel"]],
                "largest": [last["size"], last["rel"]],
                "name": [last["sort_name"], last["rel"]],
            }[sort]
            next_cursor = encode_cursor(sort, last_key)
        return {
            "offset": offset,
            "limit": limit,
            "effectiveLimit": limit,
            "files": files,
            "nextCursor": next_cursor,
        }


class Session:
    def __init__(self, root: Path, recursive: bool, include_hidden: bool,
                 index_dir: Path | None = None):
        add_recent(root)
        self.truncated = False
        self.root = root
        self.recursive = recursive
        self.include_hidden = include_hidden
        key = hashlib.sha1(str(root).encode()).hexdigest()[:16]
        self.state_file = STATE_DIR / f"{key}.json"
        self.lock = threading.RLock()
        self.decisions: dict[str, str] = {}  # relpath -> keep|delete|favorite
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self.decisions = data.get("decisions", {})
            except (OSError, ValueError):
                pass
        self.index = FolderIndex((index_dir or (CACHE / "index")) / f"{key}.sqlite3")
        self.text_jobs = TextJobs(root, (CACHE / "text") / f"{key}.sqlite3", recursive, include_hidden)
        self.index.apply_decisions(self.decisions)
        self._scan_lock = threading.Lock()
        self._scan_cond = threading.Condition()
        self._scan_cancel = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._generation = 0
        self._scan_errors: list[str] = []
        self.truncated = False
        self.start_scan()

    # ------------------------------------------------------------ state --
    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"root": str(self.root), "decisions": self.decisions}))
        tmp.replace(self.state_file)

    def resolve(self, rel: str) -> Path | None:
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            return None
        return p

    def set_decision(self, rel: str, decision: str | None) -> None:
        with self.lock:
            if decision in ("keep", "delete", "favorite"):
                self.decisions[rel] = decision
            else:
                self.decisions.pop(rel, None)
            self.save()
            self.index.set_decision(rel, self.decisions.get(rel))

    def reset_decisions(self) -> None:
        with self.lock:
            self.decisions.clear()
            self.save()
            self.index.apply_decisions(self.decisions)

    def sync_index_decisions(self) -> None:
        with self.lock:
            self.index.apply_decisions(self.decisions)

    # ---------------------------------------------------------- scanning --
    def start_scan(self, refresh: bool = False) -> int:
        with self._scan_lock:
            if self._scan_thread and self._scan_thread.is_alive():
                if not refresh:
                    return self._generation
                self._scan_cancel.set()
            cancel = threading.Event()
            self._scan_cancel = cancel
            self._scan_errors = []
            self.truncated = False
            generation = self.index.begin_scan()
            self._generation = generation
            self._scan_thread = threading.Thread(
                target=self._scan_worker, args=(generation, cancel),
                name="photto-scan", daemon=True)
            self._scan_thread.start()
            return generation

    def _flush(self, batch: list[tuple], generation: int, scanned: int) -> bool:
        if not batch:
            return True
        ok = self.index.upsert_batch(batch, generation, scanned)
        with self._scan_cond:
            self._scan_cond.notify_all()
        return ok

    def _scan_worker(self, generation: int, cancel: threading.Event) -> None:
        batch: list[tuple] = []
        seen = 0
        truncated = False
        try:
            for p in self.iter_files():
                if cancel.is_set() or self.index.current_generation() != generation:
                    return
                if seen >= MAX_FILES:
                    truncated = True
                    break
                try:
                    if p.is_symlink() or not p.is_file():
                        continue
                    st = p.stat()
                    kind = kind_of(p)
                except OSError:
                    continue
                rel = p.relative_to(self.root).as_posix()
                parent = str(Path(rel).parent) if Path(rel).parent != Path(".") else ""
                batch.append((rel, p.name, parent, p.suffix.lower().lstrip("."), kind,
                              st.st_size, st.st_mtime, sort_name(p.name), random.random(), generation))
                seen += 1
                if len(batch) >= SCAN_BATCH:
                    if not self._flush(batch, generation, seen):
                        return
                    batch = []
            if not self._flush(batch, generation, seen):
                return
            gone = self.index.finish_scan(generation, seen, truncated)
            self.truncated = truncated
            # Marks survive unless the file is really gone.
            with self.lock:
                stale = [k for k in self.decisions if not (self.root / k).is_file()]
                if gone:
                    stale += [k for k in gone if k in self.decisions and k not in stale]
                for k in stale:
                    del self.decisions[k]
                if stale:
                    self.save()
                # Rows that entered the index during this generation start
                # with a NULL cache column; reapply the authoritative JSON.
                self.index.apply_decisions(self.decisions)
            if self._scan_errors:
                self.index.set_scan_error("; ".join(self._scan_errors[:3]))
        except Exception as err:  # noqa: BLE001 - a failed scan must not kill the app
            self.index.fail_scan(generation, str(err), seen)
        finally:
            with self._scan_cond:
                self._scan_cond.notify_all()

    def wait_for_scan(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._scan_cond:
            while True:
                state = self.index.scan_info()["state"]
                thread = self._scan_thread
                if state != "scanning" and (thread is None or not thread.is_alive()):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._scan_cond.wait(min(remaining, 0.1))

    def _wait_for_rows(self, timeout: float = 2.0) -> None:
        """A cold page request waits only for the first batch, never the full scan."""
        if self.index.file_count() > 0 or self.index.scan_info()["state"] != "scanning":
            return
        deadline = time.monotonic() + timeout
        with self._scan_cond:
            while self.index.file_count() == 0 and self.index.scan_info()["state"] == "scanning":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._scan_cond.wait(min(remaining, 0.05))

    def iter_files(self):
        if not self.recursive:
            try:
                entries = list(self.root.iterdir())
            except OSError as err:
                self._scan_errors.append(str(err))
                return
            yield from (p for p in entries if self.include_hidden or not is_hidden(p))
            return

        def onerror(err):
            self._scan_errors.append(str(err))

        for dirpath, dirnames, filenames in os.walk(self.root, onerror=onerror):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and
                                 (self.include_hidden or not is_hidden(Path(dirpath) / d, d)))
            for name in filenames:
                if self.include_hidden or not is_hidden(Path(dirpath) / name, name):
                    yield Path(dirpath) / name

    # ------------------------------------------------------------ queries --
    def page(self, **kwargs) -> dict:
        self._wait_for_rows()
        return self.index.page(**kwargs)

    def scan_payload(self) -> dict:
        return {"revision": self.index.revision(), "scan": self.index.scan_info(),
                "counts": self.index.counts(), "truncated": self.index.truncated()}

    def close(self) -> None:
        with self._scan_lock:
            self._scan_cancel.set()
            thread = self._scan_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self.index.close()
        self.text_jobs.close()


# ------------------------------------------------------------- previews ----

THUMB_LIMIT = max(2, min(4, os.cpu_count() or 2))
THUMB_SEMAPHORE = threading.BoundedSemaphore(THUMB_LIMIT)


def image_size(p: Path) -> tuple[int, int] | None:
    """Best-effort dimensions without a third-party decoder."""
    try:
        with open(p, "rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head.startswith(b"BM") and len(head) >= 26:
                return struct.unpack("<ii", head[18:26])
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2:
                        return None
                    if marker[0] != 0xFF:
                        continue
                    code = marker[1]
                    if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
                        continue
                    raw = f.read(2)
                    if len(raw) < 2:
                        return None
                    length = int.from_bytes(raw, "big")
                    if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        data = f.read(5)
                        if len(data) < 5:
                            return None
                        height = int.from_bytes(data[1:3], "big")
                        width = int.from_bytes(data[3:5], "big")
                        return width, height
                    f.seek(max(0, length - 2), 1)
    except OSError:
        return None
    return None


def cache_path(p: Path, tag: str, ext: str) -> Path:
    st = p.stat()
    h = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}|{tag}".encode()).hexdigest()
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / f"{h}.{ext}"


def video_thumb(p: Path) -> Path | None:
    out = cache_path(p, "vthumb", "jpg")
    if out.exists():
        return out
    if shutil.which("ffmpegthumbnailer"):
        run_quiet(["ffmpegthumbnailer", "-i", str(p), "-o", str(out), "-s", "640", "-q", "8"],
                       capture_output=True, timeout=30)
    elif shutil.which("ffmpeg"):
        run_quiet(["ffmpeg", "-y", "-ss", "1", "-i", str(p), "-frames:v", "1", "-vf", "scale=640:-2", str(out)],
                       capture_output=True, timeout=30)
    return out if out.exists() else None


def convert_image(p: Path) -> Path | None:
    """HEIC/TIFF/RAW → JPEG so browsers can show them."""
    out = cache_path(p, "conv", "jpg")
    if out.exists():
        return out
    tools = []
    if shutil.which("magick"):
        tools.append(["magick", str(p) + "[0]", "-auto-orient", "-resize", "2400x2400>", "-quality", "88", str(out)])
    if shutil.which("vips"):
        tools.append(["vips", "thumbnail", str(p), str(out) + "[Q=88]", "2400"])
    if shutil.which("ffmpeg"):
        tools.append(["ffmpeg", "-y", "-i", str(p), "-frames:v", "1", "-vf", "scale='min(2400,iw)':-2", str(out)])
    if shutil.which("convert"):
        tools.append(["convert", str(p) + "[0]", "-auto-orient", "-resize", "2400x2400>", str(out)])
    for cmd in tools:
        try:
            run_quiet(cmd, capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if out.exists() and out.stat().st_size > 0:
            return out
    return None


def doc_preview(p: Path) -> Path | None:
    """Office docs → PDF via LibreOffice (if installed)."""
    out = cache_path(p, "docpdf", "pdf")
    if out.exists():
        return out
    lo = shutil.which("libreoffice") or shutil.which("soffice")
    if not lo:
        return None
    tmpdir = CACHE / f"lo-{secrets.token_hex(4)}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        run_quiet([lo, "--headless", "--convert-to", "pdf", "--outdir", str(tmpdir), str(p)],
                       capture_output=True, timeout=120)
        produced = next(tmpdir.glob("*.pdf"), None)
        if produced:
            shutil.move(str(produced), out)
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out if out.exists() else None


def pdf_thumb(p: Path) -> Path | None:
    out = cache_path(p, "pdfthumb", "png")
    if out.exists():
        return out
    if shutil.which("pdftoppm"):
        base = str(out)[:-4]
        run_quiet(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", "1", "-singlefile", str(p), base],
                       capture_output=True, timeout=30)
    return out if out.exists() else None


def image_thumb(p: Path) -> Path | None:
    """A right-sized JPEG for decks and grids; never a full-resolution original."""
    k = kind_of(p)
    if k == "image":
        if p.suffix.lower() == ".svg":
            return p  # vector, no decode cost
        dims = image_size(p)
        if dims and max(dims) <= 480:
            return p  # already within the target bounds
    out = cache_path(p, "ithumb", "jpg")
    if out.exists() and out.stat().st_size:
        return out
    src = convert_image(p) if k == "image-convert" else p
    if src is None:
        return None
    if src != p:
        dims = image_size(src)
        if dims and max(dims) <= 480:
            return src
    for cmd in (["vips", "thumbnail", str(src), str(out) + "[Q=80]", "480"],
                ["magick", str(src) + "[0]", "-auto-orient", "-thumbnail", "480x480>", "-quality", "80", str(out)],
                ["ffmpeg", "-y", "-i", str(src), "-frames:v", "1", "-vf", "scale='min(480,iw)':-2", str(out)]):
        if not shutil.which(cmd[0]):
            continue
        try:
            run_quiet(cmd, capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if out.exists() and out.stat().st_size:
            return out
    return None


def pdf_source(p: Path) -> Path | None:
    k = kind_of(p)
    return p if k == "pdf" else doc_preview(p) if k == "doc" else None


def pdf_pages(p: Path) -> int:
    if not shutil.which("pdfinfo"):
        return 0
    try:
        r = run_quiet(["pdfinfo", str(p)], capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return 0
    for line in r.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    return 0


def pdf_page(p: Path, n: int) -> Path | None:
    out = cache_path(p, f"page{n}", "png")
    if out.exists():
        return out
    if shutil.which("pdftoppm"):
        run_quiet(["pdftoppm", "-png", "-scale-to", "1600", "-f", str(n), "-l", str(n), "-singlefile",
                        str(p), str(out)[:-4]], capture_output=True, timeout=60)
    return out if out.exists() else None


def archive_listing(p: Path) -> str:
    import tarfile
    import zipfile
    try:
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
        elif tarfile.is_tarfile(p):
            with tarfile.open(p) as t:
                names = t.getnames()
        else:
            return ""
    except Exception:
        return ""
    more = f"\n… and {len(names) - 200} more" if len(names) > 200 else ""
    return "\n".join(names[:200]) + more


def media_info(p: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    try:
        r = run_quiet(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {}
    info = {}
    fmt = d.get("format", {})
    if "duration" in fmt:
        info["duration"] = float(fmt["duration"])
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and "width" in s:
            info["resolution"] = f"{s['width']}×{s['height']}"
            break
    tags = fmt.get("tags", {})
    for k in ("title", "artist", "album", "creation_time"):
        if k in tags:
            info[k] = tags[k]
    return info


# ------------------------------------------------------------- folders ----

def list_dirs(path: Path, include_hidden: bool) -> dict:
    dirs = []
    try:
        for e in os.scandir(path):
            try:
                if e.is_dir() and (include_hidden or not is_hidden(Path(e.path), e.name)):
                    dirs.append({"name": e.name, "path": str(Path(e.path))})
            except OSError:
                continue
    except OSError as err:
        return {"path": str(path), "parent": str(path.parent), "dirs": [], "error": str(err)}
    dirs.sort(key=lambda d: d["name"].lower())
    home = Path.home()
    places = [{"name": "Home", "path": str(home)}]
    for name in ("Pictures", "Videos", "Music", "Documents", "Downloads", "Desktop"):
        if (home / name).is_dir():
            places.append({"name": name, "path": str(home / name)})
    if IS_WIN:
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            if os.path.isdir(f"{letter}:\\"):
                places.append({"name": f"{letter}: drive", "path": f"{letter}:\\"})
    else:
        for media in ("/run/media/" + os.environ.get("USER", ""), "/media", "/mnt"):
            try:
                for e in os.scandir(media):
                    if e.is_dir():
                        places.append({"name": e.name, "path": e.path})
            except OSError:
                pass
    recent = [{"name": Path(r).name or r, "path": r} for r in load_recent()[:6]]
    return {"path": str(path), "parent": str(path.parent) if path != path.parent else None,
            "dirs": dirs, "places": places, "recent": recent}


GTK_PICKER = None  # set when running in a native GTK window


def native_pick(start: Path) -> str | None:
    """Ask the desktop for a folder: GTK in native mode, else zenity / kdialog / yad."""
    if GTK_PICKER:
        return GTK_PICKER(start)
    if IS_WIN:
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              "$d.Description = 'Pick a folder to sort'; $d.UseDescriptionForTitle = $true;"
              f"$d.SelectedPath = '{str(start)}';"
              "if ($d.ShowDialog($o) -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }")
        r = run_quiet(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True)
        out = r.stdout.strip()
        return out or None
    cmds = []
    if shutil.which("kdialog") and os.environ.get("XDG_CURRENT_DESKTOP", "").upper().find("KDE") >= 0:
        cmds.append(["kdialog", "--getexistingdirectory", str(start)])
    if shutil.which("zenity"):
        cmds.append(["zenity", "--file-selection", "--directory", "--title=SIFT: pick a folder",
                     f"--filename={start}/"])
    if shutil.which("kdialog"):
        cmds.append(["kdialog", "--getexistingdirectory", str(start)])
    if shutil.which("yad"):
        cmds.append(["yad", "--file", "--directory", f"--filename={start}/"])
    for cmd in cmds[:1]:
        r = run_quiet(cmd, capture_output=True, text=True)
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None
    raise FileNotFoundError("no zenity/kdialog/yad")


# -------------------------------------------------------------- server -----

LAST_PING = [time.monotonic()]
OPEN_ROUTES = {"/api/files", "/api/dirs", "/api/ping", "/api/root", "/api/pick"}


class Handler(BaseHTTPRequestHandler):
    session: Session | None = None
    token: str
    include_hidden = False
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet unless debugging
        if os.environ.get("PHOTTO_DEBUG"):
            sys.stderr.write("SIFT: " + (fmt % args) + "\n")

    # helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_err(self, code, msg=""):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self, q, route: str = "") -> bool:
        tok = self.headers.get("X-Token") or q.get("t", [""])[0]
        if not secrets.compare_digest(tok, self.token):
            self.send_err(403, "bad token")
            return False
        LAST_PING[0] = time.monotonic()
        if self.session is None and route not in OPEN_ROUTES:
            self.send_err(409, "no folder open")
            return False
        return True

    def send_file(self, path: Path, ctype: str | None = None, cache: str = "private, max-age=300"):
        ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def target(self, q) -> Path | None:
        rel = q.get("p", [""])[0]
        p = self.session.resolve(rel)
        if p is None or not p.is_file():
            self.send_err(404, "not found")
            return None
        return p

    # routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        route = u.path

        if route in ("/", "/index.html"):
            return self.send_file(STATIC / "index.html", "text/html; charset=utf-8", "no-cache")
        if route.startswith("/static/"):
            p = (STATIC / route[len("/static/"):]).resolve()
            if STATIC in p.parents and p.is_file():
                return self.send_file(p, cache="no-cache")
            return self.send_err(404)

        if not self.authed(q, route):
            return

        if route == "/api/ping":
            return self.send_json({"ok": True})
        if route == "/api/files":
            if self.session is None:
                return self.send_json({"root": None, "needsPick": True, "files": [], "recent": load_recent(),
                                       "platform": "windows" if IS_WIN else "linux"})
            if q.get("refresh", ["0"])[0] == "1":
                self.session.start_scan(refresh=True)
            try:
                limit = int(q.get("limit", [str(DEFAULT_PAGE_SIZE)])[0])
            except ValueError:
                limit = DEFAULT_PAGE_SIZE
            try:
                offset = int(q.get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            page = self.session.page(
                cursor=q.get("cursor", [""])[0], offset=offset, limit=limit,
                sort=q.get("sort", ["shuffle"])[0], group=q.get("group", ["all"])[0],
                decision=q.get("decision", ["none"])[0],
            )
            payload = {
                "root": str(self.session.root),
                "name": self.session.root.name or str(self.session.root),
                "recursive": self.session.recursive,
                "version": VERSION,
                "platform": "windows" if IS_WIN else "linux",
            }
            payload.update(page)
            progress = self.session.scan_payload()
            payload.update(progress)
            payload["total"] = progress["counts"]["total"]
            return self.send_json(payload)
        if route == "/api/scan":
            payload = {
                "root": str(self.session.root),
                "name": self.session.root.name or str(self.session.root),
                "recursive": self.session.recursive,
                "version": VERSION,
                "platform": "windows" if IS_WIN else "linux",
            }
            payload.update(self.session.scan_payload())
            return self.send_json(payload)
        if route == "/api/text/status":
            status = self.session.text_jobs.status(q.get("jobId", [""])[0])
            return self.send_json(status if status is not None else {}, 404 if status is None else 200)
        if route == "/api/text/results":
            page = self.session.text_jobs.results(q.get("jobId", [""])[0], q.get("offset", [0])[0], q.get("limit", [50])[0])
            return self.send_json(page if page is not None else {}, 404 if page is None else 200)
        if route == "/api/dirs":
            raw = q.get("path", [""])[0]
            fallback = self.session.root if self.session else Path.home()
            path = Path(raw).expanduser().resolve() if raw else fallback
            if not path.is_dir():
                path = fallback
            return self.send_json(list_dirs(path, self.include_hidden))
        if route == "/pdfinfo":
            p = self.target(q)
            if not p:
                return
            src = pdf_source(p)
            return self.send_json({"pages": pdf_pages(src) if src else 0})
        if route == "/pdfpage":
            p = self.target(q)
            if not p:
                return
            src = pdf_source(p)
            try:
                n = max(1, int(q.get("n", ["1"])[0]))
            except ValueError:
                n = 1
            out = pdf_page(src, n) if src else None
            return self.send_file(out, "image/png") if out else self.send_err(404)
        if route == "/raw":
            p = self.target(q)
            if p:
                self.send_file(p)
            return
        if route == "/view":
            # browser-displayable version of the file
            p = self.target(q)
            if not p:
                return
            k = kind_of(p)
            if k == "image-convert":
                out = convert_image(p)
                return self.send_file(out, "image/jpeg") if out else self.send_err(415, "no converter")
            if k == "doc":
                out = doc_preview(p)
                return self.send_file(out, "application/pdf") if out else self.send_err(415, "no converter")
            return self.send_file(p)
        if route == "/thumb":
            p = self.target(q)
            if not p:
                return
            versioned = bool(q.get("v", [""])[0])
            k = kind_of(p)
            with THUMB_SEMAPHORE:
                if k == "video":
                    out = video_thumb(p)
                elif k == "pdf":
                    out = pdf_thumb(p)
                elif k in ("image-convert", "image"):
                    out = image_thumb(p)
                else:
                    out = None
            cache = "private, max-age=31536000, immutable" if versioned else "private, max-age=300"
            return self.send_file(out, cache=cache) if out else self.send_err(404)
        if route == "/text":
            p = self.target(q)
            if not p:
                return
            if kind_of(p) == "archive":
                return self.send_json({"text": archive_listing(p), "truncated": False})
            with open(p, "rb") as f:
                data = f.read(200_000)
            return self.send_json({"text": data.decode("utf-8", "replace"),
                                   "truncated": p.stat().st_size > 200_000})
        if route == "/info":
            p = self.target(q)
            if not p:
                return
            return self.send_json(media_info(p))
        return self.send_err(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self.authed(q, u.path):
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send_err(400, "bad json")
        s = self.session

        if u.path == "/api/decide":
            rel, d = body.get("path"), body.get("decision")
            if not rel or s.resolve(rel) is None:
                return self.send_err(400)
            s.set_decision(rel, d if d in ("keep", "delete", "favorite") else None)
            return self.send_json({"ok": True})

        if u.path == "/api/text/start":
            try:
                opts = {"language": str(body.get("language", "eng"))[:32],
                        "threshold": max(0, min(100, int(body.get("threshold", 2)))),
                        "query": str(body.get("query", ""))[:200],
                        "images": bool(body.get("images", True)), "videos": bool(body.get("videos", True))}
                return self.send_json(s.text_jobs.start(**opts))
            except Exception as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/text/cancel":
            return self.send_json({"ok": s.text_jobs.cancel(str(body.get("jobId", "")))})
        if u.path == "/api/text/clear":
            s.text_jobs.clear()
            return self.send_json({"ok": True})
        if u.path == "/api/text/preview":
            job = s.text_jobs.jobs.get(str(body.get("jobId", "")))
            allowed = {m["path"] for m in job["matches"]} if job else set()
            destinations = []
            for rel in body.get("paths", []):
                if rel not in allowed: continue
                base = s.root / "SIFT_docs" / Path(rel)
                dest, n = base, 1
                while dest.exists():
                    dest = base.with_name(f"{base.stem}_sifted{n}{base.suffix}"); n += 1
                destinations.append({"path": rel, "destination": dest.relative_to(s.root).as_posix()})
            return self.send_json({"destinations": destinations})
        if u.path == "/api/text/action":
            try:
                results = s.text_jobs.action(str(body.get("jobId", "")), body.get("paths", []), str(body.get("action", "")))
                return self.send_json({"results": results, "copied": sum(x["ok"] for x in results) if body.get("action") == "copy" else 0,
                                       "moved": sum(x["ok"] for x in results) if body.get("action") == "move" else 0,
                                       "failed": sum(not x["ok"] for x in results)})
            except ValueError as e:
                return self.send_err(400, str(e))

        if u.path == "/api/root":
            path = Path(body.get("path") or (s.root if s else Path.home())).expanduser().resolve()
            if not path.is_dir():
                return self.send_err(400, "not a directory")
            old = Handler.session
            Handler.session = Session(path, bool(body.get("recursive", True)), self.include_hidden)
            if old is not None:
                old.close()
            return self.send_json({"ok": True, "root": str(path)})

        if u.path == "/api/pick":
            try:
                picked = native_pick(Path(body.get("start") or (s.root if s else Path.home())))
            except FileNotFoundError:
                return self.send_json({"error": "no-dialog"})
            return self.send_json({"path": picked})

        if u.path == "/api/reset":
            s.reset_decisions()
            return self.send_json({"ok": True})

        if u.path == "/api/commit":
            # Trash everything marked delete; optionally move favorites into a folder.
            results = {"trashed": [], "failed": [], "favorited": [], "freed": 0}
            gone: list[str] = []
            with s.lock:
                for rel, d in list(s.decisions.items()):
                    if d != "delete":
                        continue
                    p = s.resolve(rel)
                    if p is None or not p.is_file():
                        s.decisions.pop(rel, None)
                        gone.append(rel)
                        continue
                    size = p.stat().st_size
                    try:
                        trash(p)
                        results["trashed"].append(rel)
                        results["freed"] += size
                        s.decisions.pop(rel, None)
                    except Exception as e:  # noqa: BLE001
                        results["failed"].append({"path": rel, "error": str(e)})
                fav_dir = body.get("favoritesFolder")
                if fav_dir:
                    dest = s.resolve(fav_dir)
                    if dest is not None:
                        dest.mkdir(parents=True, exist_ok=True)
                        for rel, d in list(s.decisions.items()):
                            if d != "favorite":
                                continue
                            p = s.resolve(rel)
                            if p is None or not p.is_file() or p.parent == dest:
                                continue
                            target = dest / p.name
                            i = 1
                            while target.exists():
                                target = dest / f"{p.stem} ({i}){p.suffix}"
                                i += 1
                            shutil.move(str(p), target)
                            new_rel = target.relative_to(s.root).as_posix()
                            s.decisions.pop(rel, None)
                            s.decisions[new_rel] = "favorite"
                            gone.append(rel)
                            results["favorited"].append(new_rel)
                s.save()
            s.index.remove_rows(results["trashed"] + gone)
            s.sync_index_decisions()
            if results["favorited"]:
                s.start_scan(refresh=True)
            results["freedH"] = human(results["freed"])
            return self.send_json(results)

        if u.path == "/api/open":
            p = s.resolve(body.get("path", ""))
            if p is None or not p.exists():
                return self.send_err(404)
            open_path(p, reveal=bool(body.get("folder")))
            return self.send_json({"ok": True})

        return self.send_err(404)


CHROMIUMS = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave", "brave-browser",
             "brave-browser-stable", "brave-origin", "microsoft-edge-stable", "vivaldi-stable", "thorium-browser",
             "helium-browser")


def have_webkit() -> bool:
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        for v in ("4.1", "4.0"):
            try:
                gi.require_version("WebKit2", v)
                break
            except ValueError:
                continue
        else:
            return False
        from gi.repository import Gtk, WebKit2  # noqa: F401
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    except Exception:
        return False


def run_native(url: str, srv: ThreadingHTTPServer) -> None:
    """Show the UI in a real GTK window. Closing it quits SIFT."""
    global GTK_PICKER
    from gi.repository import GLib
    GLib.set_prgname("sift")
    GLib.set_application_name("SIFT")
    from gi.repository import Gdk, Gtk, WebKit2

    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    win = Gtk.Window(title="SIFT")
    win.set_default_size(1280, 880)
    win.set_icon_name("sift")

    view = WebKit2.WebView()
    st = view.get_settings()
    st.set_media_playback_requires_user_gesture(False)
    st.set_enable_developer_extras(bool(os.environ.get("PHOTTO_DEBUG")))
    st.set_javascript_can_access_clipboard(False)
    view.set_background_color(Gdk.RGBA(0.07, 0.063, 0.055, 1))
    if not os.environ.get("PHOTTO_DEBUG"):
        view.connect("context-menu", lambda *a: True)

    def on_decide_policy(_view, decision, kind):
        # keep navigation inside the app; open anything else externally
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith(url.split("#")[0]):
                decision.ignore()
                subprocess.Popen(["xdg-open", uri], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
        return False
    view.connect("decide-policy", on_decide_policy)
    view.load_uri(url)
    win.add(view)

    def pick(start: Path) -> str | None:
        done, result = threading.Event(), [None]

        def show():
            dlg = Gtk.FileChooserNative.new("Pick a folder to sort", win, Gtk.FileChooserAction.SELECT_FOLDER,
                                            "Open", "Cancel")
            dlg.set_current_folder(str(start))
            if dlg.run() == Gtk.ResponseType.ACCEPT:
                result[0] = dlg.get_filename()
            dlg.destroy()
            done.set()
            return False
        GLib.idle_add(show)
        done.wait()
        return result[0]
    GTK_PICKER = pick

    def quit_all(*_a):
        threading.Thread(target=srv.shutdown, daemon=True).start()
        Gtk.main_quit()
    win.connect("destroy", quit_all)
    win.show_all()
    Gtk.main()


def windows_browsers() -> list[str]:
    roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe", r"Chromium\Application\chrome.exe"]
    return [str(Path(r) / rel) for rel in rels for r in roots if r and (Path(r) / rel).is_file()]


def open_app_window(url: str, srv: ThreadingHTTPServer | None = None) -> bool:
    """A chrome-less Chromium-family app window. Returns False if none is installed."""
    if IS_WIN:
        exes = windows_browsers()
        if not exes:
            return False
        # A private profile makes the window its own process, so we know when it closes.
        profile = CACHE.parent / "window"
        proc = subprocess.Popen([exes[0], f"--app={url}", "--window-size=1280,880", f"--user-data-dir={profile}",
                                 "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])
        if srv is not None:
            def wait_and_quit():
                proc.wait()
                if time.monotonic() - LAST_PING[0] > 3:  # window really gone, not handed off
                    srv.shutdown()
            threading.Thread(target=wait_and_quit, daemon=True).start()
        return True
    for b in CHROMIUMS:
        exe = shutil.which(b)
        if exe:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1280,880", "--class=sift"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
    return False


def watchdog(srv: ThreadingHTTPServer, idle: float) -> None:
    """Quit once the UI stops pinging (its window/tab was closed)."""
    while True:
        time.sleep(5)
        if time.monotonic() - LAST_PING[0] > idle:
            srv.shutdown()
            return


def notify_error(msg: str) -> None:
    print(f"SIFT: {msg}", file=sys.stderr)
    if IS_WIN:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "SIFT", 0x10)
        return
    if not sys.stderr.isatty():
        for cmd in (["notify-send", "-a", "SIFT", "SIFT", msg], ["zenity", "--error", "--text", msg]):
            if shutil.which(cmd[0]):
                run_quiet(cmd, capture_output=True)
                break


def main() -> None:
    if sys.stdout is None:  # windowed .exe has no console
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    ap = argparse.ArgumentParser(prog="sift", description="Open SIFT to review and find text in local files.")
    ap.add_argument("dir", nargs="?", help="open this folder")
    ap.add_argument("-t", "--top-only", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("-r", "--recursive", action="store_true", help=argparse.SUPPRESS)  # default now
    ap.add_argument("-a", "--all", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("-p", "--port", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--window", choices=["auto", "native", "app", "browser", "none"], default="auto",
                    help=argparse.SUPPRESS)
    ap.add_argument("--browser", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--token", help=argparse.SUPPRESS)  # fixed token, for automated tests
    ap.add_argument("--version", action="version", version=f"SIFT {VERSION}")
    args, unknown = ap.parse_known_args()
    legacy_scan_flags = {"--video", "--workers", "--lang", "--threshold", "--copy", "--dry-run", "--search", "--json"}
    if any(flag.split("=", 1)[0] in legacy_scan_flags for flag in unknown):
        notify_error("OCR scanning is now available in the SIFT app. Open SIFT to find text; no files were changed.")
        sys.exit(2)
    if unknown:
        ap.error("unrecognized arguments: " + " ".join(unknown))
    if FROZEN and not args.dir:
        args.gui = True  # double-clicked .exe / Start Menu
    mode = "browser" if args.browser else "none" if args.no_browser else args.window

    root: Path | None = None
    if args.dir:
        root = Path(args.dir).expanduser().resolve()
        if not root.is_dir():
            notify_error(f"not a directory: {root}")
            sys.exit(1)

    Handler.include_hidden = args.all
    Handler.session = Session(root, not args.top_only, args.all) if root else None
    Handler.token = args.token or secrets.token_urlsafe(18)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{srv.server_address[1]}/#t={Handler.token}"

    if mode == "auto" and IS_WIN:
        mode = "app"
    if mode == "auto":
        mode = "native" if have_webkit() else "app" if any(shutil.which(b) for b in CHROMIUMS) else "browser"
    if mode == "native" and not have_webkit():
        notify_error("native window needs python-gobject + webkit2gtk; falling back to browser")
        mode = "browser"

    if sys.stdout.isatty() or mode == "none":
        print(f"\n  \033[1mSIFT\033[0m  ·  {root or 'pick a folder'}")
        print(f"  {url}")
        print("  close the window or press Ctrl+C to quit\n", flush=True)

    if mode == "native":
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            run_native(url, srv)
        except KeyboardInterrupt:
            pass
        srv.server_close()
        return

    if mode == "app" and not open_app_window(url, srv):
        mode = "browser"
    if mode == "browser":
        webbrowser.open(url)
    if mode in ("app", "browser"):
        # browsers throttle timers in background tabs, so be generous
        threading.Thread(target=watchdog, args=(srv, 45 if mode == "app" else 180), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
