#!/usr/bin/env python3
"""Drive the shared web UI against a real desktop photto server.

This is the desktop counterpart to ``android/ci/emulator_checks.py``. It runs
``photto.py`` on the large fixture, loads the UI in GTK/WebKit2, and exercises
progressive load, keyboard decisions, filters/sorts, Review, favorites move,
and the confirm-to-trash flow.

Requirements: Python 3, GTK 3, WebKit2GTK 4.x, and a display (``DISPLAY`` or
Wayland). It is not part of CI; run it manually on a Linux desktop:

    python3 tools/desktop_check.py
    python3 tools/desktop_check.py /tmp/photto-large --port 8811

Writes ``test-output/desktop.json`` with the measurements and check results.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = Path("test-output")
results: list[tuple[bool, str]] = []
perf: dict[str, object] = {}


def check(cond, msg):
    results.append((bool(cond), msg))
    print(("PASS " if cond else "FAIL ") + msg, flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def req(base: str, token: str, path: str, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data,
        headers={"X-Token": token, "Content-Type": "application/json"},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def page_all(base: str, token: str, *, sort="name", decision="any", group="all", limit=500):
    rows, cursor = [], None
    while True:
        query = f"/api/files?limit={limit}&sort={sort}&group={group}&decision={decision}"
        if cursor:
            query += "&cursor=" + urllib.parse.quote(cursor, safe="")
        page = req(base, token, query)
        rows.extend(page["files"])
        cursor = page.get("nextCursor")
        if not cursor:
            return rows


def wait_until(fn, timeout=60, interval=0.1, what="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise SystemExit(f"timed out waiting for {what}: {last!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", type=Path,
                        default=OUT / "large-fixture", help="fixture folder (default: test-output/large-fixture)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token", default="desktop-check")
    parser.add_argument("--keep", action="store_true", help="keep the temporary library")
    args = parser.parse_args()

    fixture = args.fixture.expanduser().resolve()
    if not fixture.is_dir():
        subprocess.run([sys.executable, str(REPO / "tools" / "make_fixture.py"), str(fixture)],
                       check=True)
    work = Path(tempfile.mkdtemp(prefix="photto-desktop-")) / "library"
    shutil.copytree(fixture, work, copy_function=shutil.copy2)
    expected = sum(1 for path in work.rglob("*") if path.is_file())
    port = args.port or free_port()
    base = f"http://127.0.0.1:{port}"
    token = args.token

    server = subprocess.Popen(
        [sys.executable, str(REPO / "photto.py"), str(work), "--window", "none",
         "--token", token, "--port", str(port)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    try:
        wait_until(lambda: _ping(base, token), timeout=30, what="photto server")

        t0 = time.monotonic()
        first = wait_until(lambda: _first_page(base, token), timeout=60, what="first page")
        perf["firstPageMs"] = round((time.monotonic() - t0) * 1000, 1)
        perf["firstPageState"] = first["scan"]["state"]
        first_total = first["counts"]["total"]
        complete = wait_until(lambda: _scan_complete(base, token), timeout=120, what="full scan")
        perf["fullScanMs"] = round((time.monotonic() - t0) * 1000, 1)
        check(first_total > 0, f"progressive load serves files before the scan finishes ({first_total})")
        check(perf["firstPageMs"] < perf["fullScanMs"],
              f"first page arrives before a full scan ({perf['firstPageMs']} < {perf['fullScanMs']} ms)")
        check(complete["counts"]["total"] == expected,
              f"desktop indexes the whole fixture ({complete['counts']['total']} == {expected})")

        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import GLib, Gtk, WebKit2

        window = Gtk.Window(title="photto desktop check")
        window.set_default_size(1280, 880)
        view = WebKit2.WebView()
        window.add(view)
        loaded = {"done": False}

        def on_load(_view, event):
            if event == WebKit2.LoadEvent.FINISHED:
                loaded["done"] = True
        view.connect("load-changed", on_load)
        window.show_all()
        view.load_uri(f"{base}/#t={token}&debug=1")

        def pump(seconds=0.02):
            context = GLib.MainContext.default()
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                time.sleep(0.005)

        def js(script: str, timeout=60):
            box: dict[str, str] = {}

            def done(v, result, _data):
                try:
                    value = v.run_javascript_finish(result).get_js_value()
                    box["value"] = value.to_string()
                except Exception as exc:  # noqa: BLE001
                    box["error"] = repr(exc)
            view.run_javascript(script, None, done, None)
            deadline = time.monotonic() + timeout
            while "value" not in box and "error" not in box and time.monotonic() < deadline:
                pump()
            if "error" in box:
                raise RuntimeError(box["error"])
            if "value" not in box:
                raise TimeoutError(script[:120])
            return box["value"]

        def js_json(expr):
            raw = js("JSON.stringify(" + expr + ")")
            return json.loads(raw) if raw not in ("", "undefined") else None

        def wait_js(expr, timeout=60, what="client state"):
            return wait_until(lambda: js_json(expr), timeout=timeout, interval=0.1, what=what)

        def key(name):
            js("document.dispatchEvent(new KeyboardEvent('keydown',{key:%s,bubbles:true})); 'ok'"
               % json.dumps(name))

        load_deadline = time.monotonic() + 30
        while not loaded["done"] and time.monotonic() < load_deadline:
            pump()
        if not loaded["done"]:
            raise SystemExit("timed out waiting for WebView load")
        wait_js("typeof S !== 'undefined' && S.deck.length >= 12", what="deck")
        marks = js_json("window.__phottoPerf || null") or {}
        check(isinstance(marks.get("firstPageMs"), (int, float)) and
              isinstance(marks.get("firstCardMs"), (int, float)),
              f"the UI records first-page and first-card marks ({marks})")

        # Keyboard decisions, undo, and skip.
        before = req(base, token, "/api/scan")["counts"]
        top = js_json("S.deck[0].path")
        key("ArrowLeft")
        wait_until(lambda: req(base, token, "/api/scan")["counts"]["byDecision"]["delete"] ==
                   before["byDecision"]["delete"] + 1, timeout=10, what="delete decision")
        check(top in [r["path"] for r in page_all(base, token, decision="delete")],
              "ArrowLeft marks the top file for deletion")
        key("z")
        wait_until(lambda: req(base, token, "/api/scan")["counts"]["byDecision"]["delete"] ==
                   before["byDecision"]["delete"], timeout=10, what="undo")
        check(top not in [r["path"] for r in page_all(base, token, decision="delete")],
              "z undoes the mark")
        key("ArrowRight")
        wait_until(lambda: req(base, token, "/api/scan")["counts"]["byDecision"]["keep"] >=
                   before["byDecision"]["keep"] + 1, timeout=10, what="keep decision")
        check(True, "ArrowRight keeps a file")
        key("ArrowUp")
        wait_until(lambda: req(base, token, "/api/scan")["counts"]["byDecision"]["favorite"] >=
                   before["byDecision"]["favorite"] + 1, timeout=10, what="favorite decision")
        skipped = js_json("S.deck[0].path")
        key("ArrowDown")
        time.sleep(0.5)
        check(skipped not in [r["path"] for r in
                              page_all(base, token, decision="delete") +
                              page_all(base, token, decision="keep") +
                              page_all(base, token, decision="favorite")],
              "ArrowDown skips without marking the file")
        req(base, token, "/api/reset", {})
        js("load(); 'ok'")
        wait_js("S.deck.length >= 12 && S.counts && S.counts.total > 0", what="reloaded deck")

        # Viewer and picker overlays, closed by Escape.
        key("Enter")
        wait_js("!document.querySelector('#viewer').classList.contains('hidden')", what="viewer")
        key("Escape")
        wait_js("document.querySelector('#viewer').classList.contains('hidden')", what="viewer close")
        key("g")
        wait_js("!document.querySelector('#picker').classList.contains('hidden')", what="picker")
        key("Escape")
        wait_js("document.querySelector('#picker').classList.contains('hidden')", what="picker close")
        check(True, "viewer and picker open from the keyboard and close with Escape")

        # Filter and sort controls reset the deck and cursor.
        js("[...document.querySelectorAll('#filters button')]"
           ".find(b => b.textContent.startsWith('Photos')).click(); 'ok'")
        wait_js("S.filter === 'photos' && S.deck.length > 0", what="photos filter")
        kinds = js_json("S.deck.slice(0, 20).map(f => f.kind)")
        check(kinds and all(kind in ("image", "image-convert") for kind in kinds),
              "the Photos filter shows only images")
        js("const s=document.querySelector('#sort'); s.value='newest';"
           "s.dispatchEvent(new Event('change')); 'ok'")
        wait_js("S.sort === 'newest' && S.deck.length >= 12", what="newest sort")
        mtimes = js_json("S.deck.slice(0, 20).map(f => f.mtime)")
        check(all(a >= b for a, b in zip(mtimes, mtimes[1:])), "newest sort is descending")
        js("S.filter='all'; S.sort='name'; resetDeck(); 'ok'")
        wait_js("S.filter === 'all' && S.deck.length >= 12", what="reset deck")

        # Review: 300 marks, paged tiles, lazy images.
        rows = page_all(base, token, decision="none", sort="name")
        marked = [row["path"] for row in rows[:300]]
        for path in marked:
            req(base, token, "/api/decide", {"path": path, "decision": "delete"})
        counts = req(base, token, "/api/scan")["counts"]
        check(counts["byDecision"]["delete"] == 300, "300 marks reach the desktop index")
        js("load(); 'ok'")
        wait_js("S.deck.length >= 12", what="deck before review")
        key("r")
        wait_js("!document.querySelector('#review').classList.contains('hidden')", what="Review")
        review = js_json("""(()=>{const g=document.querySelector('#reviewGrid');
          return {tiles:g.querySelectorAll('.tile').length,
                  loaded:g.querySelectorAll('img[src]').length};})()""")
        check(review and 0 < review["tiles"] <= 100, f"Review renders a paged first batch ({review})")
        check(review and 0 < review["loaded"] < review["tiles"], "offscreen Review tiles stay unloaded")
        key("Escape")
        wait_js("document.querySelector('#review').classList.contains('hidden')", what="Review close")

        # Favorites move plus confirm-to-trash through the real button path.
        favorite = next(row["path"] for row in rows[300:] if row["kind"] in ("image", "image-convert"))
        req(base, token, "/api/decide", {"path": favorite, "decision": "favorite"})
        (work / "Favorites").mkdir(exist_ok=True)
        js("document.querySelector('#favMove').checked = true;"
           "window.__commitDone = false;"
           "(async()=>{await commit(); await commit(); window.__commitDone = true;})(); 'started'")
        wait_until(lambda: js_json("window.__commitDone === true"), timeout=180,
                   interval=0.2, what="commit")
        after = wait_until(lambda: (lambda d: d if d["scan"]["state"] != "scanning" else None)
                           (req(base, token, "/api/scan")), timeout=120, what="post-commit scan")
        check(after["counts"]["total"] == expected - 300,
              f"commit trashes the 300 marked files ({after['counts']['total']})")
        check(not (work / favorite).exists() and (work / "Favorites" / Path(favorite).name).exists(),
              "favorites move into Favorites/")
        check(after["counts"]["byDecision"]["delete"] == 0,
              "trashed files lose their delete marks")
        removed = next((row["path"] for row in rows if row["path"] in set(marked)), marked[0])
        check(not (work / removed).exists(), "a committed file is gone from disk")
        # The commit starts a revalidation scan; let the UI's poll pick up the
        # final counts instead of sampling in the middle of that scan.
        ui_total = wait_until(lambda: (lambda n: n if n == after["counts"]["total"] else None)
                              (js_json("S.counts && S.counts.total")), timeout=15,
                              interval=0.2, what="UI counts after commit")
        check(ui_total == after["counts"]["total"], f"the UI agrees with the server after commit ({ui_total})")

        # Keep the WebView alive long enough to prove no stale card was left.
        wait_js("S.deck.length > 0", what="deck after commit")

    finally:
        try:
            server.terminate()
            server.wait(timeout=10)
        except Exception:  # noqa: BLE001
            server.kill()
        if not args.keep:
            shutil.rmtree(work.parent, ignore_errors=True)

    perf["checksPassed"] = sum(ok for ok, _ in results)
    perf["checksTotal"] = len(results)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "desktop.json").write_text(json.dumps(perf, indent=2))
    (OUT / "desktop-results.txt").write_text(
        "\n".join(("PASS " if ok else "FAIL ") + msg for ok, msg in results))
    print(f"\n{perf['checksPassed']}/{perf['checksTotal']} desktop checks passed")
    return 0 if perf["checksPassed"] == perf["checksTotal"] else 1


def _ping(base, token):
    try:
        return req(base, token, "/api/ping", timeout=2).get("ok") is True
    except Exception:  # noqa: BLE001
        return False


def _first_page(base, token):
    try:
        page = req(base, token, "/api/files?limit=100&decision=none", timeout=5)
        return page if page.get("files") else None
    except Exception:  # noqa: BLE001
        return None


def _scan_complete(base, token):
    try:
        page = req(base, token, "/api/scan", timeout=10)
        return page if page["scan"]["state"] != "scanning" else None
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    raise SystemExit(main())
