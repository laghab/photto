# Spec Delta

## Purpose

Lets users find photos and sampled video frames that contain useful text from the SIFT GUI, with OCR and recognized text kept on the user's device.

## ADDED Requirements

### Requirement: Local image text discovery
The SIFT GUI SHALL scan supported images in the selected folder and report files whose recognized text meets the active match rule.

#### Scenario: Image contains enough text
- **WHEN** an image's OCR result contains at least the configured number of meaningful words
- **THEN** the image appears in the text-discovery results

#### Scenario: Image is below threshold
- **WHEN** an image's OCR result contains fewer meaningful words than the configured threshold
- **THEN** the image does not appear as a match

### Requirement: Text search filter
The SIFT GUI SHALL allow users to filter OCR results by a case-insensitive text query; a query match qualifies independently of the word threshold.

#### Scenario: Search finds text
- **WHEN** a search query is active and recognized text contains that query without regard to case
- **THEN** the file appears in the results and the matching text is available for review

#### Scenario: Search excludes non-matches
- **WHEN** a search query is active and recognized text does not contain that query
- **THEN** the file does not appear in the results

#### Scenario: Search for one word
- **WHEN** the user searches for a phrase found in recognized text that is below the default word threshold
- **THEN** the file appears as a search match

### Requirement: Configurable OCR matching
The GUI SHALL let users choose an available OCR language and a minimum meaningful-word threshold, defaulting to two words of at least three letters each when no text query is active.

#### Scenario: Default document threshold
- **WHEN** a scan runs with no search query and no saved threshold preference
- **THEN** a file matches only if its recognized text contains at least two meaningful words

#### Scenario: Custom threshold
- **WHEN** the user sets a different threshold and starts a scan
- **THEN** the results use that threshold until the user changes it

### Requirement: Sampled video text discovery
The SIFT GUI SHALL inspect supported videos at regular sampled-frame intervals and report each video at most once when its sampled OCR text meets the active match rule.

#### Scenario: Text appears in a sampled frame
- **WHEN** a sampled video frame contains text that meets the active match rules
- **THEN** the video appears once in the results with a representative frame and recognized text

#### Scenario: Video has no matching sampled text
- **WHEN** no sampled frame meets the active match rules
- **THEN** the video does not appear as a match

#### Scenario: Sampling interval follows video duration
- **WHEN** the GUI samples videos shorter than 15 seconds, from 15 to under 60 seconds, or at least 60 seconds long
- **THEN** it samples at intervals of 1, 2, or 5 seconds respectively

### Requirement: Reuse OCR results for unchanged files
The GUI SHALL reuse a prior local OCR result when the source file, language, and OCR engine version are unchanged, and SHALL rescan when any of them changes.

#### Scenario: Unchanged image is scanned again
- **WHEN** a later job includes a file with an unchanged identity and the same OCR language and engine version
- **THEN** its cached OCR result is reused

#### Scenario: Source or OCR settings change
- **WHEN** a file's modification identity, selected language, or OCR engine version changes
- **THEN** the previous OCR result is not used for that scan

### Requirement: OCR job progress and cancellation
The SIFT GUI SHALL report scan progress and allow a user to cancel a running OCR job without starting file operations.

#### Scenario: Cancel a scan
- **WHEN** the user cancels a running OCR job
- **THEN** new files and frames are not submitted for OCR, completed results remain reviewable, and no source files are moved or copied

#### Scenario: OCR engine unavailable
- **WHEN** the selected platform's local OCR engine or a required video decoder is unavailable
- **THEN** the GUI identifies the missing component and the affected scan mode, and does not report those files as non-matches

### Requirement: Local-only OCR processing
The SIFT GUI SHALL perform OCR on-device and SHALL NOT upload source media or recognized text to a remote service.

#### Scenario: Run OCR offline
- **WHEN** the user scans a folder without network access
- **THEN** the OCR job can process supported files using locally available models and tools
