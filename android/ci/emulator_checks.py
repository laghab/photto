"""Drives the photto APK on an Android emulator over adb (run in CI).

Writes screenshots to $OUT so a human (or Claude) can look at the real UI.
"""
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PKG = "io.github.laghab.photto"
APK = os.environ.get("APK", "app/build/outputs/apk/debug/app-debug.apk")
OUT = Path(os.environ.get("OUT", "test-output"))
OUT.mkdir(parents=True, exist_ok=True)
PORT, TOKEN = 8799, "tt"
BASE = f"http://127.0.0.1:{PORT}"
DEV = "/sdcard/PhottoTest"
LARGE_DEV = "/sdcard/PhottoLarge"
LARGE_EXPECTED = 3011
results = []
perf = {}


def _dump_logcat():
    if "--check-budgets" in sys.argv:
        return
    try:
        (OUT / "logcat.txt").write_text(subprocess.run(
            ["adb", "logcat", "-d", "-s", "photto:*", "photto-js:*", "AndroidRuntime:E"],
            capture_output=True, text=True, timeout=20).stdout)
    except Exception:  # noqa: BLE001
        pass


import atexit  # noqa: E402
atexit.register(_dump_logcat)


def _pick_serial():
    """Keep the suite on one device when a workstation has several emulators."""
    if os.environ.get("ANDROID_SERIAL"):
        return
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return
    serials = [line.split()[0] for line in out.splitlines()[1:]
               if line.strip().endswith("device")]
    if len(serials) == 1:
        os.environ["ANDROID_SERIAL"] = serials[0]
    elif len(serials) > 1:
        raise SystemExit("multiple adb devices online; set ANDROID_SERIAL to the one under test")


_pick_serial()


def adb(*args, check=True, capture=True):
    try:
        r = subprocess.run(["adb", *args], capture_output=capture, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print("adb timed out:", args)
        raise SystemExit(1)
    if check and r.returncode != 0:
        print("adb failed:", args, r.stdout, r.stderr)
        raise SystemExit(1)
    return r.stdout


def shot(name):
    with open(OUT / f"{name}.png", "wb") as f:
        f.write(subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True).stdout)
    print("screenshot", name)


def check(cond, msg):
    results.append((cond, msg))
    print(("PASS " if cond else "FAIL ") + msg, flush=True)


def budgets_from_env():
    def number(name, default):
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            raise SystemExit(f"{name} must be a number")
    return {"firstCardMs": number("PHOTTO_BUDGET_FIRST_CARD_MS", 2500),
            "fullIndexMs": number("PHOTTO_BUDGET_FULL_INDEX_MS", 20000),
            "swipeMs": number("PHOTTO_BUDGET_SWIPE_MS", 150),
            "reviewOpenMs": number("PHOTTO_BUDGET_REVIEW_MS", 1500),
            "maxConcurrent": number("PHOTTO_BUDGET_THUMBS", 4)}


def check_budget_file():
    """Re-apply the budget assertions to an existing perf.json (CI self-test)."""
    path = OUT / "perf.json"
    if not path.exists():
        raise SystemExit(f"no {path}; run the emulator checks first")
    data = json.loads(path.read_text())
    budgets = budgets_from_env()
    first = data.get("firstCardMs")
    full = data.get("coldFullIndexMs")
    swipe = data.get("swipeToNextMs")
    review = data.get("reviewOpenMs")
    thumbs = data.get("maxThumbConcurrent", 99)
    check(first is not None and first < budgets["firstCardMs"],
          f"first card within budget ({first} < {budgets['firstCardMs']} ms)")
    check(full is not None and full < budgets["fullIndexMs"],
          f"full index within budget ({round(full or 0)} ms)")
    check(isinstance(swipe, (int, float)) and swipe < budgets["swipeMs"],
          f"swipe within budget ({swipe} ms)")
    check(isinstance(review, (int, float)) and review < budgets["reviewOpenMs"],
          f"Review within budget ({review} ms)")
    check(thumbs <= budgets["maxConcurrent"],
          f"thumbnail concurrency within budget ({thumbs})")
    ok = all(c for c, _ in results)
    print(f"\n{sum(c for c, _ in results)}/{len(results)} budget checks passed")
    raise SystemExit(0 if ok else 1)


def req(path, body=None, headers=None, timeout=30):
    h = {"X-Token": TOKEN, **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read()


def req_json(path, body=None, timeout=60):
    return json.loads(req(path, body, timeout=timeout)[2])


def hget(headers, name):
    return next((v for k, v in headers.items() if k.lower() == name.lower()), "")


def page_all(sort="name", decision="any", group="all", limit=500):
    """Walk a keyset cursor to the end; returns (rows, last response)."""
    rows, cursor, last = [], None, None
    for _ in range(50):
        q = f"/api/files?limit={limit}&sort={sort}&group={group}&decision={decision}"
        if cursor:
            q += "&cursor=" + urllib.parse.quote(cursor, safe="")
        last = req_json(q)
        rows.extend(last["files"])
        cursor = last.get("nextCursor")
        if not cursor:
            return rows, last
    raise SystemExit(f"paging did not terminate for sort={sort}")


def natural(value):
    return re.sub(r"\d+", lambda m: m.group(0).zfill(20), value.lower())


def launch(root):
    """Force-stop and start the app; returns the host monotonic start time."""
    adb("shell", "am", "force-stop", PKG)
    started = time.monotonic()
    adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity",
        "--es", "photto.test.token", TOKEN, "--ei", "photto.test.port", str(PORT),
        "--es", "photto.test.root", root)
    adb("forward", f"tcp:{PORT}", f"tcp:{PORT}", check=False)
    return started


def wait_first_page(started, timeout=45, decision="none"):
    deadline = started + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = req_json(f"/api/files?limit=200&decision={decision}", timeout=5)
            if last.get("files"):
                return (time.monotonic() - started) * 1000, last
        except Exception:  # noqa: BLE001 - the server may not be listening yet
            pass
        time.sleep(0.1)
    raise SystemExit(f"no first page within {timeout}s: {str(last)[:300]}")


def wait_scan_complete(started=None, timeout=90):
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        try:
            last = req_json("/api/scan")
            if last["scan"]["state"] != "scanning":
                only = time.monotonic() - started if started else 0
                return only * 1000 if started else 0, last
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
    raise SystemExit(f"scan did not complete within {timeout}s: {last}")


def wait_client_scan(timeout=45):
    """Wait for the shared UI to finish reacting to a scan (it polls every 700 ms)."""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = js_json("JSON.stringify({state:S.scan&&S.scan.state,revision:S.revision,"
                       "deck:S.deck.length,exhausted:S.exhausted,error:S.scan&&S.scan.error})")
        if last.get("state") not in (None, "scanning"):
            return last
        time.sleep(0.3)
    raise SystemExit(f"client scan did not finish within {timeout}s: {last}")


def wait_client_deck(minimum=1, timeout=40):
    """Wait for the shared UI to have at least [minimum] cards ready to show."""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = js_json("JSON.stringify({deck:S.deck.length,els:S.els.size,exhausted:S.exhausted,"
                       "root:S.root,scan:S.scan&&S.scan.state})")
        if last.get("deck", 0) >= minimum or last.get("exhausted"):
            return last
        time.sleep(0.2)
    raise SystemExit(f"client deck did not fill within {timeout}s: {last}")


def client_reload(timeout=40):
    """Reload the page so it reconnects to the current session, then wait for cards."""
    js("location.reload(); 'ok'")
    time.sleep(0.6)
    return wait_client_deck(timeout=timeout)


