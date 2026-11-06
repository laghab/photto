# Spec Delta

## Purpose

Make large folders usable by exposing file metadata progressively, reusing a persisted per-folder index, and keeping paged results consistent with saved decisions.

## ADDED Requirements

### Requirement: Progressive availability

For a folder larger than one page, the system SHALL expose an initial page of file metadata and allow the deck to present a card before the full folder scan finishes, while reporting that indexing is still in progress.

#### Scenario: Large folder first open

- **WHEN** the user opens a folder whose scan takes longer than producing one page
- **THEN** the first page is returned and a card is presented before indexing completes
- **AND** the UI reports that indexing is still in progress

#### Scenario: Small folder

- **WHEN** the folder fits within one page
- **THEN** the page is returned and indexing completes without a lingering progress state

### Requirement: Bounded page sizes

The API SHALL return file metadata in bounded pages, include total and per-decision counts plus scan state, and SHALL clamp requests that exceed the configured maximum page size.

#### Scenario: Page request

- **WHEN** the client requests a page with an offset and limit
- **THEN** the response contains no more than the effective limit and includes totals and scan state

#### Scenario: Oversized limit

- **WHEN** the requested limit exceeds the configured maximum
- **THEN** the server clamps it to the maximum and reports the effective limit

#### Scenario: Offset beyond the end

- **WHEN** the requested offset is at or beyond the total number of files
- **THEN** the response returns an empty page with the current totals rather than an error

### Requirement: Persisted index reuse

The system SHALL persist the index for each opened folder and reuse it on subsequent opens so that a warm open can present files before a full revalidation completes.

#### Scenario: Warm open

- **WHEN** the user reopens a folder that has a usable persisted index
- **THEN** files are available from that index without waiting for a full rescan
- **AND** revalidation runs in the background

#### Scenario: Unusable index

- **WHEN** the persisted index is missing, unreadable, or from an incompatible version
- **THEN** the system rebuilds it and reports a scanning state instead of returning stale metadata

### Requirement: Revalidation freshness

Revalidation SHALL reflect files added, removed, or changed outside the app, and SHALL remove saved decisions only for files that no longer exist.

#### Scenario: File removed externally

- **WHEN** a file with a saved decision is removed from the folder before revalidation
- **THEN** the file is omitted from the index and its saved decision is removed

#### Scenario: File added externally

- **WHEN** a file is added to the folder before revalidation
- **THEN** it appears without a decision after revalidation or an explicit refresh

#### Scenario: Change outside the loaded page

- **WHEN** a file outside the currently loaded page is removed
- **THEN** its saved decision is cleaned up during revalidation
- **AND** decisions for existing files remain intact

### Requirement: Refresh and error states

The user SHALL be able to force a fresh scan, and the system SHALL report scanning, fresh, stale, or error states without discarding readable results.

#### Scenario: Explicit refresh

- **WHEN** the user requests a refresh
- **THEN** a fresh scan starts and the UI shows its state

#### Scenario: Partial read failure

- **WHEN** part of the folder cannot be read
- **THEN** the readable results remain available
- **AND** the error state is reported and the user can retry

#### Scenario: Permission lost

- **WHEN** the app loses storage permission
- **THEN** the system reports a permission state and does not present cached entries as actionable files

### Requirement: Stable ordering across pages

While the index is stable, paged results SHALL follow the selected order with no duplicates or omissions; if revalidation changes the index during paging, the response SHALL expose a change marker so the client can reconcile.

#### Scenario: Newest-first paging

- **WHEN** the user pages through a stable folder ordered newest first
- **THEN** each file appears exactly once and the order is preserved

#### Scenario: Change during paging

- **WHEN** revalidation changes the index while the client is paging
- **THEN** the response exposes an index revision or change marker
- **AND** the client can reconcile without showing duplicates

### Requirement: Decision continuity

Saved decisions SHALL remain attached to existing files across page loads, filter changes, app restarts, and index rebuilds, and files with decisions SHALL not be dealt back into the deck.

#### Scenario: App restart

- **WHEN** the app restarts and reopens the folder
- **THEN** previously decided files retain their decisions and are not in the deck

#### Scenario: Index rebuild

- **WHEN** the index is rebuilt for the same folder
- **THEN** decisions for files that still exist remain

#### Scenario: Filter change

- **WHEN** the user changes the type filter
- **THEN** decisions are preserved and counts reflect them

### Requirement: Cross-platform contract

The Android and desktop backends SHALL expose the same paged index fields and semantics for equivalent folders so the shared UI does not branch by platform.

#### Scenario: Equivalent page requests

- **WHEN** the same page request is made against both backends for equivalent folders
- **THEN** response fields, ordering rules, counts, and scan-state semantics match
