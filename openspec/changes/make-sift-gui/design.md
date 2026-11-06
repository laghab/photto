# Design

## Context

See `proposal.md` for the product motivation and scope. The current product has one shared HTML/JavaScript UI, a Python desktop HTTP backend, and a Kotlin Android HTTP backend. Both backends already page folder contents and keep review decisions separate from the disposable SQLite folder index.

The old SIFT scanner is a Go CLI with no OCR library dependency: it invokes `tesseract <file> stdout --psm 3 -l <lang>`, and video mode invokes `ffprobe` and `ffmpeg`. It scans JPEG, PNG, WebP, BMP, TIFF, HEIC, and GIF images; video support includes MP4, MKV, MOV, AVI, 3GP, and WebM. A meaningful word has at least three Unicode letters, the default threshold is two, and videos are sampled every 1, 2, or 5 seconds based on duration. Matches are copied or moved into `SIFT_docs`, preserving relative subfolders and adding a suffix when a destination exists.

The desktop app is packaged as a Python application and already uses Tesseract and FFmpeg as optional local executables for this feature. Android has no OCR dependency today. The application ID and local preference/state paths contain `photto`; changing the Android application ID would prevent an in-place upgrade.

## Goals / Non-Goals

**Goals:**

- Give desktop and Android users the same SIFT text-finding workflow and result/action contract.
- Keep OCR, recognized text, and all file operations local to the device.
- Keep the scan responsive with bounded work, progress reporting, cancellation, and reuse of unchanged OCR results.
- Put review and an explicit confirmation between OCR matches and copy/move operations.
- Make the GUI the only scanner interface while preserving a small `sift [DIR]` launcher.
- Preserve existing Photto users' marks and installation identity through the visible rebrand.

**Non-Goals:**

- Preserve the old Go scanner, terminal progress display, command-line JSON output, or OCR scan flags.
- Add cloud OCR, AI classification, or automatic file actions immediately after a scan.
- Guarantee identical OCR text across desktop and Android engines.

## Decisions

### 1. Keep one shared UI and expose OCR jobs through both existing backends

Add a `Find text` entry point to the shared UI. It starts an asynchronous job, shows progress and cancellation, and presents paged match results with a preview, recognized-text excerpt, match reason, and video timestamp when available.

The Python and Kotlin backends expose the same token-authenticated contract:

- `POST /api/text/start` accepts the selected root, recursion, image/video modes, language, threshold, and optional query; it returns a job id.
- `GET /api/text/status?jobId=...` returns state, counts, progress, and any engine/decode errors.
- `GET /api/text/results?jobId=...&cursor=...&limit=...` returns a bounded page of matches.
- `POST /api/text/cancel` stops pending work while retaining completed results.
- `POST /api/text/action` accepts selected relative paths and `copy` or `move` after the UI has shown the destination preview.

The browser remains platform-agnostic for job and result shapes. Platform-specific work stays behind the backends.

### 1a. Refresh the shared visual hierarchy without changing review behavior

Replace the dark brown, heavily rounded presentation with a calm, high-contrast light workspace. Use compact folder and file-type navigation, reserve the center for a stable media preview, and group the keep, favorite, delete, undo, and defer actions into a clear control rail. On narrow screens, put the folder identity and primary actions in a compact first row and allow file-type filters to scroll horizontally in a second row. Keep touch targets comfortable while reducing oversized controls and decorative effects. Apply the same visual language to OCR, review, and folder-picker panels so the interface remains coherent. Existing swipe directions, keyboard shortcuts, decision states, and OCR flow remain unchanged.

### 2. Use a local OCR adapter per platform

Desktop keeps the old SIFT Tesseract executable integration and the existing duration-based FFmpeg/FFprobe frame sampler. Missing Tesseract disables image OCR with a clear setup message; missing FFmpeg disables video OCR without treating videos as non-matches. No OCR process is started from command-line flags.

Android uses bundled ML Kit Text Recognition for the Latin script in the first release, and `MediaMetadataRetriever` to extract sampled video frames. The bundled model is present at install time and works without a model download; Google's current guide estimates about 4 MB per script. The unbundled alternative is smaller but downloads the model through Google Play services, so it does not meet the offline-first requirement. Additional scripts can be added as explicit model packages later. See [ML Kit Android text recognition](https://developers.google.com/ml-kit/vision/text-recognition/v2/android).

Each adapter returns recognized text and an engine identifier/version. A shared normalization step applies the same meaningful-word counting and case-insensitive matching rules on both platforms. OCR output can differ by engine; parity is defined by the common rules and fixtures, not byte-identical recognition.