def js(expr, timeout=15):
    """Evaluate JS in the app's WebView via its DevTools socket (debug builds only)."""
    try:
        import websocket  # websocket-client, installed by CI
        pid = adb("shell", "pidof", PKG, check=False).strip().split()[0]
        adb("forward", "tcp:9229", f"localabstract:webview_devtools_remote_{pid}", check=False)
        pages = json.loads(urllib.request.urlopen("http://127.0.0.1:9229/json", timeout=10).read())
        page = next(p for p in pages if p.get("url", "").startswith(BASE))
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=timeout, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                ws.close()
                r = m["result"]
                return r.get("result", {}).get("value", r)
    except Exception as e:  # noqa: BLE001
        return f"js() failed: {e!r}"


def js_json(expr, timeout=15):
    raw = js(expr, timeout=timeout)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


if "--check-budgets" in sys.argv:
    check_budget_file()


PROBE = """JSON.stringify({ua: navigator.userAgent, reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
 vw: innerWidth, vh: innerHeight,
 deck: (r => [r.x, r.y, r.width, r.height])(document.querySelector('#deck').getBoundingClientRect()),
 cards: [...document.querySelectorAll('#deck .card')].map(c => { const cs = getComputedStyle(c), r = c.getBoundingClientRect();
   return {cls: c.className, rect: [r.x, r.y, r.width, r.height], op: cs.opacity, vis: cs.visibility, disp: cs.display, tf: cs.transform,
           anims: c.getAnimations().map(a => [a.animationName || a.transitionProperty, a.playState, a.currentTime])}; }),
 hit: (e => e && (e.className || e.tagName))(document.elementFromPoint(innerWidth / 2, innerHeight * 0.45)),
 log: window.__plog || null})"""


def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def pdf(text):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    stream = f"BT /F1 24 Tf 40 340 Td ({text}) Tj ET".encode()
    objs[3] = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + (o if isinstance(o, bytes) else o.encode()) + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def tap_text(*labels):
    """Tap the first on-screen element whose text matches one of labels (via uiautomator)."""
    for _ in range(10):
        adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", check=False)
        xml = adb("shell", "cat", "/sdcard/ui.xml", check=False)
        for label in labels:
            m = re.search(r'text="%s"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"' % re.escape(label), xml, re.I)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                return label
        time.sleep(1)
    return None


# ---------------------------------------------------------------- fixtures
fx = OUT / "fixtures"
(fx / "sub").mkdir(parents=True, exist_ok=True)
(fx / "orange.png").write_bytes(png(300, 400, (255, 150, 60)))
(fx / "sub" / "teal.png").write_bytes(png(500, 300, (40, 180, 160)))
(fx / "doc.pdf").write_bytes(pdf("Hello from photto"))
(fx / "notes.md").write_text("# Notes\n\nThis is a text file on Android.\n")
(fx / "ocr").mkdir(exist_ok=True)
fixture_root = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sift"
for name in ("known.png", "single-word.png", "text-at-3s.mp4"):
    (fx / "ocr" / name).write_bytes((fixture_root / name).read_bytes())
for name in ("clip.mp4", "tone.mp3", "photo.jpg"):
    src = Path(os.environ.get("FIXTURES", ".")) / name
    if src.exists():
        (fx / name).write_bytes(src.read_bytes())

for _ in range(60):  # shared storage mounts a little after boot
    if subprocess.run(["adb", "shell", "touch /sdcard/.photto-ready && rm /sdcard/.photto-ready"]).returncode == 0:
        break
    time.sleep(2)
adb("shell", "rm", "-rf", DEV, check=False)
adb("shell", "mkdir", "-p", DEV)
for f in fx.rglob("*"):
    if f.is_file():
        rel = f.relative_to(fx).as_posix()
        adb("shell", "mkdir", "-p", f"{DEV}/{Path(rel).parent.as_posix()}")
        adb("push", str(f), f"{DEV}/{rel}")

# The large fixture is generated locally and pushed as one archive: pushing 3,000
# files individually takes minutes, while the tar is only a few hundred KB.
large = OUT / "large-fixture"
subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "tools" / "make_fixture.py"),
                str(large), "--force"], check=True)
tar_path = OUT / "photto-large.tar"
with tarfile.open(tar_path, "w") as tf:
    for f in sorted(p for p in large.rglob("*") if p.is_file()):
        tf.add(f, arcname=f.relative_to(large).as_posix())
(OUT / "fixture.json").write_text(json.dumps({
    "root": str(large), "files": sum(1 for p in large.rglob("*") if p.is_file()),
    "bytes": sum(p.stat().st_size for p in large.rglob("*") if p.is_file())}, indent=2))
adb("push", str(tar_path), "/data/local/tmp/photto-large.tar")
adb("shell", "rm", "-rf", LARGE_DEV, check=False)
adb("shell", "mkdir", "-p", LARGE_DEV)
adb("shell", "tar", "-xf", "/data/local/tmp/photto-large.tar", "-C", LARGE_DEV)
large_count = adb("shell", f"find {LARGE_DEV} -type f | wc -l").strip()
check(large_count == str(LARGE_EXPECTED), f"large fixture staged: {large_count} files")

# ------------------------------------------------- 1. no permission yet
adb("install", "-r", "-g", APK)
adb("shell", "appops", "set", "--uid", PKG, "MANAGE_EXTERNAL_STORAGE", "deny", check=False)
adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity", "--es", "photto.test.token", TOKEN,
    "--ei", "photto.test.port", str(PORT))
time.sleep(12)
adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")
st, h, body = req("/api/files")
check(json.loads(body).get("needsPermission") is True, "without access the app asks for permission")
time.sleep(2)
shot("01-permission")

# ------------------------------------------------- 2. with permission + folder
adb("shell", "appops", "set", "--uid", PKG, "MANAGE_EXTERNAL_STORAGE", "allow")
adb("shell", "am", "force-stop", PKG)
adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity", "--es", "photto.test.token", TOKEN,
    "--ei", "photto.test.port", str(PORT), "--es", "photto.test.root", DEV)
time.sleep(6)
adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")
req("/api/reset", {})  # deterministic marks for the small-folder checks

st, h, body = req("/")
check(st == 200 and b"SIFT" in body, "UI served from the APK")
st, h, body = req("/static/app.js")
check(h.get("Content-Type", "").startswith("text/javascript"), "app.js served with JS type")
st, h, body = req("/api/files?decision=any&limit=500&sort=name")
d = json.loads(body)
paths = sorted(f["path"] for f in d["files"])
print("files:", paths)
check(d.get("platform") == "android", "platform reported as android")
check("sub/teal.png" in paths and "doc.pdf" in paths, "recursive scan finds files")
kinds = {f["path"]: f["kind"] for f in d["files"]}
check(kinds.get("doc.pdf") == "pdf" and kinds.get("notes.md") == "text", "file kinds detected")

# Bundled ML Kit OCR must work offline, keep query matching independent from
# the word threshold, and report the sampled timestamp for a video match.
adb("shell", "svc", "wifi", "disable", check=False)
adb("shell", "svc", "data", "disable", check=False)
start = req_json("/api/text/start", {"images": True, "videos": False, "language": "eng", "threshold": 2, "query": "invoice"})
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    text_state = req_json(f"/api/text/status?jobId={start['jobId']}")
    if text_state.get("state") != "running": break
    time.sleep(.25)
text_page = req_json(f"/api/text/results?jobId={start['jobId']}&offset=0&limit=100")
query_job_id = start["jobId"]
query_results = text_page["items"]
check(text_state.get("state") == "done" and any(x["path"] == "ocr/single-word.png" for x in query_results),
      "bundled OCR works offline and a one-word query matches below threshold")
