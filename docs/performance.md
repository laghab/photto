# Android performance

This document is the shared contract for the paged folder index, the thumbnail
pipeline, and the performance checks. `photto.py` (desktop) and
`android/.../Api.kt` must implement the same fields and semantics so
`static/app.js` never branches on platform for API shape.

## Why this exists

The original Android flow walked the whole folder and serialized every file
before the first card appeared. Every swipe then filtered and sorted the full
list in JavaScript, and Review requested one thumbnail per marked tile at once.
On a real library that is tens of thousands of files. The index and the paged
API turn those unbounded operations into bounded ones.

## Large-folder fixture

`tools/make_fixture.py` creates a deterministic folder with 3,000 tiny valid
PNGs plus video, audio, PDF, text, Office, and archive files:

```bash
python3 tools/make_fixture.py /tmp/photto-large --json
python3 tools/make_fixture.py /tmp/photto-large --images 5000 --seed 7 --force
```

The default fixture has 3,011 files (3000 images + 11 mixed files). It refuses
to write into a non-empty directory unless `--force` is passed. The same
generator feeds `tests/` and `android/ci/emulator_checks.py`.

## Paged index API

### `GET /api/files`

| Parameter | Default | Meaning |
|---|---|---|
| `cursor` | empty | Opaque keyset cursor. Takes precedence over `offset`. |
| `offset` | `0` | Random-access fallback and compatibility. Ignored when `cursor` is present. |
| `limit` | `200` | Page size. Clamped to `MAX_PAGE_SIZE` (500). |
| `sort` | `shuffle` | `shuffle`, `newest`, `oldest`, `largest`, `name`. |
| `group` | `all` | `all`, `photos`, `videos`, `audio`, `docs`, `other`. |
| `decision` | `none` | `none`, `keep`, `delete`, `favorite`, `any`. |
| `revision` | empty | Client's current index revision, for reconciliation. |
| `refresh` | `0` | `1` forces a new scan generation before the page is served. |

Response:

```json
{
  "root": "/storage/emulated/0/DCIM",
  "name": "DCIM",
  "recursive": true,
  "truncated": false,
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
    "byGroup": { "photos": 15000, "videos": 2000, "audio": 100, "docs": 200, "other": 1132 },
    "undecidedByGroup": { "photos": 14000, "videos": 1900, "audio": 100, "docs": 180, "other": 940 },
    "bytesByDecision": { "keep": 812000000, "delete": 340000000, "favorite": 90000000 }
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
  "nextCursor": "eyJ2IjoxLCJzIjoibmV3ZXN0IiwiayI6WzE3NjAwMDAwMDAsIkNhbWVyYS9JTUdfMDAwMS5qcGciXX0",
  "version": "1.1.0",
  "platform": "android"
}
```

Rules:

- `files` contains at most `effectiveLimit` entries.
- A limit above 500 is clamped; the response reports the effective value.
- An offset at or past `total` returns an empty page, not an error.
- `nextCursor` is `null` when the page is the last one for the current index
  revision.
- `counts` always describe the whole folder, not the filtered page.
  `bytesByDecision` sums `size` per mark so Review can show freed space
  without loading every marked row into the client.
- `total` is the number of indexed files. `scan.scanned` is the number of files
  observed by the current scan generation.
- `truncated` is true when the folder exceeded `MAX_FILES` (100,000).

### `GET /api/scan`

Returns the same `root`, `name`, `recursive`, `truncated`, `revision`, `scan`,
`counts`, `version`, and `platform` fields without file rows. The UI polls this
while a scan is running instead of reloading a page.

### Cursors

The cursor is URL-safe base64 of UTF-8 JSON:

```json
{ "v": 1, "s": "newest", "k": [1760000000.0, "Camera/IMG_0001.jpg"] }
```

