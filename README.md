<p align="center"><img src="data/sift.svg" width="96" alt=""></p>

<h1 align="center">SIFT</h1>

SIFT is a local-first desktop and Android app for reviewing folders and finding text in images and videos. Browse one file at a time, keep or favorite it, mark unwanted files for review, and use on-device OCR to find receipts, labels, screenshots, and other text.

## Install

Download the latest Windows installer or Android APK from [GitHub Releases](https://github.com/laghab/sift/releases/latest). On Linux, install with:

```sh
curl -fsSL https://raw.githubusercontent.com/laghab/sift/main/install.sh | bash
```

Arch Linux packaging is in [`packaging/arch`](packaging/arch/PKGBUILD).

## Open a folder

```sh
sift                         # open SIFT and choose a folder
sift ~/Pictures/phone-dump   # open this folder
sift .                       # open the current folder
```

The terminal command only opens the GUI. OCR scanning and file organization happen in the app. The former command-line OCR flags are no longer supported.

Large folders open without waiting for a full scan: SIFT indexes in the background and loads cards a page at a time, and Review pages in as you scroll. See [`docs/performance.md`](docs/performance.md) for the model and the measured budgets.

## Find text

Choose **Find text**, select images or video, set a language and minimum meaningful-word count, or enter a text query. Image OCR uses local Tesseract on desktop and a bundled offline Latin model on Android. Desktop video OCR uses FFmpeg and FFprobe; Android samples local video frames on-device. Video sampling is once per second below 15 seconds, every two seconds below one minute, and every five seconds for longer clips.

A meaningful word contains at least three Unicode letters. The default threshold is two words. A case-insensitive query match qualifies on its own. Results can be reviewed and copied or moved into a mirrored `SIFT_docs` folder after confirmation. Existing files are preserved; conflicts get a `_siftedN` suffix.

OCR and file handling stay on your device. Review marks are saved per folder. Existing Android installs keep their package identity, and desktop state is copied from the old Photto data directory only when a SIFT state directory does not already exist.

## Development

```sh
python3 photto.py /path/to/folder
python3 -m unittest discover -s tests -v
```

The UI is shared from `static/`, with Python desktop and Kotlin Android backends. See [`docs/sift-ocr.md`](docs/sift-ocr.md) for OCR matching and fixture details.

## License

MIT
