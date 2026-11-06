# Proposal

## Why

The SIFT name currently belongs to a command-line OCR sorter, while this repository contains the more capable cross-platform review GUI the user wants SIFT to become. Bringing the SIFT text-finding workflow into this GUI gives the product one accessible interface across desktop and Android, with a simple `sift [DIR]` launcher for people who start from a terminal.

## What Changes

- Make the existing desktop and Android GUI the SIFT product, updating visible branding, installers, release artifacts, and documentation while preserving existing user data and upgrade identity.
- Refresh the GUI's visual design and review flow with a calm light palette, clearer navigation and hierarchy, a balanced media canvas, and compact touch-friendly controls.
- Bring SIFT's local OCR workflow into the GUI: image and sampled-video scans, language and meaningful-word threshold settings, case-insensitive text search, progress, and cancellation.
- Show matches for review before file operations. Let users preview destinations, then explicitly copy or move matches into a `SIFT_docs` folder that mirrors their source subfolders and resolves name collisions safely.
- Reduce the `sift` command to launching the GUI for an optional directory. Remove command-line OCR scanning, JSON output, and scan flags.
- Keep OCR local. Use the desktop Tesseract and FFmpeg tools and an on-device Android OCR implementation; do not send media or recognized text to a cloud service.
- Replace the existing `laghab/sift` GitHub project with this GUI project after implementation and release checks are complete. Preserve this repository's history, and delete the old SIFT repository as requested.

### Out of Scope

- Cloud OCR or AI classification.
- Maintaining the old standalone Go scanner or its JSON command-line interface.
- Renaming the Android application ID if that would prevent existing Photto installs from upgrading; the visible app brand changes to SIFT while upgrade identity and data remain compatible.

## Capabilities

### New Capabilities

- `text-discovery`: Local OCR jobs over images and sampled video, with filtering, progress, cancellation, and cached results.
- `text-result-actions`: Review OCR matches and explicitly copy or move them into a mirrored `SIFT_docs` destination.
- `sift-launcher`: SIFT product identity and GUI launch behavior, including a minimal `sift [DIR]` command.

### Modified Capabilities

None. This repository has no main specs yet.

## Impact

- **Shared UI:** `static/app.js`, `static/index.html`, and `static/style.css` for SIFT branding, OCR job controls, match review, and file actions.
- **Desktop backend:** `photto.py` for OCR job routes, local Tesseract/FFmpeg integration, and launcher behavior.
- **Android backend:** `android/app/src/main/kotlin/io/github/laghab/photto/Api.kt`, `MainActivity.kt`, and `FolderIndex.kt` for local OCR, video frame sampling, result caching, job progress, and confirmed actions.
- **Packaging and identity:** Android Gradle/manifest files, Windows and Arch packaging, CI release artifacts, `README.md`, and data-path migration.
- **External project cutover:** GitHub repositories `laghab/photto` and `laghab/sift`; old SIFT deletion and current GUI rename happen only after the replacement passes its release checks.