| Sort | SQL order | Cursor key |
|---|---|---|
| `shuffle` | `shuffle ASC, rel ASC` | `[shuffle, rel]` |
| `newest` | `mtime DESC, rel ASC` | `[mtime, rel]` |
| `oldest` | `mtime ASC, rel ASC` | `[mtime, rel]` |
| `largest` | `size DESC, rel ASC` | `[size, rel]` |
| `name` | `sort_name ASC, rel ASC` | `[sort_name, rel]` |

`sort_name` is the lowercase filename with digit runs zero-padded to 20 digits,
so `IMG_2` sorts before `IMG_10`. `shuffle` is a random rank assigned once when
a file enters the index and preserved across revalidation. The cursor is invalid
if its sort does not match the request; the server then restarts from the first
page instead of returning an error.

Keyset paging is required for the deck and Review because deciding a file
removes it from the filtered result set; offset paging would skip the following
file. Offset paging remains for random access and compatibility.

### Scan states

| State | Meaning |
|---|---|
| `idle` | No scan has started. |
| `scanning` | A scan generation is walking the folder. Pages may be partial. |
| `complete` | The latest generation finished; rows not seen were removed. |
| `error` | The scan stopped with readable results retained; `error` has a message. |
| `permission` | The app cannot read storage; cached rows are not actionable. |

`revision` is a monotonically increasing integer written at scan start and at
each committed batch. A client that sees a different revision than the one it
loaded must reconcile: keep the current top card if it still exists, merge new
files, and reload page 0 for sorted views.

### Decisions

`decisions.json` (desktop) and the Android state JSON remain the source of
truth. The index `decision` column is a query cache:

- On open, the index replaces its decision column with the JSON map.
- `/api/decide`, `/api/reset`, and `/api/commit` update both stores.
- After a completed scan, decisions for paths that no longer exist are removed
  from JSON and the index. Decisions for existing files are never dropped.
- A missing, corrupt, or version-mismatched index is deleted and rebuilt; the
  JSON file is untouched.

### Thumbnail versioning

`GET /thumb`, `/view`, `/raw`, and `/pdfpage` accept an optional `v` parameter.
The UI sends `v=<epoch_ms>-<size>`, where `epoch_ms` is the file's modification
time in milliseconds and `size` is its byte length. Both backends:

- include the version (or the stat-derived equivalent) in the cache key.
- return `Cache-Control: private, max-age=31536000, immutable` when `v` is
  present and the response is a generated thumbnail.
- retain the short `max-age=300` behavior when `v` is absent.
- ignore the literal value for generation; the file stat remains authoritative.

A changed file therefore gets a new URL and bypasses stale WebView and disk
caches. An unchanged file is served from the memory/disk cache without decoding
the source again.

### Connection reuse

`MiniServer` sends `Connection: close` on every response, so each request opens
a fresh loopback TCP connection. Measured in the WebView on the reference
emulator after the index completed, with 50 sequential `/api/ping` requests:

| Samples | Median | p95 | Max |
|---:|---:|---:|---:|
| 50 | 9.2 ms | 14.5 ms | 16.8 ms |

That includes the connect, request, response, and close. The UI polls
`/api/scan` every few seconds and otherwise serves swipes from the loaded page,
so setup cost is a negligible fraction of the work. Keep-alive is intentionally
not added: it would require connection lifecycle, idle timeout, and shutdown
handling in both backends for no measured user-facing gain. Revisit only if a
future workload issues many small requests in a burst
(`test-output/keepalive.json`).

## Thumbnail pipeline

```
request /thumb?p&v
   -> memory LRU -> serve
   -> disk cache -> memory LRU -> serve
   -> priority queue -> bounded workers (2..4) -> decode at target -> encode -> cache -> serve
```

Priority: top card and visible Review tiles, then the next deck card, then
near-visible Review tiles, then offscreen prefetch. Requests are deduplicated by
cache key. A card or tile that leaves view is cancelled or deprioritized, and
its media source is released.

