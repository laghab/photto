"""Drives the photto APK on an Android emulator over adb (run in CI).

Writes screenshots to $OUT so a human (or Claude) can look at the real UI.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import zlib
import struct
from pathlib import Path

PKG = "io.github.laghab.photto"
APK = os.environ.get("APK", "app/build/outputs/apk/debug/app-debug.apk")
OUT = Path(os.environ.get("OUT", "test-output"))
OUT.mkdir(parents=True, exist_ok=True)
PORT, TOKEN = 8799, "tt"
BASE = f"http://127.0.0.1:{PORT}"
DEV = "/sdcard/PhottoTest"
results = []


def adb(*args, check=True, capture=True):
    r = subprocess.run(["adb", *args], capture_output=capture, text=True)
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


def req(path, body=None, headers=None, timeout=30):
    h = {"X-Token": TOKEN, **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read()


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
for name in ("clip.mp4", "tone.mp3", "photo.jpg"):
    src = Path(os.environ.get("FIXTURES", ".")) / name
    if src.exists():
        (fx / name).write_bytes(src.read_bytes())

adb("shell", "rm", "-rf", DEV, check=False)
adb("shell", "mkdir", "-p", DEV)
for f in fx.rglob("*"):
    if f.is_file():
        rel = f.relative_to(fx).as_posix()
        adb("shell", "mkdir", "-p", f"{DEV}/{Path(rel).parent.as_posix()}")
        adb("push", str(f), f"{DEV}/{rel}")

# ------------------------------------------------- 1. no permission yet
adb("install", "-r", "-g", APK)
adb("shell", "appops", "set", "--uid", PKG, "MANAGE_EXTERNAL_STORAGE", "deny", check=False)
adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity", "--es", "photto.test.token", TOKEN,
    "--ei", "photto.test.port", str(PORT))
time.sleep(6)
adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")
st, h, body = req("/api/files")
check(json.loads(body).get("needsPermission") is True, "without access the app asks for permission")
shot("01-permission")

# ------------------------------------------------- 2. with permission + folder
adb("shell", "appops", "set", "--uid", PKG, "MANAGE_EXTERNAL_STORAGE", "allow")
adb("shell", "am", "force-stop", PKG)
adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity", "--es", "photto.test.token", TOKEN,
    "--ei", "photto.test.port", str(PORT), "--es", "photto.test.root", DEV)
time.sleep(6)
adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")

st, h, body = req("/")
check(st == 200 and b"photto" in body, "UI served from the APK")
st, h, body = req("/static/app.js")
check(h.get("Content-Type", "").startswith("text/javascript"), "app.js served with JS type")
st, h, body = req("/api/files")
d = json.loads(body)
paths = sorted(f["path"] for f in d["files"])
print("files:", paths)
check(d.get("platform") == "android", "platform reported as android")
check("sub/teal.png" in paths and "doc.pdf" in paths, "recursive scan finds files")
kinds = {f["path"]: f["kind"] for f in d["files"]}
check(kinds.get("doc.pdf") == "pdf" and kinds.get("notes.md") == "text", "file kinds detected")

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

# ------------------------------------------------- 3. real swipe gesture
size = re.search(r"(\d+)x(\d+)", adb("shell", "wm", "size")).groups()
w, hgt = int(size[0]), int(size[1])
before = {f["path"] for f in json.loads(req("/api/files")[2])["files"] if f["decision"]}
adb("shell", "input", "swipe", str(int(w * 0.5)), str(int(hgt * 0.45)), str(int(w * 0.05)), str(int(hgt * 0.47)), "250")
time.sleep(1.5)
after = {f["path"]: f["decision"] for f in json.loads(req("/api/files")[2])["files"] if f["decision"]}
swiped = [p for p in after if p not in before]
check(len(swiped) == 1 and after[swiped[0]] == "delete", f"swipe left marks a file for deletion {swiped}")
shot("03-after-swipe")

adb("shell", "input", "swipe", str(int(w * 0.5)), str(int(hgt * 0.45)), str(int(w * 0.95)), str(int(hgt * 0.44)), "250")
time.sleep(1.2)
after2 = {f["path"]: f["decision"] for f in json.loads(req("/api/files")[2])["files"] if f["decision"]}
check(sum(1 for v in after2.values() if v == "keep") == 1, "swipe right keeps a file")

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
doomed = swiped[0]
holder = {}
t = threading.Thread(target=lambda: holder.update(r=req("/api/commit", {}, timeout=120)))
t.start()
time.sleep(4)
shot("07-trash-dialog")
tapped = tap_text("Allow", "ALLOW", "Move to trash", "Ok", "OK")
print("tapped:", tapped)
t.join(90)
res = json.loads(holder["r"][2]) if "r" in holder else {}
print("commit:", res)
check(res.get("trashed") == [doomed], "commit moves the file to Android's trash")
ls = adb("shell", "ls", f"{DEV}/{Path(doomed).parent.as_posix()}", check=False)
check(Path(doomed).name not in ls.split(), "file no longer visible in its folder")
time.sleep(1)
shot("08-after-commit")

print(f"\n{sum(c for c, _ in results)}/{len(results)} checks passed")
(OUT / "results.txt").write_text("\n".join(("PASS " if c else "FAIL ") + m for c, m in results))
if not all(c for c, _ in results):
    raise SystemExit(1)