### 3. Normalize search and threshold behavior

Use the old default of two meaningful words, where a meaningful word contains at least three Unicode letters. With no search query, a file must meet the threshold. With a non-empty query, the file matches when OCR text contains the query case-insensitively, even when it falls below the threshold. This makes targeted searches useful for a single identifier and gives images and videos the same semantics; the old Go image and video paths differ here.

Preserve old video sampling intervals: 1 second for clips under 15 seconds, 2 seconds for clips from 15 to under 60 seconds, and 5 seconds for clips of 60 seconds or longer. When a frame causes a match, retain its timestamp and a representative preview for the result.

### 4. Cache OCR results separately from user decisions

Add an OCR result table to each folder's disposable SQLite index keyed by relative path, file size, modification time, language, engine identifier/version, and video sampling settings. Store normalized recognized text and word counts so later searches can filter without decoding unchanged media again. A changed source or OCR signature causes a rescan. Deleting or rebuilding the index may discard OCR cache data; it must never discard `decisions.json` or existing review marks.

Keep this cache in app-private storage, do not include OCR text in logs, and provide a clear-cache action. Job state is transient; completed OCR cache entries remain reusable after an app restart.

### 5. Preview destinations and commit per file without overwrites

The results screen builds each destination under `<root>/SIFT_docs/<relative-parent>/<filename>`. It excludes `SIFT_docs` from future source scans. The user can select matches, choose copy or move, inspect the destination list, and confirm once. A collision gets a deterministic `_siftedN` suffix; existing files are never overwritten.

Copy to a temporary destination file and rename it into place only after the copy succeeds. Move with an atomic rename when source and destination share a filesystem; otherwise copy and verify the destination before removing the source. Report each outcome so interrupted or partial jobs are visible. Scan cancellation never starts these operations.

### 6. Rebrand the GUI while preserving upgrade identity

Change visible titles, icons, installers, commands, and release artifacts to SIFT. Keep Android `applicationId` and the current preference key stable so the new build upgrades the existing app and sees existing state. On desktop, migrate state from the Photto path only when the SIFT target path does not already exist; if both exist, show a choice and never overwrite either store. The thumbnail cache can be rebuilt under the new name.

The installed desktop launcher is `sift [DIR]`: a directory opens directly in the GUI and no argument opens the folder picker. Legacy scan flags such as `--video`, `--copy`, and `--json` produce a short migration message and perform no file operations. No separate Go scanner binary is built or released.

### 7. Replace the GitHub repository only after local release checks

Keep work and history in the current `laghab/photto` repository while implementing. At cutover, create a local mirror backup of the existing `laghab/sift` repository, delete that old remote as requested, rename `laghab/photto` to `laghab/sift`, update `origin`, and verify the README, releases workflow, and default branch on the renamed remote. Do not delete the old remote before all OCR behavior has been ported and validated locally.

## Risks / Trade-offs

- **OCR engines produce different text** → Normalize threshold/query rules, use a shared fixture with expected match outcomes, and record engine/version in the cache key.
- **Bundled Android model increases APK size** → Start with the Latin model only and measure final APK size; add scripts only when requested.
- **Desktop tools are absent** → Detect Tesseract and FFmpeg before a job starts, explain the missing component, and keep image OCR usable when only FFmpeg is missing.
- **OCR cache contains sensitive text** → Keep it app-private, omit it from logs and network routes unrelated to the current session, and let users clear it.
- **A move can fail partway through a large selection** → Preview exact destinations, never overwrite, use atomic destination creation, and report per-file outcomes.
- **Brand migration can strand marks** → Keep the Android application ID and preference key; migrate desktop state only when the target state path is absent.
- **Deleting the occupied GitHub name can interrupt access** → Complete the local mirror and release verification first, then perform the repository cutover in one sequenced operation.

## Migration Plan

1. Implement both local OCR adapters, the shared job/result/action contract, and the GUI without changing remote repositories.
2. Verify the desktop and Android OCR flows offline, including cancellation, cache invalidation, preview, copy, move, and collisions. Verify existing marks survive the brand change.
3. Update visible names and platform packaging. Keep compatibility identifiers and migrate desktop state only when safe.
4. Build and verify Linux, Windows, and Android release artifacts from this checkout.
5. Create a local mirror of `laghab/sift`, delete the old GitHub repository, rename `laghab/photto` to `laghab/sift`, update the local remote, and verify the new repository and releases.

Rollback before step 5 is a normal code revert; existing app data remains readable. After cutover, the local mirror preserves the old repository history if recovery is needed.
