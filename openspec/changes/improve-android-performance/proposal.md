# Proposal

## Why

The Android app is effectively unusable on real photo libraries. Opening a folder walks the entire tree and serializes every file before the first card appears; every swipe then does work proportional to the whole folder; and Review decodes a thumbnail for every tile at once. The shared web UI also uses desktop-grade blur, grain, and shadow effects that are expensive on mid-range phone GPUs.

Desktop is usable. Android needs a performance rescue before cleanup mode, OCR, or AI-assisted sorting can be built on top of it.

## What Changes

- **Progressive folder loading.** Large folders show the first cards before the full tree has been scanned. A persisted per-folder index makes reopening fast, and a background revalidation keeps it fresh. Explicit refresh remains available.
- **Bounded file transfer.** The API serves file metadata in pages with total counts and scan state instead of one unbounded JSON payload. The deck and the review lists request only what they need.
- **Bounded thumbnail work.** Thumbnail generation gets a fixed concurrency limit, memory and disk caches, versioned URLs, right-sized decoding, and visible-first prioritization. Review no longer requests a thumbnail for every tile simultaneously.
- **A mobile rendering profile.** Android disables backdrop blur, the grain overlay, large blurred backgrounds, and heavy shadows. Gesture updates are coalesced to animation frames, animations use transform and opacity only, and only a bounded number of cards and media elements stay live.
- **Constant-time UI updates.** Filter counts, progress, and review lists update incrementally instead of filtering the full file list on every swipe or render.
- **Measured budgets.** Debug instrumentation and emulator checks cover first-card time, full-index time, swipe latency, Review open time, and thumbnail concurrency. The budgets become part of CI.
- **Shared UI, both backends.** The web UI stays shared with desktop, and the desktop Python backend keeps API parity with Android. This is not a native Compose rewrite.

Existing safety behavior is unchanged: marks are still saved per folder, nothing is deleted until Review is confirmed, and trashing still uses the system trash.

### Out of Scope

The following are deliberately deferred to follow-up changes:

- Cleanup mode with automatic categories such as tiny images, screenshots, messaging-app dumps, and text-heavy photos.
- OCR for photos and sampled video frames.
- Local AI classification or a local LLM, and an optional cloud API key. The agreed direction is local-first with a cloud key as an explicit later opt-in, but both need their own dependency and privacy design.

## Capabilities

### New Capabilities

- `folder-indexing`: progressive scanning, persisted per-folder index, paged metadata delivery, background revalidation, and scan-state reporting for large folders.
- `thumbnail-delivery`: bounded, cached, prioritized preview generation and delivery for decks and review grids.
- `mobile-deck-ui`: responsive swipe deck, bounded review rendering, Android-specific visual profile, and per-action UI work that does not scale with folder size.
- `performance-verification`: debug timing instrumentation, reference-device budgets, CI enforcement, and failure diagnostics that make performance regressions visible.

### Modified Capabilities

None. There are no existing specs in this repository.

## Impact

- **Android backend:** `android/app/src/main/kotlin/io/github/laghab/photto/Api.kt`, `MiniServer.kt`, and `MainActivity.kt` for indexing, paging, thumbnail scheduling, and debug instrumentation.
- **Shared web UI:** `static/app.js`, `static/style.css`, and `static/index.html` for progressive loading, incremental counts, bounded review rendering, gesture handling, and the Android visual profile.
- **Desktop backend:** `photto.py` for API parity on paging, index caching, scan state, and thumbnail cache headers so the shared UI behaves consistently.
- **Verification:** `android/ci/emulator_checks.py` and `.github/workflows/build.yml` for performance budgets and regression checks.
- **Compatibility:** no new third-party UI framework, no changes to the on-disk decision format, and no migration required. Existing marks and trash behavior remain valid.
