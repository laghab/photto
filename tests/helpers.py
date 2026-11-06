"""Shared helpers for the photto test suite."""
from __future__ import annotations

import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import photto  # noqa: E402


def tiny_png(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (30, 40, 50)) -> bytes:
    """A valid, tiny PNG for thumbnail tests."""
    raw = b"".join(b"\0" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


class TempFolderCase(unittest.TestCase):
    """A test case with an isolated folder, state directory, and cache."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="photto-test-"))
        self.root = self.tmp / "root"
        self.root.mkdir()
        self._state = photto.STATE_DIR
        self._cache = photto.CACHE
        photto.STATE_DIR = self.tmp / "state"
        photto.CACHE = self.tmp / "cache"
        self._sessions: list[photto.Session] = []

    def tearDown(self) -> None:
        for session in self._sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        photto.STATE_DIR = self._state
        photto.CACHE = self._cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ fixtures --
    def write(self, rel: str, content: bytes = b"x", mtime: float | None = None) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mtime is not None:
            import os
            os.utime(path, (mtime, mtime))
        return path

    def session(self, recursive: bool = True, include_hidden: bool = False,
                wait: bool = True) -> photto.Session:
        session = photto.Session(self.root, recursive, include_hidden,
                                 index_dir=self.tmp / "index")
        self._sessions.append(session)
        if wait:
            self.assertTrue(session.wait_for_scan(30), "scan did not finish")
        return session

    def index(self) -> photto.FolderIndex:
        return photto.FolderIndex(self.tmp / "index" / "direct.sqlite3")

    @staticmethod
    def paths(page: dict) -> list[str]:
        return [f["path"] for f in page["files"]]
