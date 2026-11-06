import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import time
from pathlib import Path
import threading

from sift_ocr import (EngineUnavailable, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, match_reason, meaningful_words,
                      recognize_image, sample_interval, sample_video, tesseract_version, TextJobs)

FIXTURES = Path(__file__).parent / "fixtures" / "sift"


class OcrContractTests(unittest.TestCase):
    def test_empty_job_does_not_require_an_ocr_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = TextJobs(Path(tmp), Path(tmp) / "cache.sqlite3")
            job = {"state": "running", "cancel": threading.Event()}
            try:
                with patch("sift_ocr.tesseract_version", side_effect=EngineUnavailable("missing")) as engine:
                    jobs._run(job, [], "eng", 2, "")
                    engine.assert_not_called()
                self.assertEqual(job["state"], "done")
            finally:
                jobs.close()

    def test_meaningful_word_contract(self):
        self.assertEqual(meaningful_words("OK cat dog 48291 naïve"), ["cat", "dog", "naïve"])
        self.assertIsNone(match_reason("OK archive", 2))
        self.assertEqual(match_reason("Project archive", 2), "threshold")
        self.assertEqual(match_reason("Invoice", 2, "voice"), "query")
        self.assertIsNone(match_reason("Invoice", 2, "receipt"))
        self.assertIsNone(match_reason("Invoice document reference", 2, "receipt"))

    def test_duration_sampling_buckets(self):
        self.assertEqual([sample_interval(x) for x in (8.9, 14.99, 15, 59.9, 60, 600)], [1, 1, 2, 2, 5, 5])

    def test_documented_media_extensions(self):
        self.assertEqual(IMAGE_EXTENSIONS, {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".gif"})
        self.assertEqual(VIDEO_EXTENSIONS, {".mp4", ".mkv", ".mov", ".avi", ".3gp", ".webm"})

    @unittest.skipUnless(shutil.which("convert") and shutil.which("tesseract"), "local OCR tools unavailable")
    def test_known_and_low_text_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            for stem, expected in (("known", 5), ("low-text", 0)):
                png = Path(tmp) / f"{stem}.png"
                subprocess.run(["convert", str(FIXTURES / f"{stem}.svg"), str(png)], check=True)
                words = meaningful_words(recognize_image(png))
                self.assertGreaterEqual(len(words), expected)
                if stem == "known":
                    self.assertIn("threshold", (match_reason(" ".join(words)),))
                else:
                    self.assertIsNone(match_reason(" ".join(words)))

    def test_video_errors_are_reported(self):
        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            with self.assertRaises(EngineUnavailable):
                list(sample_video(FIXTURES / "missing.mp4"))
        else:
            with self.assertRaises(RuntimeError):
                list(sample_video(FIXTURES / "missing.mp4"))

    def test_missing_tesseract_has_actionable_error(self):
        with patch("sift_ocr.shutil.which", return_value=None):
            with self.assertRaisesRegex(EngineUnavailable, "Install Tesseract"):
                tesseract_version()

    @unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg") and shutil.which("tesseract"),
                         "local OCR tools unavailable")
    def test_video_fixture_samples_known_timestamp(self):
        frames = list(sample_video(FIXTURES / "text-at-3s.mp4"))
        self.assertTrue(frames)
        self.assertEqual([second for second, _ in frames], list(range(0, 8)))
        self.assertTrue(any("receipt" in text.casefold() for _, text in frames))

    def test_cache_and_confirmed_collision_safe_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"; root.mkdir()
            (root / "nested").mkdir(); (root / "nested" / "receipt.txt").write_text("local fixture")
            jobs = TextJobs(root, Path(tmp) / "cache.sqlite")
            job = {"matches": [{"path": "nested/receipt.txt"}], "state": "done", "cancel": __import__("threading").Event()}; jobs.jobs["fixture"] = job
            dest = root / "SIFT_docs" / "nested"; dest.mkdir(parents=True)
            (dest / "receipt.txt").write_text("keep me")
            result = jobs.action("fixture", ["nested/receipt.txt"], "copy")
            self.assertTrue(result[0]["ok"])
            self.assertEqual((dest / "receipt.txt").read_text(), "keep me")
            self.assertEqual((dest / "receipt_sifted1.txt").read_text(), "local fixture")
            moved = jobs.action("fixture", ["nested/receipt.txt"], "move")
            self.assertTrue(moved[0]["ok"])
            self.assertFalse((root / "nested" / "receipt.txt").exists())
            jobs.close()

    def test_failed_video_is_reported_separately_from_image_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"; root.mkdir()
            (root / "bad.mp4").write_bytes(b"not a video")
            (root / "good.png").write_bytes(b"fixture")
            with patch("sift_ocr.tesseract_version", return_value="test-engine"), \
                 patch("sift_ocr.recognize_image", return_value="Receipt reference archive"):
                jobs = TextJobs(root, Path(tmp) / "cache.sqlite")
                started = jobs.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = jobs.status(started["jobId"])
                    if status["state"] != "running": break
                    time.sleep(.02)
                self.assertEqual(status["state"], "done")
                self.assertEqual(status["matches"], 1)
                self.assertEqual(status["errors"][0]["kind"], "decode")
                jobs.close()

    def test_canceled_generation_cannot_write_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"; root.mkdir(); source = root / "photo.png"; source.write_bytes(b"fixture")
            started_ocr = threading.Event()
            def slow_ocr(_path, _language):
                started_ocr.set(); time.sleep(.3); return "Receipt invoice document"
            with patch("sift_ocr.tesseract_version", return_value="test-engine"), \
                 patch("sift_ocr.recognize_image", side_effect=slow_ocr):
                jobs = TextJobs(root, Path(tmp) / "cache.sqlite")
                old = jobs.start(videos=False)
                self.assertTrue(started_ocr.wait(2))
                current = jobs.start(images=False, videos=False)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and jobs.status(old["jobId"])["state"] == "running": time.sleep(.02)
                self.assertEqual(jobs.status(old["jobId"])["state"], "canceled")
                self.assertEqual(jobs.status(current["jobId"])["state"], "done")
                self.assertEqual(jobs.db.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0], 0)
                self.assertTrue(source.exists(), "canceling OCR must leave the source untouched")
                jobs.close()

    def test_brand_state_migration_copies_once_without_clobbering_conflict(self):
        from photto import migrate_state_if_absent
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "photto"; source.mkdir(); (source / "marks.json").write_text("old")
            target = Path(tmp) / "sift"
            self.assertTrue(migrate_state_if_absent(source, target))
            self.assertEqual((target / "marks.json").read_text(), "old")
            (source / "marks.json").write_text("new-old")
            self.assertFalse(migrate_state_if_absent(source, target))
            self.assertEqual((target / "marks.json").read_text(), "old")
            self.assertEqual((source / "marks.json").read_text(), "new-old")

    def test_ocr_cache_reuses_unchanged_files_and_rescans_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"; root.mkdir()
            source = root / "card.png"; source.write_bytes(b"fixture")
            with patch("sift_ocr.tesseract_version", return_value="tesseract-test"), \
                 patch("sift_ocr.recognize_image", return_value="Sample invoice document") as ocr:
                jobs = TextJobs(root, Path(tmp) / "cache.sqlite")
                def finish():
                    job = jobs.start(videos=False)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        state = jobs.status(job["jobId"])
                        if state["state"] != "running": return state
                        time.sleep(.02)
                    self.fail("OCR job did not complete")
                self.assertEqual(finish()["matches"], 1)
                self.assertEqual(ocr.call_count, 1)
                self.assertEqual(finish()["cacheHits"], 1)
                self.assertEqual(ocr.call_count, 1)
                source.write_bytes(b"changed source")
                self.assertEqual(finish()["matches"], 1)
                self.assertEqual(ocr.call_count, 2)
                jobs.close()


if __name__ == "__main__":
    unittest.main()