Android uses `ImageDecoder`/`BitmapFactory` with a target size, a memory LRU
sized from the activity memory class, and the existing disk cache with a size
cap. Python uses a bounded executor/semaphore around its external tools and the
same versioned cache headers.

## Verification

### Desktop

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_index.py` covers page bounds, clamping, empty pages, every sort
order, cursor stability, counts, revalidation, and decision continuity.
`tests/test_api.py` covers the HTTP shapes against a temporary folder.

For a large-folder UI pass on a Linux desktop with GTK/WebKit2GTK:

```bash
python3 tools/desktop_check.py
```

It copies the fixture, starts `photto.py`, and drives progressive load, keyboard
decisions, undo/skip, filters, sorts, a 300-mark paged Review, favorites move,
and the confirm-to-trash flow. On 2026-11-06 it passed 19/19 checks: the first
page arrived in 9.6 ms while scanning and the full index took 222.7 ms. Results
are written to `test-output/desktop.json` and `test-output/desktop-results.txt`.

To compare the two backends on the same folder (requires a running emulator
with the debug APK installed):

```bash
python3 tools/parity_check.py
```

It regenerates the fixture, stages the identical tar archive on Android and a
temporary desktop folder, resets both decision stores, marks the same file
kept, and compares every row for all sort and filter combinations plus the
counts object. On 2026-11-06 it passed 14/14 checks with 3,011 identical rows;
results are written to `test-output/parity.json`.

### Android emulator

`android/ci/emulator_checks.py` generates the large fixture, pushes it as a
single archive, and measures:

| Metric | Budget |
|---|---|
| First card (3,011-file fixture) | < 2.5 s |
| Full index (3,011-file fixture) | < 20 s |
| Swipe to next card (decision to interactive) | < 150 ms |
| Review open with 1,000 marks | < 1.5 s |
| Concurrent thumbnail decodes | <= 4 |

These are the ratified budgets. They are regression ceilings on the reference
emulator, not device-independent promises; a run that crosses one fails CI.
The harness writes `test-output/perf.json` with every measured value, plus
logcat and screenshots on failure. Debug builds expose timings at
`/api/debug/perf` and through `window.__phottoPerf`; release builds have no
endpoint and no network calls.

### Baseline

The baseline is the pre-change build on the same API 34 / Pixel 6 emulator with
software rendering. Methodology:

1. `tools/make_fixture.py` produces the 3,011-file fixture.
2. The fixture is pushed to `/sdcard/PhottoTest`.
3. The debug APK is launched with `photto.test.root` and a fixed token/port.
4. First card: time from launch to the first `.card` with non-zero size.
5. Full scan: time until `/api/files` reports `scan.state == "complete"` and
   `total == 3011`.
6. Swipe to next: measured in the WebView between `pointerup` and the next
   `.card.top` becoming interactive.
7. Review open: time between the Review button and the first tile.
8. Concurrency: maximum simultaneous thumbnail decodes reported by the debug
   counter.

The software-rendered emulator is used for regression trends and correctness,
not absolute frame-rate claims. Record real-device results separately when
available.

### Post-change results (2026-11-06)

Measured on the same API 34 / Pixel 6 AVD and the 3,011-file fixture. The
workstation emulator used hardware-backed rendering (`-gpu host`) because
`swiftshader_indirect` crashed the host RenderThread here; CI keeps
`-gpu swiftshader_indirect`. The baseline above used software rendering, so
treat the deltas as direction rather than an absolute device claim. The final
emulator run passed all 117 checks.

| Metric | Result | Budget |
|---|---:|---:|
| First card | **1.384 s** | < 2.5 s |
| Full index | **6.536 s** | < 20 s |
| Swipe to next card | **8.4 ms** | < 150 ms |
| Review open, 1,000 marks | **74.6 ms** | < 1.5 s |
| Maximum concurrent thumbnail decodes | **4** | <= 4 |

The first card appeared before indexing completed; the client-side first card
measurement was 172.6 ms after the first page was available. The full deck walk
decided 3,010 undecided files with 0 duplicates and at most 3 live cards. Warm
open served all 3,011 counts and a 200-row page while revalidation was still
scanning (1.67 s to the page, 996 ms in the server). Review rendered 100 tiles
and loaded 17 thumbnails initially; scrolling appended the next page and
offscreen tiles stayed unloaded. The 16-request burst peaked at 4 decodes, and
repeat requests hit the memory/disk cache. A changed file gets a new
`<mtime_ms>-<size>` version and a new thumbnail
(`test-output/changed-version.json`). Revalidation, Review undo, and confirmed
trash were also exercised. These emulator values are regression measurements,
not real-device frame-rate claims.

The CI budgets match the measurements above and remain deliberately looser
than this single run to tolerate emulator variability. The provisional-budget
status is resolved; the table above records the ratified thresholds.

## Baseline results (pre-change, 2026-11-05)

Measured on the pre-change debug APK (`versionName 1.1.0`, built from
`d3c0203`) with the 3,011-file fixture. The harness drove a Pixel 6 AVD
(`sdk_gphone64_x86_64`, API 34, `google_apis;x86_64`, software rendering),
installed with `-g`, granted `MANAGE_EXTERNAL_STORAGE` via `appops`, and
launched with:

```bash
adb shell am start -n io.github.laghab.photto/.MainActivity \
  --es photto.test.token tt --ei photto.test.port 8799 \
  --es photto.test.root /sdcard/PhottoTest
