"""Unit tests for the SQLite folder index and paged queries (tasks 1.4, 2.1-2.6)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unittest
from unittest import mock

from tests.helpers import TempFolderCase, photto


def key_for(root) -> str:
    return hashlib.sha1(str(root).encode()).hexdigest()[:16]


def row(rel: str, size: int = 10, mtime: float = 1000.0, kind: str = "image",
        shuffle: float = 0.5, generation: int = 1) -> tuple:
    name = os.path.basename(rel)
    parent = os.path.dirname(rel)
    ext = os.path.splitext(name)[1].lstrip(".")
    return (rel, name, parent, ext, kind, size, mtime, photto.sort_name(name), shuffle, generation)


class IndexLifecycleTests(TempFolderCase):
    def test_create_reopen_and_rebuild_on_schema_mismatch(self) -> None:
        index = self.index()
        generation = index.begin_scan()
        self.assertTrue(index.upsert_batch([row("a.png")], generation, 1))
        self.assertEqual(index.finish_scan(generation, 1, False), [])
        self.assertEqual(index.file_count(), 1)
        index.close()

        conn = sqlite3.connect(str(self.tmp / "index" / "direct.sqlite3"))
        conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        conn.commit()
        conn.close()

        rebuilt = self.index()
        self.assertEqual(rebuilt.file_count(), 0, "a version mismatch must drop stale rows")
        self.assertEqual(rebuilt.scan_info()["state"], "idle")
        self.assertEqual(rebuilt.revision(), 0)
        rebuilt.close()

    def test_corrupt_index_is_rebuilt(self) -> None:
        path = self.tmp / "index" / "direct.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a sqlite database" * 64)

        index = photto.FolderIndex(path)
        self.assertEqual(index.file_count(), 0)
        self.assertEqual(index.scan_info()["state"], "idle")
        index.close()

    def test_decisions_json_is_untouched_by_rebuild(self) -> None:
        self.write("a.png", b"a" * 20)
        session = self.session()
        session.set_decision("a.png", "keep")
        before = session.state_file.read_bytes()
        index_path = self.tmp / "index" / f"{key_for(self.root)}.sqlite3"
        session.close()
        index_path.write_bytes(b"broken" * 100)

        reopened = photto.Session(self.root, True, False, index_dir=self.tmp / "index")
        self._sessions.append(reopened)
        self.assertTrue(reopened.wait_for_scan(30))
        self.assertEqual(reopened.state_file.read_bytes(), before)
        self.assertEqual(self.paths(reopened.page(decision="keep", limit=50)), ["a.png"])

    def test_version_mismatch_keeps_decisions_json(self) -> None:
        self.write("a.png", b"a" * 20)
        session = self.session()
        session.set_decision("a.png", "favorite")
        before = session.state_file.read_bytes()
        index_path = self.tmp / "index" / f"{key_for(self.root)}.sqlite3"
        session.close()

        conn = sqlite3.connect(str(index_path))
        conn.execute("UPDATE meta SET value='42' WHERE key='schema_version'")
        conn.commit()
        conn.close()

        reopened = photto.Session(self.root, True, False, index_dir=self.tmp / "index")
        self._sessions.append(reopened)
        self.assertTrue(reopened.wait_for_scan(30))
        self.assertEqual(reopened.state_file.read_bytes(), before)
        self.assertEqual(self.paths(reopened.page(decision="favorite", limit=50)), ["a.png"])


class ScanTests(TempFolderCase):
    def test_completed_scan_and_counts(self) -> None:
        self.write("a.png", b"a" * 20)
        self.write("sub/b.mp4", b"b" * 30)
        self.write("sub/c.pdf", b"c" * 40)
        session = self.session()
        info = session.index.scan_info()
        self.assertEqual(info["state"], "complete")
        self.assertEqual(info["scanned"], 3)
        self.assertEqual(session.index.file_count(), 3)
        counts = session.index.counts()
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["byGroup"]["photos"], 1)
        self.assertEqual(counts["byGroup"]["videos"], 1)
        self.assertEqual(counts["byGroup"]["docs"], 1)

    def test_empty_folder(self) -> None:
        session = self.session()
        self.assertEqual(session.index.scan_info()["state"], "complete")
        self.assertEqual(session.index.file_count(), 0)
        self.assertEqual(session.index.counts()["total"], 0)
        page = session.index.page(limit=10)
        self.assertEqual(page["files"], [])
        self.assertIsNone(page["nextCursor"])

    def test_non_recursive_scan(self) -> None:
        self.write("a.png")
        self.write("sub/b.png")
        session = self.session(recursive=False)
        self.assertEqual(self.paths(session.page(limit=50, decision="any")), ["a.png"])

    def test_hidden_files_are_opt_in(self) -> None:
        self.write("a.png")
        self.write(".hidden.png")
        self.assertEqual(len(self.session().page(limit=50, decision="any")["files"]), 1)
        self.assertEqual(len(self.session(include_hidden=True).page(limit=50, decision="any")["files"]), 2)

    def test_cancelled_generation_cannot_write(self) -> None:
        index = self.index()
        first = index.begin_scan()
        self.assertTrue(index.upsert_batch([row("a.png", generation=first)], first, 1))
        second = index.begin_scan()
        self.assertEqual(index.current_generation(), second)
        self.assertFalse(index.upsert_batch([row("b.png", generation=first)], first, 2),
                         "a stale generation must be rejected")
        self.assertEqual(index.finish_scan(first, 2, False), [],
                         "a stale generation must not clean up rows")
        self.assertEqual(index.file_count(), 1)
        self.assertEqual(index.finish_scan(second, 0, False), ["a.png"])
        self.assertEqual(index.file_count(), 0)
        index.close()

    def test_refresh_cancels_and_restarts_cleanly(self) -> None:
        for i in range(40):
            self.write(f"f{i:02d}.png", b"x" * (i + 1))
        session = self.session()
        before = session.index.revision()
        session.start_scan(refresh=True)
        session.start_scan(refresh=True)
        self.assertTrue(session.wait_for_scan(30))
        self.assertEqual(session.index.scan_info()["state"], "complete")
        self.assertEqual(session.index.file_count(), 40)
        self.assertGreater(session.index.revision(), before)

    def test_truncated_scan(self) -> None:
        for i in range(3):
            self.write(f"f{i}.png")
        with mock.patch.object(photto, "MAX_FILES", 2):
            session = self.session()
        self.assertTrue(session.truncated)
        self.assertTrue(session.index.truncated())
        self.assertEqual(session.index.file_count(), 2)

    def test_unreadable_subtree_reports_error_but_keeps_rows(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("permission errors cannot be simulated as root")
        self.write("readable.png")
        locked = self.root / "locked"
        locked.mkdir()
        self.write("locked/hidden.png")
        os.chmod(locked, 0)
        try:
            session = self.session()
            info = session.index.scan_info()
            self.assertEqual(info["state"], "error")
            self.assertTrue(info["error"])
            self.assertIn("readable.png", self.paths(session.page(limit=50, decision="any")))
        finally:
            os.chmod(locked, 0o755)

    def test_revision_increases_on_revalidation(self) -> None:
        self.write("a.png")
        session = self.session()
        before = session.index.revision()
        self.write("b.png")
        session.start_scan(refresh=True)
        self.assertTrue(session.wait_for_scan(30))
        self.assertGreater(session.index.revision(), before)
        self.assertEqual(session.index.scan_info()["scanned"], 2)


class PagingTests(TempFolderCase):
    def setUp(self) -> None:
        super().setUp()
        for i in range(1, 24):
            self.write(f"shot{i}.png", b"x" * (i * 10), mtime=1000.0 + i)
        self.session_obj = self.session()
        self.index = self.session_obj.index

    def tearDown(self) -> None:
        super().tearDown()

    def _all(self, sort: str, **kwargs) -> list[str]:
        return self.paths(self.index.page(limit=photto.MAX_PAGE_SIZE, sort=sort,
                                          decision="any", **kwargs))

    def test_every_sort_pages_without_gaps_or_repeats(self) -> None:
        for sort in photto.SORTS:
            expected = self._all(sort)
            self.assertEqual(len(expected), 23)
            collected: list[str] = []
            cursor = ""
            pages = 0
            while True:
                page = self.index.page(cursor=cursor, limit=4, sort=sort, decision="any")
                self.assertLessEqual(len(page["files"]), 4)
                collected += self.paths(page)
                pages += 1
                cursor = page["nextCursor"]
                if not cursor:
                    break
                self.assertLess(pages, 20, "cursor paging did not terminate")
            self.assertEqual(collected, expected, f"cursor order differs for {sort}")
            self.assertEqual(len(set(collected)), 23, f"duplicates while paging {sort}")

    def test_sort_orders(self) -> None:
        self.assertEqual(self._all("newest"), [f"shot{i}.png" for i in range(23, 0, -1)])
        self.assertEqual(self._all("oldest"), [f"shot{i}.png" for i in range(1, 24)])
        self.assertEqual(self._all("largest"), [f"shot{i}.png" for i in range(23, 0, -1)])
        self.assertEqual(self._all("name"), [f"shot{i}.png" for i in range(1, 24)])

    def test_natural_name_sort(self) -> None:
        self.assertLess(photto.sort_name("IMG_2.png"), photto.sort_name("IMG_10.png"))
        self.assertLess(photto.sort_name("img2.png"), photto.sort_name("img10.png"))

    def test_limit_clamping(self) -> None:
        self.assertEqual(self.index.page(limit=0, decision="any")["effectiveLimit"], 1)
        clamped = self.index.page(limit=9999, decision="any")
        self.assertEqual(clamped["effectiveLimit"], photto.MAX_PAGE_SIZE)
        self.assertEqual(len(clamped["files"]), 23)

    def test_offset_random_access(self) -> None:
        expected = self._all("name")
        page = self.index.page(offset=5, limit=3, sort="name", decision="any")
        self.assertEqual(self.paths(page), expected[5:8])
        self.assertIsNotNone(page["nextCursor"])
        tail = self.index.page(offset=21, limit=5, sort="name", decision="any")
        self.assertEqual(self.paths(tail), expected[21:])
        self.assertIsNone(tail["nextCursor"], "an offset page past the remaining rows has no cursor")

    def test_empty_pages_past_the_end(self) -> None:
        for offset in (23, 24, 9999):
            page = self.index.page(offset=offset, limit=5, decision="any")
            self.assertEqual(page["files"], [])
            self.assertIsNone(page["nextCursor"])

    def test_invalid_cursor_restarts(self) -> None:
        expected = self._all("name")
        page = self.index.page(cursor="not-base64!!", limit=3, sort="name", decision="any")
        self.assertEqual(self.paths(page), expected[:3])

    def test_cursor_from_another_sort_restarts(self) -> None:
        first = self.index.page(limit=2, sort="newest", decision="any")
        page = self.index.page(cursor=first["nextCursor"], limit=2, sort="name", decision="any")
        self.assertEqual(self.paths(page), self._all("name")[:2])

    def test_cursor_query_is_bounded(self) -> None:
        first = self.index.page(limit=2, sort="name", decision="any")
        statements: list[str] = []
        self.index._conn.set_trace_callback(statements.append)
        try:
            self.index.page(cursor=first["nextCursor"], limit=2, sort="name", decision="any")
        finally:
            self.index._conn.set_trace_callback(None)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertTrue(selects)
        self.assertTrue(any("LIMIT" in s.upper() for s in selects),
                        "a cursor page must not load every remaining row")

    def test_keyset_stays_stable_when_a_page_item_is_decided(self) -> None:
        expected = self._all("name")
        first = self.index.page(limit=2, sort="name", decision="none")
        self.assertEqual(self.paths(first), expected[:2])
        self.index.set_decision(expected[0], "keep")
        second = self.index.page(cursor=first["nextCursor"], limit=3, sort="name", decision="none")
        self.assertEqual(self.paths(second), expected[2:5],
                         "deciding an earlier file must not skip the following file")

    def test_group_and_decision_filters(self) -> None:
        self.write("clip.mp4")
        self.write("tone.mp3")
        self.write("manual.pdf")
        self.write("notes.txt")
        self.write("archive.zip")
        session = self.session()
        self.index = session.index
        index = session.index
        self.index.set_decision("shot1.png", "keep")
        self.index.set_decision("shot2.png", "delete")
        self.index.set_decision("clip.mp4", "favorite")

        self.assertEqual(set(self.paths(index.page(limit=500, sort="name", group="photos",
                                                   decision="any"))),
                         {f"shot{i}.png" for i in range(1, 24)})
        self.assertEqual(self.paths(index.page(limit=500, group="videos", decision="any")),
                         ["clip.mp4"])
        self.assertEqual(self.paths(index.page(limit=500, group="audio", decision="any")),
                         ["tone.mp3"])
        self.assertEqual(set(self.paths(index.page(limit=500, group="docs", decision="any"))),
                         {"manual.pdf", "notes.txt"})
        self.assertEqual(self.paths(index.page(limit=500, group="other", decision="any")),
                         ["archive.zip"])
        self.assertEqual(self.paths(index.page(limit=500, decision="keep")), ["shot1.png"])
        self.assertEqual(self.paths(index.page(limit=500, decision="delete")), ["shot2.png"])
        self.assertEqual(self.paths(index.page(limit=500, decision="favorite")), ["clip.mp4"])
        self.assertEqual(len(index.page(limit=500, decision="any")["files"]), 28)
        self.assertEqual(len(index.page(limit=500, decision="none")["files"]), 25)

        counts = index.counts()
        self.assertEqual(counts["total"], 28, "counts describe the whole folder")
        self.assertEqual(counts["undecided"], 25)
        self.assertEqual(counts["byDecision"], {"keep": 1, "delete": 1, "favorite": 1})
        self.assertEqual(counts["byGroup"], {"photos": 23, "videos": 1, "audio": 1,
                                             "docs": 2, "other": 1})
        self.assertEqual(counts["undecidedByGroup"], {"photos": 21, "videos": 0, "audio": 1,
                                                       "docs": 2, "other": 1})
        self.assertEqual(counts["bytesByDecision"], {"keep": 10, "delete": 20, "favorite": 1})


class DecisionAndRevalidationTests(TempFolderCase):
    def test_decide_undo_reset_and_json_agreement(self) -> None:
        self.write("a.png")
        self.write("b.png")
        session = self.session()

        session.set_decision("a.png", "delete")
        self.assertEqual(self.paths(session.page(decision="delete", limit=50)), ["a.png"])
        stored = json.loads(session.state_file.read_text())
        self.assertEqual(stored["decisions"], {"a.png": "delete"})
        self.assertEqual(session.index.counts()["byDecision"]["delete"], 1)

        session.set_decision("a.png", None)
        self.assertEqual(self.paths(session.page(decision="delete", limit=50)), [])
        self.assertEqual(json.loads(session.state_file.read_text())["decisions"], {})
        self.assertEqual(session.index.counts()["byDecision"]["delete"], 0)

        session.set_decision("a.png", "keep")
        session.set_decision("b.png", "favorite")
        session.reset_decisions()
        self.assertEqual(json.loads(session.state_file.read_text())["decisions"], {})
        self.assertEqual(session.index.counts()["undecided"], 2)

    def test_decisions_survive_restart(self) -> None:
        self.write("a.png")
        session = self.session()
        session.set_decision("a.png", "keep")
        session.close()

        reopened = photto.Session(self.root, True, False, index_dir=self.tmp / "index")
        self._sessions.append(reopened)
        self.assertTrue(reopened.wait_for_scan(30))
        self.assertEqual(self.paths(reopened.page(decision="keep", limit=50)), ["a.png"])

    def test_decisions_outside_the_loaded_page_survive_revalidation(self) -> None:
        for i in range(1, 11):
            self.write(f"f{i:02d}.png", mtime=1000 + i)
        session = self.session()
        page = session.page(limit=3, sort="name", decision="any")
        last = self.paths(session.page(limit=50, sort="name", decision="any"))[-1]
        self.assertNotIn(last, self.paths(page))
        session.set_decision(last, "keep")
        session.start_scan(refresh=True)
        self.assertTrue(session.wait_for_scan(30))
        self.assertEqual(self.paths(session.page(decision="keep", limit=50)), [last])

    def test_revalidation_updates_adds_removes_and_preserves_marks(self) -> None:
        self.write("a.png", b"a" * 10, mtime=1000)
        self.write("b.png", b"b" * 20, mtime=1001)
        self.write("c.png", b"c" * 30, mtime=1002)
        session = self.session()
        session.set_decision("b.png", "keep")
        session.set_decision("c.png", "delete")

        conn = sqlite3.connect(str(self.tmp / "index" / f"{key_for(self.root)}.sqlite3"))
        shuffle_before = conn.execute("SELECT shuffle FROM files WHERE rel='a.png'").fetchone()[0]
        conn.close()

        self.write("a.png", b"a" * 99, mtime=2000)
        (self.root / "c.png").unlink()
        self.write("d.png", b"d" * 40, mtime=2001)
        session.start_scan(refresh=True)
        self.assertTrue(session.wait_for_scan(30))

        files = {f["path"]: f for f in session.page(limit=50, decision="any")["files"]}
        self.assertEqual(set(files), {"a.png", "b.png", "d.png"})
        self.assertEqual(files["a.png"]["size"], 99)
        self.assertEqual(files["a.png"]["mtime"], 2000)
        conn = sqlite3.connect(str(self.tmp / "index" / f"{key_for(self.root)}.sqlite3"))
        shuffle_after = conn.execute("SELECT shuffle FROM files WHERE rel='a.png'").fetchone()[0]
        conn.close()
        self.assertEqual(shuffle_before, shuffle_after, "shuffle rank must survive revalidation")

        stored = json.loads(session.state_file.read_text())["decisions"]
        self.assertEqual(stored, {"b.png": "keep"},
                         "a mark for a removed file must be dropped; existing marks stay")
        self.assertEqual(self.paths(session.page(decision="keep", limit=50)), ["b.png"])
        self.assertEqual(self.paths(session.page(decision="delete", limit=50)), [])
        self.assertEqual(session.index.file_count(), 3)

    def test_mark_for_a_missing_file_is_not_recreated(self) -> None:
        self.write("a.png")
        session = self.session()
        session.set_decision("ghost.png", "keep")
        session.start_scan(refresh=True)
        self.assertTrue(session.wait_for_scan(30))
        self.assertNotIn("ghost.png", json.loads(session.state_file.read_text())["decisions"])


if __name__ == "__main__":
    unittest.main()