start = req_json("/api/text/start", {"images": True, "videos": False, "language": "eng", "threshold": 2, "query": ""})
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    text_state = req_json(f"/api/text/status?jobId={start['jobId']}")
    if text_state.get("state") != "running": break
    time.sleep(.25)
text_page = req_json(f"/api/text/results?jobId={start['jobId']}&offset=0&limit=100")
threshold_job_id = start["jobId"]
matches = {x["path"] for x in text_page["items"]}
check("ocr/known.png" in matches and "ocr/single-word.png" not in matches,
      "Android applies the default meaningful-word threshold")
check(text_state.get("cacheHits", 0) >= 2,
      f"unchanged Android OCR results are reused from cache ({text_state.get('cacheHits')})")

# Destination preview and confirmed actions preserve existing data and folder structure.
dest_dir = f"{DEV}/SIFT_docs/ocr"
adb("shell", "mkdir", "-p", dest_dir)
prior = fx / "ocr" / "prior.txt"; prior.write_text("keep this existing file\n")
adb("push", str(prior), f"{dest_dir}/single-word.png")
preview = req_json("/api/text/preview", {"jobId": query_job_id, "paths": ["ocr/single-word.png"]})
check(preview["destinations"] == [{"path": "ocr/single-word.png",
      "destination": "SIFT_docs/ocr/single-word_sifted1.png"}], "Android previews mirrored collision-safe destinations")
copied = req_json("/api/text/action", {"jobId": query_job_id, "paths": ["ocr/single-word.png"], "action": "copy"})
prior_contents = adb("shell", "cat", f"{dest_dir}/single-word.png").strip()
check(copied["copied"] == 1 and prior_contents == "keep this existing file",
      "confirmed copy preserves a colliding destination and reports success")
moved = req_json("/api/text/action", {"jobId": threshold_job_id, "paths": ["ocr/known.png"], "action": "move"})
source_exists = subprocess.run(["adb", "shell", "test", "-f", f"{DEV}/ocr/known.png"]).returncode == 0
check(moved["moved"] == 1 and not source_exists, "confirmed move mirrors nested paths on Android")
# Put the fixture back after asserting move semantics so the later, independent
# swipe/review smoke checks still run against their original small-folder set.
subprocess.run(["adb", "push", str(fx / "ocr" / "known.png"), f"{DEV}/ocr/known.png"],
               capture_output=True, check=True)
req_json("/api/files?refresh=1&limit=1")
_, refreshed_text_folder = wait_scan_complete(timeout=60)
text_folder_rows, _ = page_all()
check(refreshed_text_folder["counts"]["total"] == len(text_folder_rows) and
      not any(row["path"].startswith("SIFT_docs/") for row in text_folder_rows),
      "SIFT_docs is excluded from future source scans")

start = req_json("/api/text/start", {"images": False, "videos": True, "language": "eng", "threshold": 2, "query": "receipt"})
deadline = time.monotonic() + 90
while time.monotonic() < deadline:
    text_state = req_json(f"/api/text/status?jobId={start['jobId']}")
    if text_state.get("state") != "running": break
    time.sleep(.25)
text_page = req_json(f"/api/text/results?jobId={start['jobId']}&offset=0&limit=100")
video_match = next((x for x in text_page["items"] if x["path"] == "ocr/text-at-3s.mp4"), None)
check(video_match is not None and video_match["timestamp"] == 3,
      f"Android video OCR returns its sampled matching timestamp ({video_match or text_page})")
adb("shell", "svc", "wifi", "enable", check=False)
adb("shell", "svc", "data", "enable", check=False)

for p in [x for x in ("orange.png", "doc.pdf", "clip.mp4", "photo.jpg") if x in paths]:
    try:
        st, h, body = req(f"/thumb?p={p}")
        check(st == 200 and h.get("Content-Type", "").startswith("image/") and len(body) > 100, f"thumbnail for {p}")
    except Exception as e:  # noqa: BLE001
        check(False, f"thumbnail for {p} ({e})")
if "clip.mp4" in paths:
    st, h, body = req("/raw?p=clip.mp4", headers={"Range": "bytes=0-99"})
    check(st == 206 and len(body) == 100, "video byte ranges (seeking)")
    st, h, body = req("/info?p=clip.mp4")
    check("duration" in json.loads(body), "video duration via MediaMetadataRetriever")
st, h, body = req("/pdfinfo?p=doc.pdf")
check(json.loads(body).get("pages") == 1, "PDF page count")
st, h, body = req("/pdfpage?p=doc.pdf&n=1")
check(st == 200 and len(body) > 1000, "PDF page render")
st, h, body = req("/api/dirs")
places = [p["name"] for p in json.loads(body)["places"]]
check("Phone" in places, f"picker places {places}")

req("/api/decide", {"path": "notes.md", "decision": None})  # deterministic start
time.sleep(1)
adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", check=False)
shot("02-deck")
print("PROBE deck:", js(PROBE), flush=True)
print(js("""window.__plog = []; for (const t of ['pointerdown','pointermove','pointerup','pointercancel','touchstart','touchend','touchcancel'])
  addEventListener(t, e => { if (__plog.length < 60) __plog.push(t + ':' + Math.round(e.clientX ?? e.touches?.[0]?.clientX ?? -1) + ':' + Math.round(e.clientY ?? -1) + ':' + ((e.target && e.target.className) || '')); }, true);
addEventListener('error', e => __plog.push('ERR:' + e.message + '@' + e.lineno));
addEventListener('unhandledrejection', e => __plog.push('REJ:' + (e.reason && (e.reason.stack || e.reason.message) || e.reason)));
const of = window.fetch; window.fetch = (u, o) => { if (o && o.method === 'POST') __plog.push('POST ' + u + ' ' + o.body);
  return of(u, o).then(r => { __plog.push('RES ' + u + ' ' + r.status); return r; }, e => { __plog.push('FETCHERR ' + u + ' ' + e); throw e; }); };
new MutationObserver(() => __plog.push('TOAST ' + document.querySelector('#toast').textContent)).observe(document.querySelector('#toast'), {childList: true});
for (const n of ['decide', 'flyOut', 'onTap', 'skip']) { const f = window[n]; if (typeof f === 'function') window[n] = function (...a) { __plog.push('CALL ' + n + ' ' + JSON.stringify(a)); try { return f.apply(this, a); } catch (e) { __plog.push('THROW ' + n + ' ' + e.stack); throw e; } }; }
'ok'"""))

# ------------------------------------------------- 3. real swipe gesture
size = re.search(r"(\d+)x(\d+)", adb("shell", "wm", "size")).groups()
w, hgt = int(size[0]), int(size[1])
before = {f["path"] for f in json.loads(req("/api/files?decision=any&limit=500")[2])["files"] if f["decision"]}
adb("shell", "input", "swipe", str(int(w * 0.5)), str(int(hgt * 0.45)), str(int(w * 0.1)), str(int(hgt * 0.46)), "450")
time.sleep(1.5)
after = {f["path"]: f["decision"] for f in json.loads(req("/api/files?decision=any&limit=500")[2])["files"] if f["decision"]}
swiped = [p for p in after if p not in before]
print("PROBE after swipe:", js(PROBE), flush=True)
check(len(swiped) == 1 and after[swiped[0]] == "delete", f"swipe left marks a file for deletion {swiped}")
shot("03-after-swipe")

adb("shell", "input", "swipe", str(int(w * 0.5)), str(int(hgt * 0.45)), str(int(w * 0.9)), str(int(hgt * 0.44)), "450")
time.sleep(1.2)
after2 = {f["path"]: f["decision"] for f in json.loads(req("/api/files?decision=any&limit=500")[2])["files"] if f["decision"]}
print("PROBE after right swipe:", js(PROBE), after2, flush=True)
check(sum(1 for v in after2.values() if v == "keep") == 1, "swipe right keeps a file")
small_perf = js_json("JSON.stringify(window.__phottoPerf || {})")
perf["swipeToNextMs"] = small_perf.get("swipeToNextMs")
check(isinstance(perf["swipeToNextMs"], (int, float)) and perf["swipeToNextMs"] < 150,
      f"swipe to next card stays under budget ({perf['swipeToNextMs']} ms)")

