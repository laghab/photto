#!/usr/bin/env python3
"""Compare the Android and desktop paged APIs on the same large fixture.

Requires a running emulator with the debug APK installed (the app is launched
with the test token/port/root extras). The Android folder ``/sdcard/PhottoLarge``
is replaced with a freshly generated fixture, and the desktop backend is started
on a temporary copy, so both sides see identical paths, sizes, and mtimes.

    python3 tools/parity_check.py
    ANDROID_SERIAL=emulator-5556 python3 tools/parity_check.py --port 8811

Writes ``test-output/parity.json`` and exits non-zero on any mismatch.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "test-output"
LARGE_DEV = "/sdcard/PhottoLarge"
TOKEN = "tt"
PKG = "io.github.laghab.photto"
ANDROID_PORT = 8799
KEEP = "Camera/IMG_00001.png"


def req(base: str, path: str, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data,
        headers={"X-Token": TOKEN, "Content-Type": "application/json"},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def page_all(base: str, sort="name", group="all", decision="any", limit=500):
    rows, cursor = [], None
    for _ in range(50):
        query = f"/api/files?limit={limit}&sort={sort}&group={group}&decision={decision}"
        if cursor:
            query += "&cursor=" + urllib.parse.quote(cursor, safe="")
        data = req(base, query)
        rows.extend(data["files"])
        cursor = data.get("nextCursor")
        if not cursor:
            return rows, data
    raise SystemExit("paging did not terminate")


def wait_complete(base: str, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = req(base, "/api/scan", timeout=10)
            if data["scan"]["state"] != "scanning":
                return data
        except Exception:
            pass
        time.sleep(0.1)
    raise SystemExit("scan did not complete")


def adb(*args, check=True, serial=None):
    env = dict(os.environ)
    if serial:
        env["ANDROID_SERIAL"] = serial
    result = subprocess.run(["adb", *args], capture_output=True, text=True,
                            timeout=300, env=env)
    if check and result.returncode != 0:
        raise SystemExit(f"adb failed: {args}: {result.stderr}")
    return result.stdout


def launch_android(serial, root, port):
    adb("shell", "appops", "set", "--uid", PKG, "MANAGE_EXTERNAL_STORAGE", "allow",
        check=False, serial=serial)
    adb("shell", "am", "force-stop", PKG, check=False, serial=serial)
    adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity",
        "--es", "photto.test.token", TOKEN, "--ei", "photto.test.port", str(port),
        "--es", "photto.test.root", root, serial=serial)
    adb("forward", f"tcp:{port}", f"tcp:{port}", serial=serial)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            req(base, "/api/ping", timeout=2)
            return base
        except Exception:
            time.sleep(0.3)
    raise SystemExit("android app did not answer")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5554"),
                        help="adb serial (default: emulator-5554)")
    parser.add_argument("--port", type=int, default=8811, help="desktop backend port")
    parser.add_argument("--images", type=int, default=3000, help="fixture image count")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    android = launch_android(args.serial, LARGE_DEV, ANDROID_PORT)
    print("android before restage:", json.dumps(
        req(android, "/api/files?limit=1")["counts"], sort_keys=True))

    stage = Path(tempfile.mkdtemp(prefix="photto-parity-"))
    work = stage / "library"
    subprocess.run([sys.executable, str(REPO / "tools/make_fixture.py"), str(work),
                    "--images", str(args.images), "--json"],
                   check=True, stdout=subprocess.DEVNULL)
    # Whole-second mtimes on both sides: pax headers keep fractions, and the
    # device's tar extraction rounds them, which would skew the comparison.
    for path in work.rglob("*"):
        if path.is_file():
            sec = int(path.stat().st_mtime)
            os.utime(path, (sec, sec))
    tar = stage / "library.tar"
    with tarfile.open(tar, "w", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(work.rglob("*")):
            archive.add(path, arcname=str(path.relative_to(work)), recursive=False)

    adb("shell", "rm", "-rf", LARGE_DEV, serial=args.serial)
    adb("shell", "mkdir", "-p", LARGE_DEV, serial=args.serial)
    adb("push", str(tar), "/data/local/tmp/photto-parity.tar", serial=args.serial)
    adb("shell", "tar", "-xf", "/data/local/tmp/photto-parity.tar", "-C", LARGE_DEV,
        serial=args.serial)
    req(android, "/api/files?refresh=1&limit=1")
    scan = wait_complete(android, timeout=120)
    print("android after restage:", scan["counts"]["total"], scan["scan"]["state"])

    desktop = f"http://127.0.0.1:{args.port}"
    server = subprocess.Popen(
        [sys.executable, str(REPO / "photto.py"), str(work), "--window", "none",
         "--token", TOKEN, "--port", str(args.port)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                req(desktop, "/api/ping", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        scan = wait_complete(desktop, timeout=120)
        print("desktop:", scan["counts"]["total"], scan["scan"]["state"])

        # Start from the same decision state on both backends.
        req(android, "/api/reset", {})
        req(desktop, "/api/reset", {})
        req(android, "/api/decide", {"path": KEEP, "decision": "keep"})
        req(desktop, "/api/decide", {"path": KEEP, "decision": "keep"})

        payload = {"android": {}, "desktop": {}, "checks": [], "mismatches": []}

        def compare(label, android_rows, desktop_rows):
            fields = ("path", "name", "dir", "ext", "kind", "size", "sizeH", "mtime", "decision")
            a_view = [{key: row.get(key) for key in fields} for row in android_rows]
            d_view = [{key: row.get(key) for key in fields} for row in desktop_rows]
            same_rows = a_view == d_view
            payload["checks"].append({
                "label": label, "ok": same_rows,
                "android": len(a_view), "desktop": len(d_view), "sameRows": same_rows,
                "sameSet": {row["path"] for row in a_view} == {row["path"] for row in d_view}})
            if not same_rows:
                for index, (a_row, d_row) in enumerate(zip(a_view, d_view)):
                    if a_row != d_row:
                        payload["mismatches"].append({
                            "label": label, "index": index,
                            "android": a_row, "desktop": d_row})
                        break
            print(("PASS " if same_rows else "FAIL ") + label +
                  f" ({len(a_view)} rows)")

        for sort in ("name", "largest", "newest", "oldest"):
            a_rows, a_page = page_all(android, sort=sort)
            d_rows, d_page = page_all(desktop, sort=sort)
            payload["android"][sort] = a_page["counts"]
            payload["desktop"][sort] = d_page["counts"]
            compare(f"{sort} paging order and fields", a_rows, d_rows)

        for group in ("photos", "videos", "audio", "docs", "other"):
            a_rows, _ = page_all(android, sort="name", group=group)
            d_rows, _ = page_all(desktop, sort="name", group=group)
            compare(f"{group} filter order and fields", a_rows, d_rows)

        for decision in ("none", "keep", "delete", "favorite"):
            a_rows, _ = page_all(android, sort="name", decision=decision)
            d_rows, _ = page_all(desktop, sort="name", decision=decision)
            compare(f"{decision} filter", a_rows, d_rows)

        a_counts = req(android, "/api/scan")["counts"]
        d_counts = req(desktop, "/api/scan")["counts"]
        counts_ok = a_counts == d_counts
        payload["checks"].append({"label": "counts object", "ok": counts_ok,
                                  "android": a_counts, "desktop": d_counts})
        print(("PASS " if counts_ok else "FAIL ") + "counts object")

        payload["ok"] = all(check["ok"] for check in payload["checks"])
        (OUT / "parity.json").write_text(json.dumps(payload, indent=2, default=str))
        passed = sum(check["ok"] for check in payload["checks"])
        print(f"{passed}/{len(payload['checks'])} parity checks passed")
        return 0 if payload["ok"] else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        adb("shell", "rm", "-f", "/data/local/tmp/photto-parity.tar", check=False,
            serial=args.serial)
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
