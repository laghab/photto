# Spec Delta

## Purpose

Lets users inspect OCR matches and organize them safely from the SIFT GUI, preserving folder context and preventing accidental overwrites.

## ADDED Requirements

### Requirement: Review before file operations
OCR scanning and result review SHALL leave source files unchanged until the user explicitly confirms a copy or move operation.

#### Scenario: Review without confirming
- **WHEN** the user scans a folder, reviews matches, or closes the results view without confirming an operation
- **THEN** no source file is copied, moved, or deleted

#### Scenario: Confirm an operation
- **WHEN** the user confirms a copy or move from the reviewed results
- **THEN** only the selected matching files are processed using the chosen action

### Requirement: Mirrored SIFT_docs destinations
The GUI SHALL preview each destination under `SIFT_docs` and preserve the file's relative subfolder path beneath the selected root.

#### Scenario: Match is nested in a subfolder
- **WHEN** a selected match is located in a nested source folder
- **THEN** its proposed destination mirrors that relative folder path under `SIFT_docs`

#### Scenario: Destination name already exists
- **WHEN** a destination filename already exists
- **THEN** the GUI proposes a unique filename and does not overwrite the existing file

### Requirement: Per-file action outcomes
The GUI SHALL report success and failure for each selected copy or move so partial operation results are clear.

#### Scenario: Source changes before confirmation
- **WHEN** a selected source file is missing or unreadable when the confirmed operation runs
- **THEN** that file is reported as failed, other selected files continue, and no unrelated destination is overwritten

#### Scenario: Operation completes
- **WHEN** processing of the selected matches finishes
- **THEN** the GUI reports copied, moved, and failed counts and keeps the result list available for review
