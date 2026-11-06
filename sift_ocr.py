"""Local OCR primitives shared by the SIFT desktop backend."""
from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import tempfile
import threading
import sqlite3
import uuid
import shutil as _shutil
import concurrent.futures
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".3gp", ".webm"}
_slots = threading.BoundedSemaphore(max(1, min(4, (os.cpu_count() or 2) // 2)))


class EngineUnavailable(RuntimeError):
    pass


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def meaningful_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]+", text, flags=re.UNICODE) if len(w) >= 3]


def match_reason(text: str, threshold: int = 2, query: str = "") -> str | None:
    if query.strip():
        return "query" if normalize(query) in normalize(text) else None
    if len(meaningful_words(text)) >= max(0, int(threshold)):
        return "threshold"
    return None


def tesseract_version() -> str:
    exe = shutil.which("tesseract")
    if not exe:
        raise EngineUnavailable("Tesseract is not installed. Install Tesseract to find text in images.")
    result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
    return result.stdout.splitlines()[0] if result.stdout else "tesseract-unknown"


def recognize_image(path: Path, language: str = "eng") -> str:
    exe = shutil.which("tesseract")
    if not exe:
        raise EngineUnavailable("Tesseract is not installed. Install Tesseract to find text in images.")
    with _slots:
        try:
            result = subprocess.run([exe, str(path), "stdout", "--psm", "3", "-l", language],
                                    capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("OCR timed out") from exc
    if result.returncode:
        raise RuntimeError((result.stderr.strip() or "Tesseract could not read this image")[:300])
    return result.stdout


def sample_interval(duration: float) -> int:
    return 1 if duration < 15 else 2 if duration < 60 else 5


def sample_video(path: Path, language: str = "eng", cancel: threading.Event | None = None):
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise EngineUnavailable("FFmpeg and FFprobe are required for video text search; image search is still available.")
    p = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
                        "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, timeout=30)
    try:
        duration = float(p.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("FFprobe could not read this video") from exc
    if p.returncode or duration <= 0:
        raise RuntimeError("FFprobe could not read this video")
    interval = sample_interval(duration)
    with tempfile.TemporaryDirectory(prefix="sift-frames-") as tmp:
        decoded = 0
        for second in range(0, max(1, int(duration)), interval):
            if cancel and cancel.is_set():
                break
            frame = Path(tmp) / f"frame-{second}.png"
            result = subprocess.run([ffmpeg, "-v", "error", "-ss", str(second), "-i", str(path),
                                     "-frames:v", "1", "-y", str(frame)], capture_output=True, timeout=30)
            if result.returncode or not frame.exists():
                continue
            decoded += 1
            yield second, recognize_image(frame, language)
        if decoded == 0 and not (cancel and cancel.is_set()):
            raise RuntimeError("FFmpeg could not decode any video frames")


class TextJobs:
    """Per-folder, cancellable OCR jobs with a separate disposable result cache."""
    def __init__(self, root: Path, cache_path: Path, recursive: bool = True, include_hidden: bool = False):
        self.root = root.resolve()
        self.recursive = recursive
        self.include_hidden = include_hidden
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cache_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS ocr_cache (path TEXT,size INTEGER,mtime REAL,language TEXT,engine TEXT,sampling TEXT,text TEXT,words INTEGER,PRIMARY KEY(path,size,mtime,language,engine,sampling))")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(ocr_cache)")}
        if "samples" not in columns:
            self.db.execute("ALTER TABLE ocr_cache ADD COLUMN samples TEXT NOT NULL DEFAULT '[]'")
        self.db.commit()
        self.lock = threading.RLock()
        self.jobs = {}
        self.generation = 0
        self.closed = False

    def clear(self):
        with self.lock:
            self.db.execute("DELETE FROM ocr_cache"); self.db.commit()

    def start(self, *, language="eng", threshold=2, query="", images=True, videos=True):
        job_id, cancel = uuid.uuid4().hex, threading.Event()
        files = []
        for base, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if d != "SIFT_docs" and (self.include_hidden or not d.startswith("."))]
            if not self.recursive: dirs[:] = []
            for name in names:
                if not self.include_hidden and name.startswith("."): continue
                p = Path(base) / name
                ext = p.suffix.lower()
                if (images and ext in IMAGE_EXTENSIONS) or (videos and ext in VIDEO_EXTENSIONS):
                    files.append(p)
        with self.lock:
            if self.closed: raise RuntimeError("OCR session is closed")
            for prior in self.jobs.values():
                if prior["state"] == "running": prior["cancel"].set()
            self.generation += 1
            generation = self.generation
            job = {"id": job_id, "state": "running", "total": len(files), "done": 0,
                   "matches": [], "errors": [], "cacheHits": 0, "cancel": cancel, "generation": generation}
            self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job, files, language, int(threshold), query),
                         name="sift-ocr-" + job_id[:6], daemon=True).start()
        return {"jobId": job_id, "total": len(files)}

    def _run(self, job, files, language, threshold, query):
        try:
            engine = tesseract_version()
        except Exception as exc:
            with self.lock:
                job["errors"].append({"error": str(exc), "kind": "engine"})
                job["state"] = "error"
            return
        def one(path):
            rel = path.relative_to(self.root).as_posix()
            video = path.suffix.lower() in VIDEO_EXTENSIONS
            if job["cancel"].is_set(): return None
            try:
                st = path.stat()
                sampling = "1/2/5s" if video else "none"
                key = (rel, st.st_size, st.st_mtime, language, engine, sampling)
                with self.lock:
                    row = self.db.execute("SELECT samples FROM ocr_cache WHERE path=? AND size=? AND mtime=? AND language=? AND engine=? AND sampling=?", key).fetchone()
                    if row: job["cacheHits"] += 1
                texts = [(sec, txt) for sec, txt in json.loads(row[0])] if row else (list(sample_video(path, language, job["cancel"])) if video else [(None, recognize_image(path, language))])
                if not row:
                    text = "\n".join(t for _, t in texts)
                    with self.lock:
                        if job["generation"] == self.generation and not self.closed:
                            self.db.execute("INSERT OR REPLACE INTO ocr_cache(path,size,mtime,language,engine,sampling,text,words,samples) VALUES(?,?,?,?,?,?,?,?,?)", (*key, text, len(meaningful_words(text)), json.dumps(texts)))
                            self.db.commit()
                combined = "\n".join(t for _, t in texts)
                reason = match_reason(combined, threshold, query)
                if reason:
                    second = next((sec for sec, txt in texts if sec is not None and
                                   (match_reason(txt, threshold, query) or (reason == "threshold" and meaningful_words(txt)))), None)
                    return {"path": rel, "text": combined[:4000], "reason": reason, "timestamp": second}
            except Exception as exc:  # per-file diagnostics are visible to the caller
                return {"error": f"{rel}: {exc}", "kind": "decode" if video else "ocr"}
            return None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="sift-ocr") as pool:
                pending = set()
                it = iter(files)
                while len(pending) < 4 and not job["cancel"].is_set():
                    try: pending.add(pool.submit(one, next(it)))
                    except StopIteration: break
                while pending:
                    done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        result = future.result()
                        with self.lock:
                            if result and "error" in result: job["errors"].append(result)
                            elif result: job["matches"].append(result)
                            job["done"] += 1
                    while len(pending) < 4 and not job["cancel"].is_set():
                        try: pending.add(pool.submit(one, next(it)))
                        except StopIteration: break
            with self.lock:
                job["state"] = "canceled" if job["cancel"].is_set() else "done"
        except Exception as exc:
            with self.lock:
                job["errors"].append({"error": str(exc), "kind": "engine"}); job["state"] = "error"

    def status(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            if not j: return None
            return {"jobId": j["id"], "state": j["state"], "total": j["total"], "done": j["done"],
                    "matches": len(j["matches"]), "cacheHits": j["cacheHits"], "errors": j["errors"][-50:]}

    def results(self, job_id, offset=0, limit=50):
        with self.lock:
            j = self.jobs.get(job_id)
            if not j: return None
            offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
            return {"items": j["matches"][offset:offset+limit], "offset": offset, "limit": limit,
                    "total": len(j["matches"]), "nextOffset": offset + limit if offset + limit < len(j["matches"]) else None}

    def cancel(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            if not j: return False
            j["cancel"].set(); return True

    def action(self, job_id, paths, operation):
        if operation not in ("copy", "move"): raise ValueError("action must be copy or move")
        with self.lock:
            j = self.jobs.get(job_id)
            allowed = {m["path"] for m in j["matches"]} if j else set()
        result = []
        for rel in paths:
            if rel not in allowed: result.append({"path": rel, "ok": False, "error": "not a result"}); continue
            src = (self.root / rel).resolve()
            if not src.is_relative_to(self.root) or not src.is_file():
                result.append({"path": rel, "ok": False, "error": "source missing"}); continue
            dest_dir = self.root / "SIFT_docs" / Path(rel).parent
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            n = 1
            while dest.exists():
                dest = dest_dir / f"{src.stem}_sifted{n}{src.suffix}"; n += 1
            tmp = dest.with_name(dest.name + ".sift-tmp")
            try:
                if operation == "move":
                    try: src.rename(tmp)
                    except OSError: _shutil.copy2(src, tmp)
                else: _shutil.copy2(src, tmp)
                # Hard-link creation is atomic and fails if another process created dest.
                while True:
                    try:
                        os.link(tmp, dest)
                        tmp.unlink()
                        break
                    except FileExistsError:
                        n += 1
                        dest = dest_dir / f"{src.stem}_sifted{n}{src.suffix}"
                if operation == "move" and src.exists(): src.unlink()
                result.append({"path": rel, "destination": dest.relative_to(self.root).as_posix(), "ok": True})
            except Exception as exc:
                try: tmp.unlink(missing_ok=True)
                except OSError: pass
                if operation == "move" and not src.exists():
                    try:
                        if tmp.exists(): tmp.rename(src)
                    except OSError: pass
                result.append({"path": rel, "ok": False, "error": str(exc)})
        return result

    def close(self):
        with self.lock:
            self.closed = True
            self.generation += 1
            for job in self.jobs.values(): job["cancel"].set()
            self.db.close()