# ------------------------------------------------- 4. review + back button
adb("shell", "input", "keyevent", "KEYCODE_R")
time.sleep(1.5)
shot("04-review")
adb("shell", "input", "keyevent", "KEYCODE_BACK")
time.sleep(1.2)
top = adb("shell", "dumpsys", "activity", "activities")
check(f"{PKG}/.MainActivity" in top, "back closes the overlay instead of leaving the app")
shot("05-after-back")

adb("shell", "input", "keyevent", "KEYCODE_G")
time.sleep(1.5)
shot("06-picker")
adb("shell", "input", "keyevent", "KEYCODE_BACK")
time.sleep(1)

# ------------------------------------------------- 5. trash with system dialog
doomed = swiped[0] if swiped else "orange.png"
extra = "doc.pdf" if doomed == "notes.md" else "notes.md"  # a non-media file too
req("/api/decide", {"path": extra, "decision": "delete"})
holder = {}
t = threading.Thread(target=lambda: holder.update(r=req("/api/commit", {}, timeout=120)))
t.start()
time.sleep(4)
shot("07-trash-dialog")
tapped = None
dialog_deadline = time.monotonic() + 120
while t.is_alive() and time.monotonic() < dialog_deadline:
    tapped = tap_text("Allow", "ALLOW", "Move to trash", "Ok", "OK")
    if tapped:
        break
    t.join(2)
print("tapped:", tapped)
t.join(90)
res = json.loads(holder["r"][2]) if "r" in holder else {}
print("commit:", res)
check(sorted(res.get("trashed", [])) == sorted([doomed, extra]), "commit trashes a media file and a text file")
ls = adb("shell", "ls", f"{DEV}/{Path(doomed).parent.as_posix()}", check=False)
check(Path(doomed).name not in ls.split() and extra not in adb("shell", "ls", DEV, check=False).split(), "files no longer visible in their folder")
time.sleep(1)
shot("08-after-commit")

# ------------------------------------- 5b. unreadable subtree -> error state
ERR_REL = f"files/errroot"
ERR_ROOT = f"/data/user/0/{PKG}/{ERR_REL}"
adb("shell", "run-as", PKG, "rm", "-rf", ERR_REL)
adb("shell", "run-as", PKG, "mkdir", "-p", ERR_REL + "/blocked")
adb("shell", "run-as", PKG, "touch", ERR_REL + "/ok.png")
adb("shell", "run-as", PKG, "chmod", "000", ERR_REL + "/blocked")
adb("shell", "am", "force-stop", PKG)
adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity",
    "--es", "photto.test.token", TOKEN, "--ei", "photto.test.port", str(PORT),
    "--es", "photto.test.root", ERR_ROOT)
adb("forward", f"tcp:{PORT}", f"tcp:{PORT}", check=False)
_, scan_err = wait_scan_complete(timeout=30)
check(scan_err["scan"]["state"] == "error" and "blocked" in (scan_err["scan"].get("error") or ""),
      f"an unreadable subtree ends the scan in the error state ({scan_err['scan'].get('error')})")
check(scan_err["counts"]["total"] == 1, "readable files are still indexed before the error")
adb("shell", "run-as", PKG, "chmod", "700", ERR_REL + "/blocked")
adb("shell", "run-as", PKG, "rm", "-rf", ERR_REL)

# ------------------------------------------------- 6. large folder: cold, paging, reuse
adb("shell", "am", "force-stop", PKG, check=False)
adb("shell", "run-as", PKG, "rm", "-rf", "files/index", "cache/previews")
cold_start = launch(LARGE_DEV)
perf["coldFirstPageMs"], first_page = wait_first_page(cold_start)
# The shared UI should render a usable card from the first page while the
# index is still scanning, instead of waiting for the whole folder.
cold_client = {}
cold_deadline = time.monotonic() + 10
while time.monotonic() < cold_deadline:
    cold_client = js_json("JSON.stringify({deck:S.deck.length,"
                          "top:document.querySelectorAll('#deck .card.top').length,"
                          "scan:S.scan && S.scan.state})")
    if cold_client.get("deck"):
        break
    time.sleep(0.2)
check(cold_client.get("deck", 0) > 0 and cold_client.get("top", 0) == 1 and
      cold_client.get("scan") == "scanning",
      f"a card is usable before the full scan completes ({cold_client})")
cold_complete, scan = wait_scan_complete(cold_start)
perf["coldFullIndexMs"] = cold_complete
check(first_page.get("platform") == "android", "large folder reports android")
check(scan["counts"]["total"] == LARGE_EXPECTED,
      f"large scan indexes {LARGE_EXPECTED} files ({scan['counts']['total']})")
check(perf["coldFirstPageMs"] < perf["coldFullIndexMs"],
      f"cold first page is served before the full index "
      f"({round(perf['coldFirstPageMs'])} < {round(perf['coldFullIndexMs'])} ms)")
cancel_job = req_json("/api/text/start", {"images": True, "videos": False, "language": "eng", "threshold": 2})
req_json("/api/text/cancel", {"jobId": cancel_job["jobId"]})
cancel_deadline = time.monotonic() + 30
while time.monotonic() < cancel_deadline:
    cancel_state = req_json(f"/api/text/status?jobId={cancel_job['jobId']}")
    if cancel_state.get("state") != "running": break
    time.sleep(.1)
large_after_cancel = adb("shell", f"find {LARGE_DEV} -type f | wc -l").strip()
check(cancel_state.get("state") == "canceled", "large Android OCR job cancels promptly")
check(large_after_cancel == str(LARGE_EXPECTED), "canceling OCR leaves every source file in place")
req("/api/reset", {})  # deterministic marks for this run
time.sleep(1)
client_perf = js_json("JSON.stringify(window.__phottoPerf || {})")
perf["clientFirstCardMs"] = client_perf.get("firstCardMs")
perf["coldServerFirstPageMs"] = req_json("/api/debug/perf").get("firstPageMs")
shot("09-large-deck")

local_rels = [p.relative_to(large).as_posix() for p in large.rglob("*") if p.is_file()]
expected = sorted(local_rels, key=lambda r: (natural(Path(r).name), r))
pages = {}
for sort in ("name", "newest", "oldest", "largest", "shuffle"):
    rows, last = page_all(sort)
    pages[sort] = rows
    check(len(rows) == LARGE_EXPECTED and len({r["path"] for r in rows}) == LARGE_EXPECTED,
          f"{sort} cursor paging returns each file exactly once ({len(rows)})")
check([r["path"] for r in pages["name"]] == expected, "name paging matches the fixture order")
check(all(a["mtime"] >= b["mtime"] for a, b in zip(pages["newest"], pages["newest"][1:])),
      "newest paging is descending")
check(all(a["mtime"] <= b["mtime"] for a, b in zip(pages["oldest"], pages["oldest"][1:])),
      "oldest paging is ascending")
check(all(a["size"] >= b["size"] for a, b in zip(pages["largest"], pages["largest"][1:])),
      "largest paging is descending")
check({r["path"] for r in pages["shuffle"]} == set(expected), "shuffle covers the same files")

