"""HTTP-level tests for the paged API and decision routes (tasks 2.4, 2.5, 2.7)."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.helpers import TempFolderCase, photto, tiny_png
from sift_ocr import TextJobs

TOKEN = "test-token"


class ApiTests(TempFolderCase):
    def setUp(self) -> None:
        super().setUp()
        self._old_session = getattr(photto.Handler, "session", None)
        self._old_token = getattr(photto.Handler, "token", "")
        photto.Handler.session = None
        photto.Handler.token = TOKEN
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), photto.Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        owned = getattr(photto.Handler, "session", None)
        if owned is not None and owned not in self._sessions:
            owned.close()
        photto.Handler.session = self._old_session
        photto.Handler.token = self._old_token
        super().tearDown()

    # ------------------------------------------------------------ helpers --
    def req(self, path: str, body=None, token: str = TOKEN, headers: dict | None = None,
            raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"X-Token": token, **(headers or {})}
        if data is not None:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, headers=h,
                                   method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            try:
                return err.code, dict(err.headers), err.read()
            finally:
                err.close()

    def get_json(self, path: str, token: str = TOKEN):
        status, headers, body = self.req(path, token=token)
        return status, headers, json.loads(body)

    def use_session(self):
        session = self.session()
        photto.Handler.session = session
        return session

    # -------------------------------------------------------------- tests --
    def test_no_folder_and_token_responses(self) -> None:
        status, _, d = self.get_json("/api/files")
        self.assertEqual(status, 200)
        self.assertTrue(d["needsPick"])
        self.assertEqual(d["files"], [])
        self.assertIn("platform", d)

        status, _, _ = self.req("/api/scan")
        self.assertEqual(status, 409, "scan status needs a folder")

        status, _, _ = self.req("/api/files", token="wrong")
        self.assertEqual(status, 403)

        status, _, _ = self.req("/api/decide", {"path": "a.png", "decision": "keep"})
        self.assertEqual(status, 409)

        status, _, _ = self.req("/api/decide", {"path": "a.png", "decision": "keep"},
                                token="wrong")
        self.assertEqual(status, 403)

    def test_paged_files_scan_and_filters(self) -> None:
        for i in range(1, 10):
            self.write(f"f{i}.png", b"x" * (i * 10), mtime=1000 + i)
        session = self.use_session()

        status, headers, d = self.get_json("/api/files?limit=4&sort=name")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("application/json"))
        for key in ("root", "name", "recursive", "truncated", "offset", "limit",
                    "effectiveLimit", "total", "revision", "scan", "counts", "files",
                    "nextCursor", "version", "platform"):
            self.assertIn(key, d, key)
        self.assertEqual(d["total"], 9)
        self.assertEqual(d["scan"]["state"], "complete")
        self.assertEqual(d["counts"]["total"], 9)
        self.assertEqual(d["effectiveLimit"], 4)
        self.assertEqual([f["path"] for f in d["files"]], ["f1.png", "f2.png", "f3.png", "f4.png"])
        self.assertTrue(d["nextCursor"])

        collected = [f["path"] for f in d["files"]]
        cursor = d["nextCursor"]
        while cursor:
            status, _, page = self.get_json(f"/api/files?limit=4&sort=name&cursor={cursor}")
            self.assertEqual(status, 200)
            collected += [f["path"] for f in page["files"]]
            cursor = page["nextCursor"]
        self.assertEqual(collected, [f"f{i}.png" for i in range(1, 10)])

        status, _, scan = self.get_json("/api/scan")
        self.assertEqual(status, 200)
        self.assertNotIn("files", scan)
        self.assertEqual(scan["revision"], d["revision"])
        self.assertEqual(scan["counts"]["total"], 9)
        self.assertEqual(scan["scan"]["state"], "complete")

        _, _, clamped = self.get_json("/api/files?limit=9999")
        self.assertEqual(clamped["effectiveLimit"], photto.MAX_PAGE_SIZE)
        _, _, zero = self.get_json("/api/files?limit=0")
        self.assertEqual(zero["effectiveLimit"], 1)
        _, _, empty = self.get_json("/api/files?offset=99")
        self.assertEqual(empty["files"], [])
        self.assertIsNone(empty["nextCursor"])
        _, _, photos = self.get_json("/api/files?group=photos&limit=50")
        self.assertEqual(len(photos["files"]), 9)

        status, _, _ = self.req("/api/decide", {"path": "f1.png", "decision": "keep"})
        self.assertEqual(status, 200)
        _, _, kept = self.get_json("/api/files?decision=keep&limit=50")
        self.assertEqual([f["path"] for f in kept["files"]], ["f1.png"])
        _, _, deleted = self.get_json("/api/files?decision=delete&limit=50")
        self.assertEqual(deleted["files"], [])
        _, _, any_page = self.get_json("/api/files?decision=any&limit=50")
        self.assertEqual(len(any_page["files"]), 9)
        self.assertEqual(any_page["counts"]["undecided"], 8)
        self.assertEqual(session.index.file_count(), 9)

    def test_scan_refresh_route(self) -> None:
        self.write("a.png")
        session = self.use_session()
        before = session.index.revision()
        self.write("b.png")
        status, _, d = self.get_json("/api/files?refresh=1")
        self.assertEqual(status, 200)
        self.assertTrue(session.wait_for_scan(30))
        self.assertGreater(session.index.revision(), before)
        self.assertEqual(session.index.file_count(), 2)
        self.assertIn(d["total"], (1, 2), "the response is served while the refresh is starting")

    def test_decide_undo_reset_and_commit(self) -> None:
        self.write("a.png", b"a" * 10)
        self.write("b.png", b"b" * 20)
        self.write("c.png", b"c" * 30)
        session = self.use_session()

        status, _, _ = self.req("/api/decide", {"path": "a.png", "decision": "delete"})
        self.assertEqual(status, 200)
        _, _, page = self.get_json("/api/files?decision=delete&limit=50")
        self.assertEqual([f["path"] for f in page["files"]], ["a.png"])
        self.assertEqual(json.loads(session.state_file.read_text())["decisions"],
                         {"a.png": "delete"})

        self.req("/api/decide", {"path": "a.png", "decision": None})
        _, _, page = self.get_json("/api/files?decision=delete&limit=50")
        self.assertEqual(page["files"], [])

        self.req("/api/decide", {"path": "b.png", "decision": "favorite"})
        self.req("/api/reset", {})
        self.assertEqual(json.loads(session.state_file.read_text())["decisions"], {})
        self.assertEqual(session.index.counts()["undecided"], 3)

        self.req("/api/decide", {"path": "a.png", "decision": "delete"})
        self.req("/api/decide", {"path": "b.png", "decision": "delete"})
        moved: list[str] = []

        def fake_trash(path):
            moved.append(path.name)
            path.unlink()

        with mock.patch.object(photto, "trash", side_effect=fake_trash):
            status, _, body = self.req("/api/commit", {})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(result["trashed"]), ["a.png", "b.png"])
        self.assertEqual(sorted(moved), ["a.png", "b.png"])
        self.assertGreater(result["freed"], 0)
        self.assertEqual(sorted(result["failed"]), [])
        self.assertEqual(session.index.file_count(), 1)
        self.assertFalse((self.root / "a.png").exists())
        self.assertFalse((self.root / "b.png").exists())
        _, _, files = self.get_json("/api/files?decision=any&limit=50")
        self.assertEqual([f["path"] for f in files["files"]], ["c.png"])
        self.assertEqual(json.loads(session.state_file.read_text())["decisions"], {})

    def test_commit_moves_favorites(self) -> None:
        self.write("a.png")
        self.write("b.png")
        session = self.use_session()
        self.req("/api/decide", {"path": "a.png", "decision": "favorite"})

        status, _, body = self.req("/api/commit", {"favoritesFolder": "Favorites"})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["favorited"], ["Favorites/a.png"])
        self.assertTrue((self.root / "Favorites" / "a.png").is_file())
        self.assertFalse((self.root / "a.png").exists())
        self.assertTrue(session.wait_for_scan(30))
        self.assertEqual(json.loads(session.state_file.read_text())["decisions"],
                         {"Favorites/a.png": "favorite"})
        _, _, page = self.get_json("/api/files?decision=favorite&limit=50")
        self.assertEqual([f["path"] for f in page["files"]], ["Favorites/a.png"])

    def test_root_switch_and_decision_continuity(self) -> None:
        self.write("a.png")
        self.write("b.png")
        status, _, body = self.req("/api/root", {"path": str(self.root)})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["root"], str(self.root))
        session = photto.Handler.session
        self.assertIsNotNone(session)
        self.assertTrue(session.wait_for_scan(30))
        _, _, d = self.get_json("/api/files?limit=50")
        self.assertEqual(d["total"], 2)

        self.req("/api/decide", {"path": "a.png", "decision": "keep"})
        session.close()
        status, _, _ = self.req("/api/root", {"path": str(self.root)})
        self.assertEqual(status, 200)
        reopened = photto.Handler.session
        self._sessions.append(reopened)
        self.assertTrue(reopened.wait_for_scan(30))
        _, _, d = self.get_json("/api/files?decision=keep&limit=50")
        self.assertEqual([f["path"] for f in d["files"]], ["a.png"])

        other = self.tmp / "other"
        other.mkdir()
        (other / "only.png").write_bytes(tiny_png())
        self.req("/api/root", {"path": str(other)})
        self.assertTrue(photto.Handler.session.wait_for_scan(30))
        _, _, d = self.get_json("/api/files?limit=50")
        self.assertEqual(d["total"], 1)
        self.assertNotEqual(d["root"], str(self.root))

        status, _, _ = self.req("/api/root", {"path": str(self.tmp / "missing")})
        self.assertEqual(status, 400)

    def test_thumbnail_versioned_cache_headers(self) -> None:
        data = tiny_png(2, 2)
        self.write("pic.png", data)
        self.use_session()

        status, headers, body = self.req("/thumb?p=pic.png")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("image/png"))
        self.assertEqual(headers.get("Cache-Control"), "private, max-age=300")
        self.assertEqual(body, data)

        status, headers, body2 = self.req("/thumb?p=pic.png&v=1234-99")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "private, max-age=31536000, immutable")
        self.assertEqual(body2, data)

        status, headers, partial = self.req("/raw?p=pic.png", headers={"Range": "bytes=0-7"})
        self.assertEqual(status, 206)
        self.assertEqual(len(partial), 8)
        self.assertEqual(partial, data[:8])

        status, _, _ = self.req("/thumb?p=missing.png")
        self.assertEqual(status, 404)

    def test_thumbnail_burst_is_bounded(self) -> None:
        self.write("pic.png", tiny_png())
        self.use_session()
        original = photto.image_thumb
        lock = threading.Lock()
        active = 0
        peak = 0

        def slow(path):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.15)
                return original(path)
            finally:
                with lock:
                    active -= 1

        def fetch(_):
            return self.req("/thumb?p=pic.png")[0]

        with mock.patch.object(photto, "image_thumb", side_effect=slow):
            with ThreadPoolExecutor(max_workers=12) as pool:
                statuses = list(pool.map(fetch, range(12)))
        self.assertEqual(statuses, [200] * 12)
        self.assertLessEqual(peak, photto.THUMB_LIMIT,
                             "thumbnail decodes must respect the configured bound")
        self.assertGreaterEqual(peak, 2, "the bounded pipeline should still run in parallel")

    def test_missing_and_bad_requests(self) -> None:
        self.use_session()
        # A mark under the root is accepted even before the file is indexed;
        # revalidation removes marks whose file never existed.
        status, _, _ = self.req("/api/decide", {"path": "nope.png", "decision": "keep"})
        self.assertEqual(status, 200)
        status, _, _ = self.req("/api/decide", {"path": "../escape.png", "decision": "keep"})
        self.assertEqual(status, 400)
        status, _, _ = self.req("/api/decide", raw=b"{bad json")
        self.assertEqual(status, 400)
        status, _, _ = self.req("/api/unknown")
        self.assertEqual(status, 404)

    def test_text_job_routes_are_authenticated_and_page_results(self) -> None:
        source = self.write("nested/source.png", b"source bytes")
        prior = self.root / "SIFT_docs" / "nested" / "source.png"
        prior.parent.mkdir(parents=True); prior.write_bytes(b"keep existing")
        session = self.use_session()
        session.set_decision("keep.jpg", "keep")
        session.text_jobs.jobs["preview-job"] = {"matches": [{"path": "nested/source.png"}], "state": "done",
            "cancel": threading.Event(), "generation": 0}
        status, _, preview = self.req("/api/text/preview", {"jobId": "preview-job", "paths": ["nested/source.png"]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(preview)["destinations"][0]["destination"],
                         "SIFT_docs/nested/source_sifted1.png")
        status, _, action = self.req("/api/text/action", {"jobId": "preview-job", "paths": ["nested/source.png"], "action": "copy"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(action)["copied"], 1)
        self.assertEqual(prior.read_bytes(), b"keep existing")
        self.assertEqual((self.root / "SIFT_docs" / "nested" / "source_sifted1.png").read_bytes(), b"source bytes")
        self.assertTrue(source.exists())
        status, _, started = self.get_json("/api/text/status?jobId=missing")
        self.assertEqual(status, 404)
        status, _, start = self.req("/api/text/start", {"images": False, "videos": False})
        self.assertEqual(status, 200)
        job_id = json.loads(start)["jobId"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, state = self.get_json(f"/api/text/status?jobId={job_id}")
            if state["state"] != "running": break
            time.sleep(.02)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["total"], 0)
        status, _, page = self.get_json(f"/api/text/results?jobId={job_id}&offset=-2&limit=999")
        self.assertEqual(status, 200, page)
        self.assertEqual((page["offset"], page["limit"], page["items"]), (0, 100, []))
        status, _, _ = self.req("/api/text/cancel", {"jobId": job_id}, token="wrong")
        self.assertEqual(status, 403)
        status, _, _ = self.req("/api/text/clear", {})
        self.assertEqual(status, 200)
        self.assertEqual(session.decisions["keep.jpg"], "keep")

    def test_action_reports_missing_source_and_continues_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"; root.mkdir()
            (root / "present.txt").write_text("preserve")
            jobs = TextJobs(root, Path(tmp) / "cache.sqlite")
            jobs.jobs["partial"] = {"matches": [{"path": "missing.txt"}, {"path": "present.txt"}],
                                     "state": "done", "cancel": threading.Event()}
            outcome = jobs.action("partial", ["missing.txt", "present.txt"], "copy")
            self.assertEqual([item["ok"] for item in outcome], [False, True])
            self.assertEqual((root / "present.txt").read_text(), "preserve")
            self.assertEqual((root / "SIFT_docs" / "present.txt").read_text(), "preserve")
            jobs.close()


if __name__ == "__main__":
    unittest.main()