```

The fixture was pushed as one tar archive and extracted on-device. Timings
used the WebView DevTools socket for in-page measurements and the forwarded
HTTP port for server-side measurements. Values are one run on an emulator;
use them for regression direction, not absolute device claims.

| Metric | Baseline | Notes |
|---|---:|---|
| First card | **6.10 s** | launch to first `.card` with non-zero size |
| Full scan (UI) | **6.10 s** | launch until `S.files.length == 3011`; equal to first card because the UI blocks on one full response |
| Server scan + JSON | **1.84 s / 1.40 s** | two cold `/api/files` requests; the old backend rescans on every request |
| Swipe to next card | **7.5 ms** | in-page `pointerup` to the next top card rendered, with 3,011 files in memory |
| Review open, 1,001 marks | **55.6 ms** | build 1,001 tiles; 1,001 `<img>` elements created and `src` assigned eagerly |
| Review thumbnails | 121 / 148 complete at 1 s / 3 s | `loading="lazy"` deferred offscreen fetches, but there was no priority or cancellation |
| Client reload with marks | **1.19 s** | `/api/files` + client filtering and deck build |
| Thumbnail burst | 16 requests in **0.037 s** | no limiter or counter in the old build; 32x24 PNGs bypassed decoding via the old "under 1.5 MB" shortcut |
| Process threads during burst | 72 | observed with `ps -T` |

What the baseline means:

- The first-card problem is the full-folder `/api/files` response and the
  client's full-list work, not Android disk traversal: the server-side walk
  is only about 1.4-1.8 s of the 6.1 s.
- Swiping is cheap at 3,000 files but `buildDeck()` filters and sorts the whole
  list, and `updateChrome()` scans it six more times; those costs grow with
  the folder. Paging removes them from the swipe path.
- Review's own DOM build is fast at 1,001 tiles, but it eagerly creates every
  tile and image element. Offscreen fetch behaviour currently depends on the
  browser's lazy-loading heuristic, not on photto's priority queue.
- The old build has no thumbnail bound or debug counter. The 16-request burst
  shows the server accepts unbounded concurrent thumbnail handlers; a real
  decode of large images would overlap without limit.

The post-change results above ratify these budgets. The first-card target
(< 2.5 s) and the concurrency cap (<= 4) are the two budgets the baseline did
not already satisfy.