# decision continuity and a forced index rebuild
keep_path = next(r["path"] for r in pages["name"] if r["kind"] == "image")
req("/api/decide", {"path": keep_path, "decision": "keep"})
adb("shell", "am", "force-stop", PKG, check=False)
adb("shell", "run-as", PKG, "rm", "-rf", "files/index")
rebuild_start = launch(LARGE_DEV)
perf["rebuildFirstPageMs"], _ = wait_first_page(rebuild_start)
wait_scan_complete(rebuild_start)
kept = page_all("keep")[0]
check(any(r["path"] == keep_path for r in kept),
      "a mark survives closing the app and deleting the index")
check(req_json("/api/scan")["counts"]["byDecision"]["keep"] == 1,
      "rebuilt index re-applies the decision JSON")

# warm open uses the existing index instead of walking every file first
warm_start = launch(LARGE_DEV)
perf["warmFirstPageMs"], warm_page = wait_first_page(warm_start)
warm_perf = req_json("/api/debug/perf")
perf["warmServerFirstPageMs"] = warm_perf.get("firstPageMs")
perf["warmIndexCompleteMs"] = warm_perf.get("indexCompleteMs")
perf["warmScanState"] = warm_page["scan"]["state"]
check(warm_page["counts"]["total"] == LARGE_EXPECTED and len(warm_page["files"]) == 200,
      f"warm open serves a full page from the existing index ({warm_page['counts']['total']} files)")
check(warm_page["scan"]["state"] == "scanning" or
      (perf["warmServerFirstPageMs"] or 10_000) < (perf["warmIndexCompleteMs"] or 0),
      "warm open does not wait for revalidation")
check((perf["warmServerFirstPageMs"] or 10_000) < (perf["coldFullIndexMs"] or 0),
      f"warm page comes from the index, not a completed rescan "
      f"({perf['warmServerFirstPageMs']} < {round(perf['coldFullIndexMs'] or 0)} ms)")
wait_scan_complete(warm_start)

# revalidation: added and removed files never drop existing marks
added = "Downloads/new-photo.png"
subprocess.run(["adb", "push", str(fx / "orange.png"), f"{LARGE_DEV}/{added}"],
               capture_output=True, check=True)
req("/api/files?refresh=1&limit=1")
wait_scan_complete(timeout=60)
rows, _ = page_all("name")
check(any(r["path"] == added for r in rows) and len(rows) == LARGE_EXPECTED + 1,
      "refresh indexes a newly added file")
adb("shell", "rm", "-f", f"{LARGE_DEV}/{added}")
req("/api/files?refresh=1&limit=1")
wait_scan_complete(timeout=60)
rows, _ = page_all("name")
check(not any(r["path"] == added for r in rows) and len(rows) == LARGE_EXPECTED,
      "refresh removes a deleted file")
check(any(r["path"] == keep_path for r in page_all("keep")[0]) and
      req_json("/api/scan")["counts"]["byDecision"]["keep"] == 1,
      "revalidation preserves marks for files that still exist")

# ------------------------------------- 6b. cancellation and lifecycle
# A folder switch must stop the old generation before the new index is queried.
adb("shell", "am", "force-stop", PKG, check=False)
adb("shell", "run-as", PKG, "rm", "-rf", "files/index", "cache/previews")
launch(LARGE_DEV)
time.sleep(0.25)
req("/api/root", {"path": DEV, "recursive": True})
_, switched = wait_scan_complete(timeout=30)
# System trash renames files in place to hidden ".trashed-*" entries; the
# scanner skips dotfiles, so exclude them from the expected indexed count.
small_expected = int(adb("shell", f"find '{DEV}' -type f -not -name '.*' -not -path '{DEV}/SIFT_docs/*' -not -path '{DEV}/SIFT trash/*' | wc -l").strip())
check(switched["counts"]["total"] == small_expected,
      f"folder switch cancels the old scan and indexes only the new folder "
      f"({switched['counts']['total']} == {small_expected})")
switch_rev = switched["revision"]
time.sleep(1)
after_switch = req_json("/api/scan")
check(after_switch["revision"] == switch_rev and after_switch["counts"]["total"] == small_expected,
      "no stale-generation writes after a folder switch")
req("/api/root", {"path": LARGE_DEV, "recursive": True})
_, switched_back = wait_scan_complete(timeout=60)
check(switched_back["counts"]["total"] == LARGE_EXPECTED,
      "switching back rebuilds the large index")
check(switched_back["counts"]["byDecision"]["keep"] == 1,
      f"folder switches keep marks for each folder "
      f"({switched_back['counts']['byDecision']['keep']})")

# Destroying the activity mid-scan must not corrupt the index or lose marks.
adb("logcat", "-c")
adb("shell", "am", "force-stop", PKG, check=False)
adb("shell", "run-as", PKG, "rm", "-rf", "files/index")
launch(LARGE_DEV)
time.sleep(0.3)
adb("shell", "am", "force-stop", PKG)
time.sleep(0.5)
crash = adb("logcat", "-d", "-s", "AndroidRuntime:E", check=False)
check("FATAL EXCEPTION" not in crash, "destroying the activity mid-scan does not crash")
destroy_start = launch(LARGE_DEV)
wait_first_page(destroy_start)
_, destroyed = wait_scan_complete(destroy_start, timeout=60)
check(destroyed["counts"]["total"] == LARGE_EXPECTED,
      f"a restart after a mid-scan destroy indexes the folder again "
      f"({destroyed['counts']['total']})")
check(destroyed["counts"]["byDecision"]["keep"] == 1, "decisions survive a mid-scan destroy")

# Losing storage access: changing MANAGE_EXTERNAL_STORAGE kills the app process
# on API 34, which stops the walker before it can write again. Verify the OS
# stop, then the permission screen on relaunch, then a clean full reindex.
adb("shell", "am", "force-stop", PKG, check=False)
adb("shell", "run-as", PKG, "rm", "-rf", "files/index")
launch(LARGE_DEV)
time.sleep(1)
adb("shell", "appops", "set", "--uid", PKG, "MANAGE_EXTERNAL_STORAGE", "deny")
time.sleep(1.5)
alive = adb("shell", "pidof", PKG, check=False).strip()
check(not alive, f"revoking storage access stops the scanner process ({alive or 'stopped'})")
adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity",
    "--es", "photto.test.token", TOKEN, "--ei", "photto.test.port", str(PORT),
    "--es", "photto.test.root", LARGE_DEV)
time.sleep(3)
adb("forward", f"tcp:{PORT}", f"tcp:{PORT}", check=False)
st, _, body = req("/api/files")
check(json.loads(body).get("needsPermission") is True, "revoked access reports needsPermission")
shot("13-permission-revoked")
adb("shell", "appops", "set", "--uid", PKG, "MANAGE_EXTERNAL_STORAGE", "allow")
adb("shell", "am", "force-stop", PKG)
launch(LARGE_DEV)
req("/api/files?refresh=1&limit=1")
_, permitted = wait_scan_complete(timeout=60)
check(permitted["counts"]["total"] == LARGE_EXPECTED,
      f"a full rescan completes after access returns ({permitted['counts']['total']})")

# ------------------------------------- 6c. client deck, counts, reconciliation
client_reload()
wait_client_deck(12)
client = js_json("JSON.stringify({counts:S.counts,revision:S.revision,scan:S.scan&&S.scan.state})")
server = req_json("/api/scan")
for key in ("total", "undecided", "byDecision", "byGroup", "undecidedByGroup", "bytesByDecision"):
    check(client.get("counts", {}).get(key) == server["counts"].get(key),
          f"client counts match the server: {key}")

