# Tasks

## 1. OCR behavior fixtures and contract

- [x] 1.1 Add deterministic OCR fixtures with known text, low-text images, and nested folders; verify fixture metadata and expected meaningful-word counts.
- [x] 1.2 Add a short test video with text at known timestamps and verify desktop and Android samplers select frames at the 1/2/5-second duration intervals.
- [x] 1.3 Document the shared OCR match contract (default threshold, meaningful words, query behavior, supported file types, video sampling, and local-only processing) in `docs/sift-ocr.md`; verify each documented behavior has a fixture case.

## 2. Local OCR engines

- [x] 2.1 Implement the desktop Tesseract adapter with language selection, bounded subprocess concurrency, and clear missing-engine errors; verify known-text images and missing Tesseract behavior.
- [x] 2.2 Implement desktop FFmpeg/FFprobe frame sampling, cleanup of temporary frames, and unavailable-tool reporting; verify expected sample timestamps and an unreadable video error.
- [x] 2.3 Add bundled offline ML Kit Latin text recognition to Android and apply the shared normalization and meaningful-word counter; verify OCR works in airplane mode on the emulator.
- [x] 2.4 Implement Android video frame sampling with `MediaMetadataRetriever`; verify one video result includes the matching timestamp and representative frame.
- [x] 2.5 Verify image and video engines apply the same search and threshold rules, including a one-word search match below the default threshold.

## 3. OCR jobs and result cache

- [x] 3.1 Add a versioned OCR-result table to each folder index, keyed by path, source size/mtime, language, engine version, and sampling settings; verify unchanged files reuse results and changed signatures rescan.
- [x] 3.2 Add a user action to clear cached OCR text without changing review decisions; verify the cache is cleared while `decisions.json` and marks remain intact.
- [x] 3.3 Implement authenticated `/api/text/start`, `/api/text/status`, `/api/text/results`, and `/api/text/cancel` routes with paged results and the same response shape on both backends; verify route contracts and page bounds.
- [x] 3.4 Run OCR through a bounded cancellable worker pool and tie each job to its folder generation; verify a canceled or switched-folder job cannot write stale results.
- [x] 3.5 Report per-file decode and OCR failures separately from non-matches; verify a failed decoder does not silently exclude a video as having no text.

## 4. GUI discovery and review

- [x] 4.1 Add a SIFT “Find text” workflow to the shared UI with image/video selection, language, threshold, and text query controls; verify options reach both backends with matching defaults.
- [x] 4.2 Render paged OCR results with source preview, recognized-text excerpt, match reason, and video timestamp; verify large result sets do not render or request all thumbnails at once.
- [x] 4.3 Show scan progress and file counts, support cancellation, and retain completed results after cancellation; verify the UI remains responsive on the large fixture.
- [x] 4.4 Show actionable missing-tool, unsupported-file, permission, and partial-error states; verify images remain usable when only desktop video tools are unavailable.
- [x] 4.5 Refresh the shared GUI visual hierarchy and mobile review layout; verify folder/filter navigation, media preview, swipe actions, and OCR dialogs remain clear and usable at phone and desktop sizes.

## 5. Confirmed SIFT_docs actions

- [x] 5.1 Preview each selected match's destination under `SIFT_docs`, preserve nested relative paths, and exclude `SIFT_docs` from source scans; verify preview paths and scan exclusion.
- [x] 5.2 Implement explicitly confirmed copy and move operations with unique collision names and no overwrites; verify existing files are preserved and destinations use `_siftedN` suffixes.
- [x] 5.3 Use atomic destination creation and report per-file success/failure for interrupted, missing-source, and permission-error cases; verify a partial failure leaves unrelated files unchanged.
- [x] 5.4 Verify canceling OCR, closing results, and reviewing matches never changes source files before the user confirms an action.

## 6. SIFT identity, launcher, and data compatibility

- [x] 6.1 Replace user-visible Photto branding with SIFT in the UI, Android label, desktop window, icons, installers, release artifact names, and README; verify no user-facing release surface still advertises Photto.
- [x] 6.2 Provide `sift [DIR]` as a GUI launcher only; verify `sift DIR` opens that folder, `sift` opens the picker, and legacy OCR scan flags do not scan or alter files.
- [x] 6.3 Keep the Android application ID and preference key stable, and migrate desktop state only when the SIFT destination is absent; verify existing marks survive upgrade and conflicting state stores are never overwritten.
- [ ] 6.4 Update Linux, Windows, Android, and CI packaging to build the GUI under SIFT names without shipping the old Go scanner; verify each package artifact and launcher behavior.

## 7. Integration and repository cutover

- [x] 7.1 Run the Python test suite, syntax checks, and Linux API smoke test; verify all pass.
- [ ] 7.2 Run Windows packaging and smoke tests plus Android debug/release builds; verify installers and APKs use the SIFT brand and the Android OCR model works offline.
- [x] 7.3 Run desktop and Android end-to-end OCR workflows: search, threshold, video sampling, cancellation, cache reuse, preview, copy, move, collision handling, and existing review/trash behavior; verify expected per-file outcomes.
- [x] 7.4 Run `openspec validate --strict make-sift-gui` and review the final diff against this change's scope; verify no CLI scanning interface or cloud OCR was added.
- [ ] 7.5 Create a local mirror of `laghab/sift`, delete the old GitHub SIFT repository as requested, rename `laghab/photto` to `laghab/sift`, update `origin`, and verify the new remote's default branch, README, and release workflow.

## Workflow follow-up

- Archive the change after release review is complete.
- Verify the archived capability inventory includes text discovery, text result actions, and SIFT launch behavior.
