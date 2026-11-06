# SIFT OCR behavior contract

SIFT performs OCR locally. Desktop OCR uses Tesseract; desktop video decoding uses FFmpeg and FFprobe. Android uses its bundled on-device Latin text model and `MediaMetadataRetriever`. Source media and recognized text are not uploaded.

The default minimum is **2 meaningful words**. A meaningful word contains at least three Unicode letters; digits, punctuation, and shorter words do not count. A configured threshold applies when no query is entered. A non-empty query is matched case-insensitively and qualifies on its own, even if the text has fewer than two meaningful words. `tests/test_sift_ocr.py` covers word counting, threshold, case-insensitive query, and the query-below-threshold case.

Image OCR supports JPEG, PNG, WebP, BMP, TIFF, HEIC, and GIF. The deterministic `known.svg` and `low-text.svg` fixtures are rendered to PNG in the OCR test; the nested fixture directory exercises relative-path handling. Video OCR supports MP4, MKV, MOV, AVI, 3GP, and WebM. Videos are sampled every 1 second below 15 seconds, every 2 seconds from 15 to under 60 seconds, and every 5 seconds at 60 seconds or longer. `text-at-3s.mp4` contains a known text frame and the tests assert the short-video sample timestamps.

Video decode errors and unavailable tools are errors, not non-matches. Image OCR remains available when video tools are unavailable. OCR results may differ by engine, but threshold and query matching follow this shared contract.