# A mixed decide/undo/skip sequence must leave the client and server in agreement.
mix = js_json("JSON.stringify((()=>{const a=S.deck[0], b=S.deck[1];return {a:a.path,b:b.path};})())")
js("decide('keep'); 'ok'")
time.sleep(0.5)
js("decide('delete'); 'ok'")
time.sleep(0.5)
js("undo(); 'ok'")
time.sleep(0.5)
js("skip(); skip(); 'ok'")
time.sleep(1.2)
mixed_client = js_json("JSON.stringify({keep:S.counts.byDecision.keep,"
                       "delete:S.counts.byDecision.delete,undecided:S.counts.undecided})")
mixed_server = req_json("/api/scan")["counts"]
check(mixed_client.get("keep") == mixed_server["byDecision"]["keep"] == 2 and
      mixed_client.get("delete") == mixed_server["byDecision"]["delete"] == 0,
      f"decide, undo and skip reconcile with the server ({mixed_client})")
check(mix.get("a") in {r["path"] for r in page_all("keep")[0]},
      "the kept file from the mixed sequence is stored")
req("/api/decide", {"path": mix.get("a"), "decision": None})
time.sleep(0.5)
js("load()")
wait_client_deck(12)

# Drain the whole deck through the card pipeline: every undecided file once.
undecided_expected = LARGE_EXPECTED - 1  # keep_path is already marked
walk = js_json("""(async()=>{
  const orig = window.api;
  window.api = (p, b, o) => String(p).startsWith('/api/decide')
    ? Promise.resolve({ok:true}) : orig(p, b, o);
  const seen = new Set(); let duplicates = 0, steps = 0, maxLive = 0;
  while (steps < 4000 && !(S.exhausted && S.deck.length === 0)) {
    if (!S.deck.length) { await new Promise(r => setTimeout(r, 40)); continue; }
    const f = S.deck[0];
    if (seen.has(f.path)) duplicates++;
    seen.add(f.path);
    decide('keep');
    steps++;
    maxLive = Math.max(maxLive, document.querySelectorAll('#deck .card:not(.flying)').length);
    if (steps % 25 === 0) {
      document.querySelectorAll('#deck .card.flying').forEach(e => e.remove());
      await new Promise(r => setTimeout(r, 0));
    }
  }
  window.api = orig;
  document.querySelectorAll('#deck .card.flying').forEach(e => e.remove());
  const out = {steps, unique: seen.size, duplicates, exhausted: S.exhausted,
               deck: S.deck.length, maxLive};
  await load();
  return JSON.stringify(out);
})()""", timeout=120)
check(walk.get("steps") == undecided_expected and walk.get("unique") == undecided_expected,
      f"the full deck traverses every undecided file ({walk})")
check(walk.get("duplicates") == 0 and walk.get("exhausted") is True and walk.get("deck") == 0,
      "deck traversal has no duplicates and ends exhausted")
check(walk.get("maxLive", 99) <= 3,
      f"the deck keeps at most three live cards ({walk.get('maxLive')})")

# Sort and filter changes reset the cursor and never mix stale rows.
sort_probe = js_json("JSON.stringify((()=>{const s=document.querySelector('#sort');"
                     "s.value='name';s.onchange();return {sort:S.sort};})())")
check(sort_probe.get("sort") == "name", "the sort control resets the deck")
wait_client_deck(12)
names = js_json("JSON.stringify(S.deck.slice(0,12).map(f=>f.path))")
expected_undecided = [r for r in expected if r != keep_path]
check(names == expected_undecided[:12], "name sort returns the fixture order from page one")
js("S.filter='videos'; resetDeck(); 'ok'")
wait_client_deck(1)
videos = js_json("JSON.stringify({deck:S.deck.map(f=>f.kind),"
                 "left:S.counts.undecidedByGroup.videos})")
check(videos.get("deck") and all(k == "video" for k in videos["deck"]) and
      videos.get("left") == len(videos["deck"]),
      f"the video filter only shows videos ({videos})")
js("S.filter='all'; S.sort='name'; resetDeck(); 'ok'")
wait_client_deck(12)

# Revalidation while the UI is open: new files appear, removed files leave no card.
new_rel = "Camera/aa-ui-new.png"
subprocess.run(["adb", "push", str(fx / "orange.png"), f"{LARGE_DEV}/{new_rel}"],
               capture_output=True, check=True)
js("S.scan = {...(S.scan || {}), state: 'scanning'}; 'ok'")
req("/api/files?refresh=1&limit=1")
wait_scan_complete(timeout=60)
js("pollScan().then(() => 'ok')", timeout=60)
wait_client_deck(1)
time.sleep(0.3)
added_view = js_json("JSON.stringify({total:S.counts.total,"
                     "seen:S.deck.some(f=>f.path==='%s')})" % new_rel)
check(added_view.get("total") == LARGE_EXPECTED + 1 and added_view.get("seen") is True,
      f"a newly indexed file appears in the sorted deck ({added_view})")
adb("shell", "rm", "-f", f"{LARGE_DEV}/{new_rel}")
js("S.scan = {...(S.scan || {}), state: 'scanning'}; 'ok'")
req("/api/files?refresh=1&limit=1")
wait_scan_complete(timeout=60)
js("pollScan().then(() => 'ok')", timeout=60)
wait_client_deck(1)
time.sleep(0.3)
removed_view = js_json("JSON.stringify({total:S.counts.total,"
                       "deck:S.deck.some(f=>f.path==='%s'),"
                       "els:[...S.els.keys()].includes('%s')})" % (new_rel, new_rel))
check(removed_view.get("total") == LARGE_EXPECTED and not removed_view.get("deck") and
      not removed_view.get("els"),
      f"a removed file leaves no stale card ({removed_view})")

# ------------------------------------- 6d. Android rendering and gestures
style = js_json("""JSON.stringify((()=>{
  const cs = (s) => getComputedStyle(document.querySelector(s));
  const small = [];
  for (const sel of ['#filters button', '.review-btn', '#deck .card.top .icon-btn',
                     '.ctl', '#bDelete']) {
    for (const e of document.querySelectorAll(sel)) {
      const r = e.getBoundingClientRect();
      if (r.width < 44 || r.height < 44) small.push([sel, Math.round(r.width), Math.round(r.height)]);
    }
  }
  const f = document.querySelector('#filters');
  const fr = f.getBoundingClientRect();
  return {grain: cs('.grain').display,
    blur: [...document.querySelectorAll('.blur')].map(e => getComputedStyle(e).display),
    backdrop: cs('#filters').backdropFilter || cs('#filters').webkitBackdropFilter,
    iconBackdrop: cs('.card.top .icon-btn').backdropFilter || cs('.card.top .icon-btn').webkitBackdropFilter,
    bg: getComputedStyle(document.body).backgroundImage,
    shadow: cs('.card.top').boxShadow,
    small,
    scroll: [document.documentElement.scrollWidth, document.documentElement.clientWidth],
    filters: [Math.round(fr.left), Math.round(fr.right), innerWidth, f.scrollWidth, f.clientWidth]};
})())""")
check(style.get("grain") == "none", "the Android profile hides the grain overlay")
check(all(v == "none" for v in style.get("blur", ["none"])) or not style.get("blur"),
      "the Android profile hides blurred backdrops")
check(style.get("backdrop") == "none" and style.get("iconBackdrop") == "none",
      "the Android profile removes backdrop filters")
check("linear-gradient" in (style.get("bg") or "") and "22px" in (style.get("shadow") or ""),
      "the Android profile keeps cheap gradients and reduced shadows")
check(style.get("small") == [], f"phone controls are at least 44 px ({style.get('small')})")
check(style.get("scroll", [1, 0])[0] <= style.get("scroll", [0, 1])[1] + 1,
      f"the phone layout has no horizontal page overflow ({style.get('scroll')})")
check(style.get("filters", [0, 0, 0])[1] <= style.get("filters", [0, 0, 0])[2] + 1 and
      style.get("filters", [0, 0, 0])[0] >= -1,
      f"the filter row stays inside the viewport ({style.get('filters')})")

