"""End-to-end check of a built photto.exe on Windows (run in CI).

usage: python smoke_test.py path\\to\\photto.exe
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

EXE = sys.argv[1]
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "smoke"


def req(path, body=None, headers=None):
    h = {"X-Token": TOKEN, **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status, dict(resp.headers), resp.read()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        raise SystemExit(1)


root = Path(tempfile.mkdtemp(prefix="photto-smoke-"))
(root / "sub" / "deeper").mkdir(parents=True)
(root / "hello.txt").write_text("hello world")
(root / "sub" / "notes.md").write_text("# notes")
(root / "sub" / "deeper" / "doomed-photto-file.txt").write_text("delete me")
(root / ".hidden.txt").write_text("hidden")
subprocess.run(["attrib", "+h", str(root / "sub" / "notes.md")], check=False)  # Windows hidden attribute
(root / "sub" / "visible.md").write_text("visible")

proc = subprocess.Popen([EXE, str(root), "--window", "none", "--port", str(PORT), "--token", TOKEN])
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    else:
        check(False, "server started")
    check(True, "server started")

    st, h, body = req("/")
    check(st == 200 and b"photto" in body, "UI html served")
    st, h, body = req("/static/app.js")
    check("javascript" in h.get("Content-Type", ""), f"app.js content-type ({h.get('Content-Type')})")

    st, h, body = req("/api/files")
    d = json.loads(body)
    paths = sorted(f["path"] for f in d["files"])
    print("files:", paths)
    check(d["platform"] == "windows", "platform is windows")
    check("sub/deeper/doomed-photto-file.txt" in paths, "recursive scan with posix relative paths")
    check(".hidden.txt" not in paths and "sub/notes.md" not in paths, "dot-files and hidden-attribute files skipped")

    st, h, body = req("/raw?p=hello.txt", headers={"Range": "bytes=0-4"})
    check(st == 206 and body == b"hello", "byte-range request")

    st, h, body = req("/api/dirs")
    places = [p["name"] for p in json.loads(body)["places"]]
    print("places:", places)
    check(any(n.endswith("drive") for n in places), "drive letters listed")

    req("/api/decide", {"path": "sub/deeper/doomed-photto-file.txt", "decision": "delete"})
    st, h, body = req("/api/commit", {})
    res = json.loads(body)
    print("commit:", res)
    check(res["trashed"] == ["sub/deeper/doomed-photto-file.txt"] and not res["failed"], "commit reports trashed")
    check(not (root / "sub" / "deeper" / "doomed-photto-file.txt").exists(), "file gone from folder")

    ps = "(New-Object -ComObject Shell.Application).NameSpace(10).Items() | ForEach-Object { $_.Name }"
    names = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    check("doomed-photto-file" in names, "file is in the Recycle Bin")
finally:
    proc.terminate()

# GUI mode: launching with no folder must open the picker flow, not scan anything.
proc = subprocess.Popen([EXE, "--window", "none", "--port", str(PORT), "--token", TOKEN])
try:
    for _ in range(60):
        try:
            st, h, body = req("/api/files")
            break
        except OSError:
            time.sleep(0.5)
    check(json.loads(body).get("needsPick") is True, "no-argument launch starts with the folder picker")
finally:
    proc.terminate()
print("all Windows checks passed")
