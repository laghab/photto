<p align="center"><img src="data/photto.svg" width="96" alt=""></p>

<h1 align="center">photto</h1>

<p align="center">Tinder for your files on Linux, Windows and Android. Swipe through a folder's photos, videos, music,<br>
PDFs, documents and everything else, and decide what stays.</p>

---

Open a folder and photto deals its files out one card at a time. Swipe right to
keep a file, left to delete it, or up to favorite it. When you're done, check
the pile in **Review** and send the rejects to the Trash in one go.

It's inspired by [photoo](https://github.com/xigua222/photoo) for Android, and
works for any file type.

## Install

### Windows 10 / 11
Download **`photto-…-windows-setup.exe`** from the [latest release](https://github.com/laghab/photto/releases/latest)
and run it. No admin rights are needed. It adds photto to the Start Menu, and right-clicking a folder
offers **Sort with photto**. There's also a portable `.exe` if you'd rather not install.
Deleted files go to the **Recycle Bin**.

### Android 11+
Download **`photto-…-android.apk`** from the [latest release](https://github.com/laghab/photto/releases/latest)
and open it to install (allow "install unknown apps" for your browser or file manager when asked).
On first launch, tap **Allow access** to grant *All files access*. Deleted photos, videos and music go to
Android's **trash**, where the Files or Photos app can restore them for 30 days; Android asks you to confirm
each batch. Other file types that Android won't trash are moved into a `photto trash` folder instead.

### Linux

```sh
curl -fsSL https://raw.githubusercontent.com/laghab/photto/main/install.sh | bash
```

This installs photto for your user only, with no root needed. It shows up in
your app menu like any other app. To remove it:

```sh
curl -fsSL https://raw.githubusercontent.com/laghab/photto/main/uninstall.sh | bash
```

**Arch / CachyOS / Manjaro:** a [PKGBUILD](packaging/arch/PKGBUILD) is included.

```sh
git clone https://github.com/laghab/photto && cd photto/packaging/arch && makepkg -si
```

**From a clone:** `./install.sh`, or just run `python3 photto.py` without installing.

### Linux requirements

Python 3.10+ is the only hard requirement. Optional extras improve previews:

| Package | What it adds |
|---|---|
| `python-gobject` + `webkit2gtk-4.1` | a proper app window (otherwise a Chromium app window or your browser is used) |
| `ffmpeg` / `ffmpegthumbnailer` | video thumbnails, duration and resolution |
| `poppler` | PDF thumbnails and page-by-page reading |
| `imagemagick` or `libvips` | HEIC, RAW and TIFF photos; fast thumbnails |
| `libreoffice` | previews of `.docx`, `.xlsx`, `.pptx` and friends |

## Use it

**From the app menu:** open *photto* and pick a folder. Recent folders are
remembered.

**From a terminal (optional):**

```sh
cd ~/Pictures/phone-dump && photto     # this folder and its subfolders
photto ~/Downloads                     # any folder
photto -t ~/Downloads                  # top level only, skip subfolders
```

Closing the window quits photto.

### Controls

| | Swipe | Keyboard | Button |
|---|---|---|---|
| Delete | ← | `←` `X` | ✕ |
| Keep | → | `→` `K` | ✓ |
| Favorite | ↑ | `↑` `F` | ★ |
| Decide later | ↓ | `↓` `S` | ↓ |
| Undo | | `Z` `Backspace` | ↶ |
| Full view | double-click | `Enter` | ⤢ |
| Open in its usual app | | `O` | |
| Play/pause, mute | click | `Space`, `M` | |
| Review | | `R` | Review |
| Change folder | | `G` | folder name |

Use the filter chips to work on one type at a time (Photos, Videos, Audio,
Docs, Other). Order can be shuffle, newest, oldest, largest or by name.

## Your files are safe

- **Swiping only marks files.** Nothing on disk changes until you press
  *Move to Trash* in Review and then confirm it.
- **Deleted files go to the system trash**: the Linux Trash, the Windows
  Recycle Bin, or Android's trash. You can restore them from there.
- **Your marks are saved** in `~/.local/state/photto/`. You can quit halfway
  and carry on later, and files you've already sorted won't come back.
- **Favorites stay where they are.** Review can optionally move them into a
  `Favorites/` subfolder.
- **Everything stays on your device.** photto serves its UI on `127.0.0.1`
  only, behind a random per-run token. There's no network access and no
  telemetry. (The Android app requests the Internet permission only so it can talk to
  its own built-in local server.)

Hidden files and folders are skipped, and so are `.git`, `node_modules` and
similar clutter. Use `-a` to include hidden ones.

## Command-line options

```
photto [DIR] [options]
  -t, --top-only     only DIR itself, not its subfolders
  -a, --all          include hidden files and folders
  --window MODE      auto | native | app | browser | none
  --browser          open in your default browser
  --no-browser       just print the URL (e.g. to open it from another device via SSH tunnel)
  -p, --port N       fixed port instead of a random one
  --version
```

## License

MIT

## How it's built

One web UI (`static/`) is shared by every platform:

- **Linux / Windows:** `photto.py` (Python standard library only) serves the UI and file API on
  127.0.0.1. It shows the UI in a GTK/WebKit window on Linux, or an Edge/Chrome app window on Windows,
  falling back to the browser. Windows builds are made with PyInstaller and Inno Setup.
- **Android:** a small Kotlin app (`android/`, no third-party libraries) runs the same API in-process
  and shows the UI in a WebView.

CI builds every platform on each push. It runs the Windows build on a real Windows VM, including the
Recycle Bin and the installer, and runs the APK on an Android emulator, swiping and trashing files
for real. Tagged versions are published as GitHub releases.