drag = js_json("""(async()=>{
  const el = document.querySelector('#deck .card.top');
  if (!el) return JSON.stringify({error: 'no card'});
  const origCapture = Element.prototype.setPointerCapture;
  Element.prototype.setPointerCapture = () => {};
  const origRaf = window.requestAnimationFrame;
  let frames = 0;
  window.requestAnimationFrame = (cb) => { frames++; return origRaf(cb); };
  const ev = (t, x, y) => el.dispatchEvent(new PointerEvent(t, {bubbles: true,
    clientX: x, clientY: y, pointerId: 7, button: 0, pointerType: 'touch', isPrimary: true}));
  ev('pointerdown', 120, 320);
  for (let i = 1; i <= 30; i++) ev('pointermove', 120 + i, 320);
  await new Promise((r) => origRaf(() => origRaf(r)));
  window.requestAnimationFrame = origRaf;
  const tf = el.style.transform;
  const wc = getComputedStyle(el).willChange;
  const dragClass = el.classList.contains('dragging');
  ev('pointerup', 150, 320);
  await new Promise((r) => setTimeout(r, 60));
  Element.prototype.setPointerCapture = origCapture;
  const out = {frames, transform: tf, willChange: wc, dragging: dragClass,
               after: el.style.transform};
  return JSON.stringify(out);
})()""")
check(drag.get("frames") == 1, f"a fast drag coalesces into one animation frame ({drag})")
check("translate3d(30px" in (drag.get("transform") or "") and drag.get("dragging") is True,
      "the dragged card follows the pointer with a composited transform")
check("transform" in (drag.get("willChange") or "") and drag.get("after") == "",
      "will-change is applied during the drag and removed after it")
time.sleep(0.8)
clean = js_json("JSON.stringify({live:document.querySelectorAll('#deck .card').length,"
                "active:document.querySelectorAll('.card.dragging,.card.flying').length,"
                "wc:[...document.querySelectorAll('#deck .card')].map(e=>getComputedStyle(e).willChange)})")
check(clean.get("active") == 0 and all(v == "auto" for v in clean.get("wc", ["auto"])),
      "no card keeps will-change after the gesture")
check(clean.get("live", 99) <= 3, f"the live deck stays bounded ({clean.get('live')})")

# Media elements start without a source and are released on teardown.
video_rows = page_all("name", "any", "videos", limit=10)[0][:2]
audio_rows = page_all("name", "any", "audio", limit=10)[0][:2]
media_probe = js(
    "JSON.stringify((()=>{"
    f"const V1={json.dumps(video_rows[0])}, V2={json.dumps(video_rows[1])};"
    f"const A1={json.dumps(audio_rows[0])}, A2={json.dumps(audio_rows[1])};"
    "const probe=(topFile, behindFile, tag)=>{"
    "const top=makeCard(topFile), behind=makeCard(behindFile);"
    "const a=top.querySelector(tag), b=behind.querySelector(tag);"
    "const before=[a.getAttribute('src'),b.getAttribute('src')];"
    "activate(top,topFile);"
    "const active=[a.getAttribute('src'),b.getAttribute('src')];"
    "deactivate(top);teardown(top);teardown(behind);"
    "const gone=[a.getAttribute('src'),b.getAttribute('src')];"
    "return {before,active,gone};};"
    "return {video:probe(V1,V2,'video'),audio:probe(A1,A2,'audio')};})())",
    timeout=30)
try:
    media = json.loads(media_probe) if isinstance(media_probe, str) else {}
except ValueError:
    media = {}
video = media.get("video") or {}
audio = media.get("audio") or {}
check(video.get("before") == [None, None],
      "video cards start without a source")
check(bool((video.get("active") or [None])[0]) and (video.get("active") or [None, None])[1] is None,
      "only the active video card gets a source")
check(video.get("gone") == [None, None],
      "teardown releases the video source")
check(audio.get("before") == [None, None],
      "audio cards start without a source")
check(bool((audio.get("active") or [None])[0]) and (audio.get("active") or [None, None])[1] is None,
      "only the active audio card gets a source")
check(audio.get("gone") == [None, None],
      "teardown releases the audio source")

# ------------------------------------------------- 7. thumbnail pipeline probes
thumbs_url = lambda row, version=True, prio=1: (
    "/thumb?p=" + urllib.parse.quote(row["path"]) +
    (("&v=" + urllib.parse.quote(f"{int(row['mtime'] * 1000)}-{row['size']}")) if version else "") +
    f"&prio={prio}")

image_rows = [r for r in pages["name"] if r["kind"] == "image"][:16]
with ThreadPoolExecutor(max_workers=16) as pool:
    burst = list(pool.map(lambda r: req(thumbs_url(r)), image_rows))
check(all(st == 200 and hget(h, "Content-Type").startswith("image/") and len(b) > 50
          for st, h, b in burst), "16 concurrent thumbnails all render")
after_burst = req_json("/api/debug/perf")["thumbs"]
perf["maxThumbConcurrent"] = after_burst["maxConcurrent"]
check(after_burst["maxConcurrent"] <= after_burst["workers"],
      f"thumbnail decodes stay bounded ({after_burst['maxConcurrent']} <= {after_burst['workers']})")
check(after_burst["decodes"] >= len(image_rows) - 3, "the burst actually decoded new thumbnails")

before_repeat = req_json("/api/debug/perf")["thumbs"]
for row in image_rows:
    req(thumbs_url(row))
after_repeat = req_json("/api/debug/perf")["thumbs"]
check(after_repeat["decodes"] == before_repeat["decodes"],
      "repeat thumbnail requests do not decode again")
check(after_repeat["memoryHits"] + after_repeat["diskHits"] >
      before_repeat["memoryHits"] + before_repeat["diskHits"],
      "repeat thumbnail requests hit a cache")

st, h, _ = req(thumbs_url(image_rows[0]))
check("immutable" in hget(h, "Cache-Control"), "versioned thumbnails use immutable caching")
st, h, _ = req(thumbs_url(image_rows[0], version=False))
check("max-age=300" in hget(h, "Cache-Control"), "unversioned thumbnails keep a short cache")
try:
    req("/thumb?p=" + urllib.parse.quote("Downloads/readme.txt"))
    check(False, "unsupported thumbnails return 404")
except urllib.error.HTTPError as e:  # noqa: PERF203
    check(e.code == 404, "unsupported thumbnails return 404")

# ------------------------------------------------- 8. Review with 1,000 marks
marks = [r["path"] for r in pages["name"] if r["path"] != keep_path][:1000]
for path in marks:
    req("/api/decide", {"path": path, "decision": "delete"})
counts = req_json("/api/scan")["counts"]
check(counts["byDecision"]["delete"] == 1000 and counts["byDecision"]["keep"] == 1,
      "1,000 delete marks and one keep are stored")
js("load()")
time.sleep(1)
js("window.__phottoPerf.reviewOpenMs = null; openReview(); 'ok'")
time.sleep(1.5)
review = js_json("""JSON.stringify((()=>{const g=document.querySelector('#reviewGrid');const t=g.querySelector('.tile');
  return {tiles:g.querySelectorAll('.tile').length,loaded:g.querySelectorAll('img[src]').length,
  pending:g.querySelectorAll('img[data-src]').length,openMs:window.__phottoPerf.reviewOpenMs,
  cv:t?getComputedStyle(t).contentVisibility:null,contain:t?getComputedStyle(t).contain:null};})())""")
perf["reviewOpenMs"] = review.get("openMs")
check(review.get("tiles") == 100, f"Review renders one page of tiles ({review.get('tiles')})")
check(0 < review.get("loaded", 0) < review.get("tiles", 0),
      f"Review loads only visible thumbnails ({review.get('loaded')}/{review.get('tiles')})")
