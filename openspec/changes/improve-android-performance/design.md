# Design

## Context

See `proposal.md` for motivation. The current system has one shared web UI and two backends: `photto.py` on desktop and a Kotlin HTTP server on Android. Both implement the same routes and JSON shape. The UI currently receives every file in one `/api/files` response, keeps the full list in JavaScript, sorts and filters it in the browser, and requests thumbnails one URL at a time with no global limit. Android review renders every marked tile and requests every thumbnail at once.

Constraints that shape this design:

- Keep the shared web UI. No native Compose rewrite.
- Keep the API contract aligned between Python and Kotlin.
- No new third-party UI framework. The Python backend must remain standard-library only.
- Preserve the existing decision state format and trash behavior.
- Android `minSdk` is 30; the WebView is updatable, but mid-range GPU performance is the target.
- The CI emulator uses software rendering, so it is useful for consistency and regression detection, not for absolute frame-rate claims.

## Goals / Non-Goals

**Goals:**

- First card appears without waiting for a full folder scan.
- Warm opens reuse a persisted index.
- Page responses stay small and bounded.
- Swipe work does not scale with folder size.
- Thumbnail generation has a hard concurrency ceiling and visible-first priority.
- Review opens and scrolls with thousands of marked files.
- Performance budgets are measured and enforced in CI.
- Desktop behavior and the shared UI remain intact.

**Non-Goals:**

- Cleanup mode, OCR, AI classification, or cloud API integration.
- A native Android UI.
- Changing the decision vocabulary or the trash safety model.
- Instant detection of external file changes while the app is idle. Revalidation happens on open, on explicit refresh, and after commits.
- A general-purpose file index for other applications.

## Decisions

### 1. Store the index in SQLite; keep decisions in the existing JSON

Both platforms already have SQLite available: Python through `sqlite3` in the standard library, Android through the framework database API. A per-folder SQLite index gives paging, sorting, counts, persistence, and crash-safe updates without adding a dependency or inventing a custom binary format.

The existing `decisions.json` remains the source of truth for user marks. The index mirrors decisions in a column for query efficiency, but on every index open the JSON map is re-applied. If the DB is deleted or corrupted, it is rebuilt and the marks survive. A 100,000-file JSON snapshot would be slow to rewrite and awkward to query; SQLite avoids both problems.

Schema (per folder, in app-private state):

```sql
CREATE TABLE files (
  rel       TEXT PRIMARY KEY,   -- POSIX relative path
  name      TEXT NOT NULL,
  dir       TEXT NOT NULL,
  ext       TEXT NOT NULL,
  kind      TEXT NOT NULL,
  size      INTEGER NOT NULL,
  mtime     REAL NOT NULL,
  sort_name TEXT NOT NULL,      -- numeric-aware natural sort key
  shuffle   REAL NOT NULL,      -- stable random rank for the life of the index
  decision  TEXT,               -- NULL | keep | delete | favorite
  seen      INTEGER NOT NULL    -- scan generation, for deleting vanished rows
);

CREATE INDEX files_mtime   ON files(mtime, rel);
CREATE INDEX files_size    ON files(size, rel);
CREATE INDEX files_name    ON files(sort_name, rel);
CREATE INDEX files_shuffle ON files(shuffle, rel);
CREATE INDEX files_kind    ON files(kind, decision);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
```

`meta` stores schema version, canonical root, revision, scan state, scanned count, timestamps, and last error.

Alternatives considered:

- **JSON snapshot per folder.** Simpler, but 100k-object serialization/parsing on every warm open is exactly the cost we are removing, and paging would still require loading the whole file.
- **In-memory index rebuilt on every launch.** Fast to query, but loses the warm-open benefit and repeats the Android filesystem walk.
- **Index in the existing decisions JSON.** Couples rebuildable cache data to user state; a corrupt index would risk marks.

Use WAL mode where available, batch scan writes in transactions, and keep the DB in app-private storage. The DB is disposable cache data; the JSON remains authoritative.

### 2. Progressive scanning with revisions

Opening a folder starts (or resumes) a background scan. The scan walks the tree, upserts metadata in batches, and increments a revision at each batch boundary. Page reads use the current snapshot and include `scan.state` and `revision`.

```
open folder
    |
    +--> DB exists? -- yes --> serve page immediately, start revalidation
    |                    |
    |                    +--> revision N, state=scanning
    |
    +--> no ----------> create DB, start scan, serve partial pages as rows arrive
                         |
                         +--> page 0 may be empty at first; UI shows progress
```

Key behaviors:

- The scan is cancellable. Switching folders or losing permission cancels the current generation; results from the old folder are never mixed into the new one.
- A `seen` generation column avoids keeping a separate 100k-path set in memory. Rows not seen by the completed generation are deleted in the final transaction.
- A failed or partial scan keeps readable rows, sets `state=error`, and records the error. A retry starts a new generation.
- Warm open serves the existing DB before revalidation begins. The UI can show cards immediately and update when the revision changes.
- The default sort is shuffle, which is stable because `shuffle` is assigned once per file and preserved across revalidation. Sorted modes are provisional while scanning; the UI labels the list as still scanning and reloads page 0 when the scan completes. This avoids pretending that the first 200 scanned files are the newest 200 in the folder.

### 3. Cursor paging, server-side sort and filter

Offset paging is unsafe for a deck because deciding files removes them from the result set; the next offset then skips files. The UI uses opaque keyset cursors instead.

Each sort order has a stable key:

| Sort | Key |
|---|---|
| shuffle | `(shuffle, rel)` |
| newest | `(-mtime, rel)` |
| oldest | `(mtime, rel)` |
| largest | `(-size, rel)` |
| name | `(sort_name, rel)` |

The response includes `nextCursor` for the next page. The client passes it back. The server translates it into a `WHERE` clause, so concurrent decisions and revalidation cannot shift the page window. Offset remains supported for random access, but the deck and Review use cursors.

Filtering is server-side:

- `group=all|photos|videos|audio|docs|other`
- `decision=none|keep|delete|favorite|any`
- `sort=shuffle|newest|oldest|largest|name`

Counts are computed from SQL aggregate queries and cached per revision. The response carries total, undecided, per-decision, and per-group counts so the UI never recomputes them from a full list.

### 4. API contract

`GET /api/files` query parameters:

| Parameter | Default | Notes |
|---|---|---|
| `cursor` | empty | Opaque keyset cursor; takes precedence over offset |
| `offset` | 0 | Used for random access and compatibility |
| `limit` | 200 | Clamped to a configured maximum, e.g. 500 |
| `sort` | shuffle | One of the five orders |
| `group` | all | Type group |
| `decision` | none | Deck uses `none`; Review uses a decision value |
| `revision` | empty | Client's current revision, for reconciliation |
| `refresh` | 0 | `1` starts a new scan generation |

Response shape:

```json
{
  "root": "/storage/emulated/0/DCIM",
  "name": "DCIM",
  "recursive": true,
  "offset": 0,
  "limit": 200,
  "effectiveLimit": 200,
  "total": 18432,
  "revision": 37,
  "scan": {
    "state": "scanning",
    "scanned": 5120,
    "startedAt": 1760000000,
    "error": null
  },
  "counts": {
    "total": 18432,
    "undecided": 17120,
    "byDecision": { "keep": 900, "delete": 312, "favorite": 100 },
    "byGroup": { "photos": 15000, "videos": 2000, "audio": 100, "docs": 200, "other": 1132 }
  },
  "files": [
    {
      "path": "Camera/IMG_0001.jpg",
      "name": "IMG_0001.jpg",
      "dir": "Camera",
      "ext": "jpg",
      "kind": "image",
      "size": 2400000,
      "sizeH": "2.3 MB",
      "mtime": 1760000000.0,
      "decision": null
    }
  ],
  "nextCursor": "eyJ2IjoxLCJrIjpbMTc2MDAwMDAwMCwiQ2FtZXJhL0lNR18wMDAxLmpwZyJdfQ"
}
```

`GET /api/scan` returns the same `scan`, `revision`, and `counts` without file rows, for lightweight polling while the deck is already populated.

`GET /api/thumb` gains an optional `v` parameter. The UI passes a version derived from `mtime` and `size`. When `v` is present, the backend may return a long-lived immutable cache header; without it, the existing short cache behavior remains for compatibility.

`POST /api/decide`, `/api/commit`, `/api/root`, `/api/reset`, `/api/dirs`, `/api/open`, `/raw`, `/view`, `/text`, `/info`, `/pdfinfo`, and `/pdfpage` keep their current shapes. `/api/commit` updates the index's decision column alongside the JSON map.

### 5. Thumbnail pipeline

Both backends get a bounded, prioritized preview path:

```
request /thumb?p&v
      |
      +--> memory LRU -- hit --> serve bytes
      |
      +--> disk cache -- hit --> load into LRU, serve
      |
      +--> priority queue --> worker pool (2-4) --> decode at target size --> encode --> cache --> serve
```

Android:

- Replace the on-demand `ThumbnailUtils` call with a small pipeline. Read image bounds first (`BitmapFactory.Options.inJustDecodeBounds`) to choose a sample size, or use `ImageDecoder` with a target size.
- Use at most 2-4 concurrent decodes. Prefer 2 on low-memory devices. A `ThreadPoolExecutor` with a priority queue and a keyed dedupe map prevents duplicate work for the same file.
- Cache encoded thumbnail bytes in an `LruCache` sized from `ActivityManager.getMemoryClass()` (e.g. one eighth of the heap, capped). Clear on `onTrimMemory`.
- Keep the disk cache, but add a size/age cap and prune opportunistically. Include the version in the cache key.
- Do not return the original for images under 1.5 MB unless its decoded dimensions are already within the target. That shortcut is a major source of full-size decodes in Review.
- Defer video/audio `src` assignment until a card becomes the top card. Behind cards keep only the poster. This prevents three videos from buffering at once.

Python:

- Wrap thumbnail helpers in a shared bounded executor or semaphore so a large Review cannot spawn unlimited `ffmpeg`, ImageMagick, or LibreOffice processes.
- Apply the same versioned cache key and cache headers.

Priority levels:

1. Top card and currently visible Review tiles.
2. Next one or two deck cards.
3. Near-visible Review tiles.
4. Offscreen prefetch.

The UI only creates thumbnail requests for elements near the viewport. It does not request a thumbnail for every Review tile at once. When a card or tile leaves view, its request is dropped or deprioritized and its media is torn down.

### 6. Review rendering

Review becomes a paged list, not a full render:

- The panel requests `decision=delete|favorite|keep` with a cursor, sorted largest-first by default.
- It renders a page of tiles (e.g. 60-100) and appends the next page when the user approaches the end.
- Tiles use `loading="lazy"` and `content-visibility: auto` with a fixed intrinsic size. This bounds paint and layout even if the user scrolls through thousands of entries.
- A tile click removes the mark locally, updates counts, and posts the decision. The cursor remains valid because it is keyset-based.
- Closing Review cancels pending tile requests and releases tile images.

This avoids the current behavior where opening a 1,000-file delete pile creates 1,000 DOM tiles and 1,000 thumbnail requests.

### 7. UI state: remove the full-list assumption

The client keeps only what it is showing:

- `S.deck`: the current deck window plus the next page, not the whole folder.
- `S.counts`: server-maintained totals, updated optimistically on decide/undo.
- `S.cursor`: cursor for the current deck query.
- `S.review`: current Review page and cursor.
- `S.revision`: index revision used to detect revalidation changes.

`render()` already keeps three cards; that stays. `updateChrome()` stops filtering `S.files` and reads `S.counts`. `buildDeck()` no longer sorts the full list; the server returns the requested order. `drawReview()` stops filtering `S.files`; it renders fetched pages.

Deck refill:

```
decide/skip -> remove card -> if deck length < low-water mark
                               -> GET /api/files with S.cursor
                               -> append new files, skipping paths already in deck
```

When the server revision changes during a scan, the client preserves the current top card if it still exists, merges newly indexed files, and shows a subtle "new files found" state. Changing sort or filter resets the deck and cursor and fetches page 0.

### 8. Mobile rendering profile and gestures

Add a platform profile driven by `body[data-platform="android"]`:

- Hide the grain overlay.
- Remove `backdrop-filter` from filters, stamps, badges, viewer, and Review overlays; use opaque or semi-opaque solid backgrounds.
- Hide the `.media .blur` background image, or replace it with a solid tint derived from the file kind.
- Reduce shadows to cheap, small shadows.
- Keep gradients that are cheap; avoid full-screen radial gradients if profiling shows they matter.
- Add `contain: layout paint style` to cards and tiles where safe.
- Use `content-visibility: auto` and `contain-intrinsic-size` on Review tiles.

Gestures:

- Keep pointer events and the existing thresholds.
- Coalesce `pointermove` into one `requestAnimationFrame` update. Store the latest event coordinates; do not write styles per event.
- Use `translate3d` for the dragged card and update stamps in the same frame.
- Remove `will-change` at rest; add it only while dragging.
- Keep the decision animation on `transform` and `opacity`. Remove `will-change` after the animation.

Startup:

- Remove the blocking `.scanning` overlay from the stage.
- Show a slim progress/status line and a skeleton card, then replace the skeleton with the first card as soon as the first page arrives.
- Keep the welcome, permission, and picker states as they are.

### 9. Performance instrumentation and CI

Debug-only instrumentation:

- Kotlin: record `first_page_ms`, `first_card_ms`, `index_complete_ms`, `swipe_to_next_ms`, `review_open_ms`, `thumb_queue_depth`, `thumb_decodes`, and `thumb_max_concurrent`. Expose them at `/api/debug/perf` only in debug builds, and log a compact summary with the `photto-perf` tag.
- JavaScript: use `performance.mark`/`measure` for `photto:first-page`, `photto:first-card`, `photto:swipe`, and `photto:review-open`; expose `window.__phottoPerf` in debug. The existing emulator harness already evaluates JS through the WebView DevTools socket.

CI:

- Extend `android/ci/emulator_checks.py` with a generated large-folder fixture. Generate small PNGs on the host, push a tar/zip, and unpack on the device to avoid thousands of `adb push` calls.
- Measure first card, full index, swipe-to-next, Review open, and max concurrent thumbnail decodes.
- Provisional budgets, to be ratified after the first baseline run on the reference emulator:

| Metric | Provisional budget |
|---|---|
| First card, 3,000-file fixture | < 2.5 s |
| Full index, 3,000-file fixture | < 20 s |
| Swipe to next card (decision to interactive) | < 150 ms |
| Review open with 1,000 marks | < 1.5 s |
| Concurrent thumbnail decodes | <= 4 |

- Save timings, logs, screenshots, and the probe output on failure. The existing harness already saves screenshots and logcat; add a `perf.json` artifact and include failures in `results.txt`.
- Add non-performance checks that the Android profile is active: `body[data-platform="android"]`, grain hidden, and `backdrop-filter` computed as `none` on the filter bar.

Desktop:

- Add `tests/test_index.py` using `tempfile` and `unittest` to cover page bounds, counts, cursor stability, revalidation, and decision continuity.
- Run the test suite in CI alongside the existing Python syntax/build checks.

### 10. Cross-platform parity

Introduce a small shared contract document (in this change's design/spec, not a new service) and test both implementations against it:

- Same query parameters and response fields.
- Same cursor semantics and sort keys.
- Same scan states and revision behavior.
- Same thumbnail version parameter and cache headers.
- Same decision continuity after revalidation.

The shared UI must not branch on platform for API shape. Platform branching is limited to the visual profile and Android-specific file access.

## Risks / Trade-offs

- **Stale index vs. speed.** A warm open can show files that were removed or added since the last scan. → Serve immediately, revalidate in the background, expose `revision`, and remove decisions only for files that no longer exist. Keep an explicit refresh.
- **Provisional sorted views during scanning.** Newest/largest/name views can change when the scan completes. → Default to stable shuffle for progressive viewing; label sorted views as scanning and reload page 0 on completion.
- **SQLite adds a second state store.** A corrupt or stale index can disagree with `decisions.json`. → Treat the DB as disposable cache; re-apply decisions JSON on open; version the schema; rebuild on mismatch; never delete user marks on index errors.
- **Cursor complexity across five sort orders.** Each order needs a stable key and comparison. → Use one opaque cursor encoder with a version field and test each order for duplicates and omissions.
- **Thumbnail priority inversion.** Deck and Review compete for the same workers. → Priority queue, keyed dedupe, and cancellation on teardown; cap offscreen prefetch.
- **WebView GPU variability.** Some devices still jank with effects disabled. → Android profile removes the known expensive effects; test on the emulator and at least one real mid-range device; keep the profile behind one CSS class for easy tuning.
- **Software-rendered CI emulator.** Budgets may not reflect real-device frame rate. → Use CI for regression trends and correctness; record real-device measurements manually in the change verification notes.
- **Large Review DOM.** Paged tiles still accumulate as the user scrolls. → `content-visibility` bounds paint; if measurement shows DOM cost, add a windowed renderer in a follow-up. This is a deferrable open question.
- **API shape change.** An older client expecting all files in `/api/files` would break. → The UI and both backends ship together; there are no external API consumers. The response remains backward-compatible in field names, and the first page is returned by default.

## Migration Plan

1. Ship the new index and paged API behind the normal app update. No user action is required.
2. On first open after update, no index DB exists; the app scans and builds it. Existing `decisions.json` is loaded and applied to the new DB.
3. On later opens, the DB is reused and revalidated in the background.
4. If the DB is missing, corrupt, or from an older schema, delete and rebuild it. Decisions survive because they live in JSON.
5. Rollback: reverting the app removes the paged client and ignores the index DB. The JSON decision file is unchanged, so the previous version still reads the same marks. The index DB can be left in place or removed by the uninstaller.

## Open Questions

- Exact CI budget numbers after the first baseline run on the reference emulator.
- Whether HTTP keep-alive is needed in `MiniServer` once decode work is bounded, or whether localhost connection setup is negligible.
- Whether paged Review with `content-visibility` is sufficient for very large lists, or whether a windowed renderer is needed.
