#!/usr/bin/env python3
"""photto — swipe through any folder like Tinder: keep, favorite, or trash.

Usage:  photto [DIR]        sort DIR (default: current folder) and its subfolders
        photto              from the app menu: opens a folder picker

Runs a private local web app (127.0.0.1 only, per-run token) and shows it in a
native window (WebKitGTK), a browser app window, or your default browser.
Nothing is deleted until you confirm in Review, and even then files go to the
freedesktop Trash, so they can be restored.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0.0"
MAX_FILES = 100_000
APP_DIR = Path(__file__).resolve().parent
STATIC = APP_DIR / "static"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "photto"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "photto"

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

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".cache", ".Trash", ".Trash-1000"}


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

def trash(path: Path) -> None:
    """Move to the freedesktop Trash. Prefer gio, fall back to the spec."""
    if shutil.which("gio"):
        r = subprocess.run(["gio", "trash", "--", str(path)], capture_output=True, text=True)
        if r.returncode == 0:
            return
    if shutil.which("kioclient"):
        r = subprocess.run(["kioclient", "move", str(path), "trash:/"], capture_output=True, text=True)
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

RECENT_FILE = STATE_DIR / "recent.json"


def load_recent() -> list[str]:
    try:
        return [p for p in json.loads(RECENT_FILE.read_text()) if Path(p).is_dir()]
    except (OSError, ValueError):
        return []


def add_recent(path: Path) -> None:
    items = [str(path)] + [p for p in load_recent() if p != str(path)]
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RECENT_FILE.write_text(json.dumps(items[:10]))
    except OSError:
        pass


class Session:
    def __init__(self, root: Path, recursive: bool, include_hidden: bool):
        add_recent(root)
        self.truncated = False
        self.root = root
        self.recursive = recursive
        self.include_hidden = include_hidden
        key = hashlib.sha1(str(root).encode()).hexdigest()[:16]
        self.state_file = STATE_DIR / f"{key}.json"
        self.lock = threading.Lock()
        self.decisions: dict[str, str] = {}  # relpath -> keep|delete|favorite
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self.decisions = data.get("decisions", {})
            except (OSError, ValueError):
                pass

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

    def iter_files(self):
        if not self.recursive:
            try:
                yield from (p for p in self.root.iterdir() if self.include_hidden or not p.name.startswith("."))
            except OSError:
                return
            return
        for dirpath, dirnames, filenames in os.walk(self.root, onerror=lambda e: None):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS and (self.include_hidden or not d.startswith(".")))
            for name in filenames:
                if self.include_hidden or not name.startswith("."):
                    yield Path(dirpath) / name

    def scan(self) -> list[dict]:
        out = []
        self.truncated = False
        for p in self.iter_files():
            if len(out) >= MAX_FILES:
                self.truncated = True
                break
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
                kind = kind_of(p)
            except OSError:
                continue
            rel = str(p.relative_to(self.root))
            out.append({
                "path": rel,
                "name": p.name,
                "dir": str(Path(rel).parent) if Path(rel).parent != Path(".") else "",
                "ext": p.suffix.lower().lstrip("."),
                "kind": kind,
                "size": st.st_size,
                "sizeH": human(st.st_size),
                "mtime": st.st_mtime,
                "decision": self.decisions.get(rel),
            })
        # Forget marks only for files that are really gone (not just hidden by the current scope).
        with self.lock:
            stale = [k for k in self.decisions if not (self.root / k).is_file()]
            for k in stale:
                del self.decisions[k]
            if stale:
                self.save()
        return out


# ------------------------------------------------------------- previews ----

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
        subprocess.run(["ffmpegthumbnailer", "-i", str(p), "-o", str(out), "-s", "640", "-q", "8"],
                       capture_output=True, timeout=30)
    elif shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(p), "-frames:v", "1", "-vf", "scale=640:-2", str(out)],
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
            subprocess.run(cmd, capture_output=True, timeout=60)
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
        subprocess.run([lo, "--headless", "--convert-to", "pdf", "--outdir", str(tmpdir), str(p)],
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
        subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", "1", "-singlefile", str(p), base],
                       capture_output=True, timeout=30)
    return out if out.exists() else None


def image_thumb(p: Path) -> Path | None:
    """Small JPEG for grids. Small web images are served as-is."""
    if kind_of(p) == "image" and (p.stat().st_size < 1_500_000 or p.suffix.lower() in (".svg", ".gif")):
        return p
    out = cache_path(p, "ithumb", "jpg")
    if out.exists():
        return out
    src = convert_image(p) if kind_of(p) == "image-convert" else p
    if src is None:
        return None
    for cmd in (["vips", "thumbnail", str(src), str(out) + "[Q=80]", "480"],
                ["magick", str(src) + "[0]", "-auto-orient", "-thumbnail", "480x480>", "-quality", "80", str(out)],
                ["ffmpeg", "-y", "-i", str(src), "-frames:v", "1", "-vf", "scale='min(480,iw)':-2", str(out)]):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if out.exists() and out.stat().st_size:
            return out
    return src


def pdf_source(p: Path) -> Path | None:
    k = kind_of(p)
    return p if k == "pdf" else doc_preview(p) if k == "doc" else None


def pdf_pages(p: Path) -> int:
    if not shutil.which("pdfinfo"):
        return 0
    try:
        r = subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True, timeout=20)
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
        subprocess.run(["pdftoppm", "-png", "-scale-to", "1600", "-f", str(n), "-l", str(n), "-singlefile",
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
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(p)],
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
                if e.is_dir() and (include_hidden or not e.name.startswith(".")):
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
    cmds = []
    if shutil.which("kdialog") and os.environ.get("XDG_CURRENT_DESKTOP", "").upper().find("KDE") >= 0:
        cmds.append(["kdialog", "--getexistingdirectory", str(start)])
    if shutil.which("zenity"):
        cmds.append(["zenity", "--file-selection", "--directory", "--title=photto: pick a folder",
                     f"--filename={start}/"])
    if shutil.which("kdialog"):
        cmds.append(["kdialog", "--getexistingdirectory", str(start)])
    if shutil.which("yad"):
        cmds.append(["yad", "--file", "--directory", f"--filename={start}/"])
    for cmd in cmds[:1]:
        r = subprocess.run(cmd, capture_output=True, text=True)
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
            sys.stderr.write("photto: " + (fmt % args) + "\n")

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
                return self.send_json({"root": None, "needsPick": True, "files": [], "recent": load_recent()})
            files = self.session.scan()
            return self.send_json({
                "root": str(self.session.root),
                "name": self.session.root.name or str(self.session.root),
                "recursive": self.session.recursive,
                "truncated": self.session.truncated,
                "files": files,
                "version": VERSION,
            })
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
            k = kind_of(p)
            out = None
            if k == "video":
                out = video_thumb(p)
            elif k == "pdf":
                out = pdf_thumb(p)
            elif k == "image-convert":
                out = image_thumb(p)
            elif k == "image":
                out = image_thumb(p)
            return self.send_file(out) if out else self.send_err(404)
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
            with s.lock:
                if d in ("keep", "delete", "favorite"):
                    s.decisions[rel] = d
                else:
                    s.decisions.pop(rel, None)
                s.save()
            return self.send_json({"ok": True})

        if u.path == "/api/root":
            path = Path(body.get("path") or (s.root if s else Path.home())).expanduser().resolve()
            if not path.is_dir():
                return self.send_err(400, "not a directory")
            Handler.session = Session(path, bool(body.get("recursive", True)), self.include_hidden)
            return self.send_json({"ok": True, "root": str(path)})

        if u.path == "/api/pick":
            try:
                picked = native_pick(Path(body.get("start") or (s.root if s else Path.home())))
            except FileNotFoundError:
                return self.send_json({"error": "no-dialog"})
            return self.send_json({"path": picked})

        if u.path == "/api/reset":
            with s.lock:
                s.decisions.clear()
                s.save()
            return self.send_json({"ok": True})

        if u.path == "/api/commit":
            # Trash everything marked delete; optionally move favorites into a folder.
            results = {"trashed": [], "failed": [], "favorited": [], "freed": 0}
            with s.lock:
                for rel, d in list(s.decisions.items()):
                    if d != "delete":
                        continue
                    p = s.resolve(rel)
                    if p is None or not p.is_file():
                        s.decisions.pop(rel, None)
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
                            new_rel = str(target.relative_to(s.root))
                            s.decisions.pop(rel, None)
                            s.decisions[new_rel] = "favorite"
                            results["favorited"].append(new_rel)
                s.save()
            results["freedH"] = human(results["freed"])
            return self.send_json(results)

        if u.path == "/api/open":
            p = s.resolve(body.get("path", ""))
            if p is None or not p.exists():
                return self.send_err(404)
            target = p.parent if body.get("folder") else p
            subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
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
    """Show the UI in a real GTK window. Closing it quits photto."""
    global GTK_PICKER
    from gi.repository import GLib
    GLib.set_prgname("photto")
    GLib.set_application_name("photto")
    from gi.repository import Gdk, Gtk, WebKit2

    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    win = Gtk.Window(title="photto")
    win.set_default_size(1280, 880)
    win.set_icon_name("photto")

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


def open_app_window(url: str) -> bool:
    """A chrome-less Chromium-family app window. Returns False if none is installed."""
    for b in CHROMIUMS:
        exe = shutil.which(b)
        if exe:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1280,880", "--class=photto"],
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
    print(f"photto: {msg}", file=sys.stderr)
    if not sys.stderr.isatty():
        for cmd in (["notify-send", "-a", "photto", "photto", msg], ["zenity", "--error", "--text", msg]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, capture_output=True)
                break


def main() -> None:
    ap = argparse.ArgumentParser(prog="photto", description="Swipe through files: keep, favorite or trash.")
    ap.add_argument("dir", nargs="?", help="folder to sort (default: current folder; from the app menu: pick one)")
    ap.add_argument("-t", "--top-only", action="store_true", help="only this folder, not its subfolders")
    ap.add_argument("-r", "--recursive", action="store_true", help=argparse.SUPPRESS)  # default now
    ap.add_argument("-a", "--all", action="store_true", help="include hidden files and folders")
    ap.add_argument("-p", "--port", type=int, default=0, help="port (default: random free port)")
    ap.add_argument("--gui", action="store_true",
                    help="launched from the desktop: with no DIR, start with the folder picker")
    ap.add_argument("--window", choices=["auto", "native", "app", "browser", "none"], default="auto",
                    help="how to show the UI (default: native GTK window, else browser app window, else browser)")
    ap.add_argument("--browser", action="store_true", help="same as --window browser")
    ap.add_argument("--no-browser", action="store_true", help="same as --window none (just print the URL)")
    ap.add_argument("--version", action="version", version=f"photto {VERSION}")
    args = ap.parse_args()
    mode = "browser" if args.browser else "none" if args.no_browser else args.window

    root: Path | None = None
    if args.dir:
        root = Path(args.dir).expanduser().resolve()
        if not root.is_dir():
            notify_error(f"not a directory: {root}")
            sys.exit(1)
    elif not args.gui:
        root = Path.cwd()

    Handler.include_hidden = args.all
    Handler.session = Session(root, not args.top_only, args.all) if root else None
    Handler.token = secrets.token_urlsafe(18)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{srv.server_address[1]}/#t={Handler.token}"

    if mode == "auto":
        mode = "native" if have_webkit() else "app" if any(shutil.which(b) for b in CHROMIUMS) else "browser"
    if mode == "native" and not have_webkit():
        notify_error("native window needs python-gobject + webkit2gtk; falling back to browser")
        mode = "browser"

    if sys.stdout.isatty() or mode == "none":
        print(f"\n  \033[1mphotto\033[0m  ·  {root or 'pick a folder'}")
        print(f"  {url}")
        print("  close the window or press Ctrl+C to quit\n")

    if mode == "native":
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            run_native(url, srv)
        except KeyboardInterrupt:
            pass
        srv.server_close()
        return

    if mode == "app" and not open_app_window(url):
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