check(review.get("cv") == "auto", "Review tiles use content-visibility")
review_layout = js_json("""JSON.stringify((()=>{
  const g = document.querySelector('#reviewGrid').getBoundingClientRect();
  const f = document.querySelector('#review .review-foot').getBoundingClientRect();
  const tabs = document.querySelector('#reviewTabs');
  const tr = tabs.getBoundingClientRect();
  const small = [];
  for (const sel of ['#reviewTabs button', '#commitBtn', '#reviewClose']) {
    for (const e of document.querySelectorAll(sel)) {
      const r = e.getBoundingClientRect();
      if (r.width < 44 || r.height < 44) small.push([sel, Math.round(r.width), Math.round(r.height)]);
    }
  }
  return {gridBottom: g.bottom, footTop: f.top, footBottom: f.bottom, vh: innerHeight,
          tabs: [Math.round(tr.left), Math.round(tr.right), innerWidth,
                 tabs.scrollWidth, tabs.clientWidth],
          overflow: getComputedStyle(tabs).overflowX, small};
})())""")
check(review_layout.get("footBottom", 1e9) <= review_layout.get("vh", 0) + 1 and
      review_layout.get("gridBottom", 1e9) <= review_layout.get("footTop", 0) + 1,
      "the Review footer stays visible below the tile grid")
check(review_layout.get("tabs", [0, 0, 0])[1] <= review_layout.get("tabs", [0, 0, 0])[2] + 1 and
      review_layout.get("overflow") in ("auto", "scroll"),
      f"the Review tab row scrolls inside the viewport ({review_layout.get('tabs')})")
check(review_layout.get("small") == [],
      f"Review controls are at least 44 px ({review_layout.get('small')})")
check(isinstance(perf["reviewOpenMs"], (int, float)) and perf["reviewOpenMs"] < 1500,
      f"Review opens within budget ({perf['reviewOpenMs']} ms)")
shot("10-large-review")

js("(()=>{const g=document.querySelector('#reviewGrid');g.scrollTop=g.scrollHeight;return 'ok';})()")
time.sleep(1.2)
review2 = js_json("""JSON.stringify((()=>{const g=document.querySelector('#reviewGrid');
  return {tiles:g.querySelectorAll('.tile').length,loaded:g.querySelectorAll('img[src]').length};})())""")
check(review2.get("tiles", 0) > review.get("tiles", 0), "Review appends the next page on scroll")
check(review2.get("loaded", 0) < review2.get("tiles", 1), "offscreen Review tiles stay unloaded")
js("document.querySelector('#reviewGrid .tile').click(); 'ok'")
time.sleep(0.8)
after_undo = js_json("JSON.stringify({deleteCount:S.counts.byDecision.delete, keepCount:S.counts.byDecision.keep})")
check(after_undo.get("deleteCount") == 999, "undo in Review drops the delete count")
js("closeReview(); 'ok'")

review_thumbs = req_json("/api/debug/perf")["thumbs"]
perf["reviewMaxConcurrent"] = review_thumbs["maxConcurrent"]
check(review_thumbs["maxConcurrent"] <= review_thumbs["workers"],
      f"Review thumbnails stay bounded ({review_thumbs['maxConcurrent']} <= {review_thumbs['workers']})")

# ------------------------------------------------- 9. commit the 999 marked files
holder = {}
t = threading.Thread(target=lambda: holder.update(r=req("/api/commit", {}, timeout=900)))
t.start()
time.sleep(8)
shot("11-large-trash-dialog")
tapped = None
dialog_deadline = time.monotonic() + 180
while t.is_alive() and time.monotonic() < dialog_deadline:
    tapped = tap_text("Allow", "ALLOW", "Move to trash", "Ok", "OK")
    if tapped:
        break
    t.join(2)
print("tapped:", tapped)
t.join(900)
res = json.loads(holder["r"][2]) if "r" in holder else {}
print("large commit:", {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()})
check(len(res.get("trashed", [])) == 999, f"commit trashes the reviewed files ({len(res.get('trashed', []))})")
sample = (res.get("trashed") or marks)[0]
gone = adb("shell",
           f"test -e '{LARGE_DEV}/{sample}' && echo yes || echo no", check=False).strip()
check(gone == "no", "a trashed file is gone from its folder")
wait_scan_complete(timeout=90)
final = req_json("/api/scan")["counts"]
check(final["total"] == LARGE_EXPECTED - 999, f"index forgets trashed files ({final['total']})")
check(final["byDecision"]["delete"] == 0 and final["byDecision"]["keep"] == 1,
      "trashed files lose their delete marks, kept files keep theirs")
shot("12-large-after-commit")

# ------------------------------------- 9b. reduced motion on Android
scale = adb("shell", "settings", "get", "global", "animator_duration_scale").strip()
adb("shell", "settings", "put", "global", "animator_duration_scale", "0")
time.sleep(0.5)
# Chromium reads the emulator's animation scale when the WebView is created,
# not on a page reload: recreate the activity so prefers-reduced-motion flips.
launch(LARGE_DEV)
wait_client_deck(1)
motion = js_json("JSON.stringify({reduce:matchMedia('(prefers-reduced-motion: reduce)').matches})")
check(motion.get("reduce") is True, "the reduced-motion setting reaches the WebView")
delete_before = req_json("/api/scan")["counts"]["byDecision"]["delete"]
adb("shell", "input", "swipe", str(int(w * 0.5)), str(int(hgt * 0.45)),
    str(int(w * 0.1)), str(int(hgt * 0.46)), "450")
time.sleep(1.2)
delete_after = req_json("/api/scan")["counts"]["byDecision"]["delete"]
check(delete_after == delete_before + 1,
      "a swipe still decides with animations disabled")
if scale in ("", "null"):
    adb("shell", "settings", "delete", "global", "animator_duration_scale", check=False)
else:
    adb("shell", "settings", "put", "global", "animator_duration_scale", scale)

# ------------------------------------------------- 10. budgets and report
budgets = budgets_from_env()
perf["firstCardMs"] = round((perf.get("coldFirstPageMs") or 0) +
                            (perf.get("clientFirstCardMs") or 0))
perf["budgets"] = budgets
check(perf["firstCardMs"] < budgets["firstCardMs"],
      f"first card within budget ({perf['firstCardMs']} < {budgets['firstCardMs']} ms)")
check(perf["coldFullIndexMs"] < budgets["fullIndexMs"],
      f"full index within budget ({round(perf['coldFullIndexMs'])} ms)")
check(isinstance(perf.get("swipeToNextMs"), (int, float)) and
      perf["swipeToNextMs"] < budgets["swipeMs"], f"swipe within budget ({perf.get('swipeToNextMs')} ms)")
check(isinstance(perf.get("reviewOpenMs"), (int, float)) and
      perf["reviewOpenMs"] < budgets["reviewOpenMs"],
      f"Review within budget ({perf.get('reviewOpenMs')} ms)")
check(perf.get("maxThumbConcurrent", 99) <= budgets["maxConcurrent"],
      f"thumbnail concurrency within budget ({perf.get('maxThumbConcurrent')})")
perf["checksPassed"] = sum(c for c, _ in results)
perf["checksTotal"] = len(results)
(OUT / "perf.json").write_text(json.dumps(perf, indent=2, default=str))
if not all(c for c, _ in results):
    shot("99-failure")

print(f"\n{sum(c for c, _ in results)}/{len(results)} checks passed")
(OUT / "results.txt").write_text("\n".join(("PASS " if c else "FAIL ") + m for c, m in results))
if not all(c for c, _ in results):
    raise SystemExit(1)
