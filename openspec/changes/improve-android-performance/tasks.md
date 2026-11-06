# Tasks

## 1. Baseline, contract, and test scaffolding

- [x] 1.1 Add a repeatable large-folder fixture generator for Android and desktop: at least 3,000 small images plus a mix of video, audio, PDF, text, and archive files. Verify it reports the expected file count and total size.
- [x] 1.2 Capture baseline timings on the current Android build using the fixture: first card, full scan, swipe-to-next, Review open, and thumbnail concurrency. Record the methodology and numbers in `docs/performance.md`.
- [x] 1.3 Write the paged index API contract in `docs/performance.md`: query parameters, response fields, cursor semantics, scan states, and thumbnail versioning. Verify both backend implementations reference the same field names.
- [x] 1.4 Add the Python test package with `tests/test_index.py` and a temp-folder helper. Verify `python3 -m unittest discover -s tests` runs and fails on a deliberately broken assertion.
- [x] 1.5 Add the Python test command to `.github/workflows/build.yml`. Verify the command passes locally and the workflow step is present.

## 2. Desktop index and paged API

- [x] 2.1 Add a SQLite-backed folder index module with the schema from `design.md`, atomic creation, schema versioning, and rebuild-on-mismatch. Verify a unit test creates, reopens, and rebuilds an index without touching `decisions.json`.
- [x] 2.2 Implement the background scan with generation tracking, revision increments, `seen` cleanup, cancellation, and partial-error state. Verify tests cover a completed scan, a cancelled scan, and a folder with an unreadable subtree.
- [x] 2.3 Implement cursor queries for shuffle, newest, oldest, largest, and name, plus group and decision filters and per-revision counts. Verify tests page every sort order with no duplicates or omissions.
- [x] 2.4 Wire `/api/files` and the new `/api/scan` route to the index, including page clamping, empty pages past the end, and permission/no-folder responses. Verify with an HTTP-level test against a temporary folder.
- [x] 2.5 Keep the decisions JSON authoritative while syncing the index decision column on decide, reset, revalidation, and commit. Verify tests cover decide, undo, reset, and commit with index and JSON in agreement.
- [x] 2.6 Add revalidation tests for added, removed, and changed files, including decisions outside the loaded page. Verify stale decisions are removed only when the file is gone.
- [x] 2.7 Wire the desktop thumbnail path to a bounded worker/semaphore and add version-aware cache headers. Verify a burst of concurrent requests never exceeds the configured limit.

## 3. Android index and paged API

- [x] 3.1 Add the framework SQLite index with the same schema, schema version, WAL mode, and decisions sync. Verify by opening, closing, and rebuilding an index on the emulator without losing marks.
- [x] 3.2 Implement the Android background scan executor with generation tracking, revision increments, `seen` cleanup, cancellation on folder switch, and permission/error states. Verify the emulator probe shows `scanning`, `complete`, and `error` transitions.
- [x] 3.3 Implement the same cursor queries, filters, and counts as the desktop backend. Verify the emulator returns the same page contents and ordering for an equivalent fixture.
- [x] 3.4 Wire `/api/files`, `/api/scan`, `/api/decide`, `/api/reset`, and `/api/commit` to the index while preserving current responses for small folders. Verify existing emulator checks still pass.
- [x] 3.5 Add emulator checks for paging, warm-open reuse, revalidation, and decision continuity. Verify the checks fail if the index is ignored or the cursor repeats a file.
- [x] 3.6 Verify the scanner stops cleanly when the activity is destroyed or storage permission is revoked, with no writes from the old generation.

## 4. Thumbnail pipeline

- [x] 4.1 Add the Android priority queue, bounded worker pool, keyed dedupe, and cancellation. Verify debug counters show at most the configured concurrent decodes during a large Review.
- [x] 4.2 Add the Android memory LRU and versioned disk cache, right-sized decoding, and removal of the "serve the original if under 1.5 MB" shortcut. Verify repeat requests avoid re-decoding and changed files produce a new version.
- [x] 4.3 Defer video and audio `src` assignment until a card is on top, and release media on teardown. Verify behind cards do not start playback or buffer and are released after a swipe.
- [x] 4.4 Add the Python thumbnail concurrency bound and the same versioned cache headers. Verify concurrent thumbnail requests respect the limit and repeat requests hit the cache.
- [x] 4.5 Update the shared UI to request versioned thumbnails and to request them only for visible or near-visible cards and tiles. Verify the network probe shows no requests for far-offscreen Review tiles.
- [x] 4.6 Extend the emulator checks for thumbnail concurrency, cache headers, repeat requests, and failure placeholders. Verify the checks report the observed values.

## 5. Shared UI: paged deck, counts, and Review

- [x] 5.1 Refactor the client state from a full file list to a paged deck with counts, cursor, revision, and scan state. Verify the first page renders and displayed counts match the server for the fixture.
- [x] 5.2 Implement deck refill from `nextCursor`, duplicate-path filtering, and low-water-mark prefetch. Verify swiping through the full fixture produces no skipped or repeated files.
- [x] 5.3 Update decide, undo, and skip to adjust counts optimistically and reconcile with the server on failure. Verify counts and server decisions agree after a mixed sequence of actions.
- [x] 5.4 Reset the deck and cursor on filter or sort changes, and reconcile when the index revision changes during a scan. Verify no duplicate cards and no stale cards after revalidation.
- [x] 5.5 Convert Review to paged rendering with an `IntersectionObserver`, lazy tile images, and incremental append. Verify a 1,000-mark Review opens promptly and only visible thumbnails are requested.
- [x] 5.6 Keep Review tile undo and the favorites/kept tabs correct with paging and cursor updates. Verify an undone tile disappears, counts drop, and the next page does not skip an entry.
- [x] 5.7 Remove remaining full-list assumptions such as filtering all files in `updateChrome()` and `drawReview()`. Verify with a code search and a 3,000-file fixture that no per-swipe operation scans the whole set.
- [x] 5.8 Replace the blocking scanning overlay with a skeleton card and a non-blocking progress/status line. Verify a card is interactive before the full scan completes.
- [x] 5.9 Preserve the viewer, picker, keyboard controls, favorites move, confirm-to-trash flow, and desktop behavior. Verify the existing emulator checks and a manual desktop pass.

## 6. Mobile rendering and gestures

- [x] 6.1 Add the Android visual profile: hide grain, remove backdrop filters, hide the large blurred background, reduce shadows, and keep cheap gradients. Verify computed styles on the emulator show `backdrop-filter: none` and the grain hidden.
- [x] 6.2 Coalesce pointer movement into one `requestAnimationFrame` update and use `translate3d` for the dragged card. Verify a fast synthetic drag produces no more style updates than frames and the card follows the pointer.
- [x] 6.3 Add and remove `will-change` around the drag and fly-out animations, keeping animations on transform and opacity. Verify no element retains `will-change` after the animation ends.
- [x] 6.4 Bound live cards and media, preload the next one or two images, and avoid creating media for far-behind cards. Verify the deck never exceeds the configured card count during a long swipe run.
- [x] 6.5 Add `content-visibility` and containment to Review tiles and check that scrolling a large list does not paint offscreen tiles. Verify with the probe and a scroll test.
- [x] 6.6 Review the Android layout for touch: safe-area padding, control sizes, filter row overflow, and Review footer placement. Verify with phone-viewport screenshots and the emulator.
- [x] 6.7 Verify reduced-motion behavior on Android and confirm the decision still completes with animations disabled. Verify by enabling the system setting in the emulator and running a swipe.

## 7. Instrumentation, budgets, and documentation

- [x] 7.1 Add the debug-only Kotlin performance counters and `/api/debug/perf` endpoint for first page, first card, full index, swipe-to-next, Review open, queue depth, decodes, and max concurrency. Verify the endpoint returns values only in debug builds.
- [x] 7.2 Add JavaScript performance marks for first page, first card, swipe, and Review open, exposed through `window.__phottoPerf` in debug. Verify the emulator probe reads the values.
- [x] 7.3 Add the large-folder fixture and budget assertions to `android/ci/emulator_checks.py`, and save `perf.json`, logs, and screenshots on failure. Verify a passing run and a deliberately lowered budget both behave as expected.
- [x] 7.4 Ratify the provisional budgets from `design.md` after the baseline run and record the final numbers in `docs/performance.md` and the CI script. Verify the documented numbers match the assertions.
- [x] 7.5 Measure HTTP connection overhead in `MiniServer` and decide whether keep-alive is needed. Record the decision and evidence in `docs/performance.md`; do not add keep-alive without measured benefit.
- [x] 7.6 Document the Android performance model and the new API in `docs/performance.md`, and update `README.md` only where behavior or expectations changed. Verify the documented commands and measurements are reproducible.

## 8. Integration verification

- [x] 8.1 Run the full Android emulator suite on the large fixture: open, swipe through a page, open a 1,000-mark Review, undo, commit, and verify trashing. Verify every check passes and `perf.json` is within budget.
- [x] 8.2 Run a desktop large-folder pass: progressive load, keyboard decisions, filter/sort changes, Review, favorites move, and commit. Verify no regressions against the pre-change behavior.
- [ ] 8.3 Run the Linux and Windows build jobs and the existing Android emulator checks. Verify the shared UI and both backends remain compatible.
- [x] 8.4 Run `openspec validate --strict` on this change and confirm the final artifact set is complete. Verify the command exits successfully.
- [x] 8.5 Review the final diff to confirm no cleanup mode, OCR, AI, native-UI rewrite, or unrelated refactor slipped into this change. Verify the scope matches `proposal.md`.

## Workflow follow-up

- Archive the change after the project's review requirements are satisfied.
- Verify the archived specs and the main capability inventory after archiving.
