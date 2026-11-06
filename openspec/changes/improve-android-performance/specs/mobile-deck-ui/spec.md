# Spec Delta

## Purpose

Keep swiping, reviewing, and starting the app responsive on Android phones while preserving the shared desktop experience and the existing safety model.

## ADDED Requirements

### Requirement: Frame-coalesced gestures

Pointer movement SHALL update the dragged card at most once per animation frame using transform and opacity only, and SHALL NOT read layout or restyle offscreen content during the gesture.

#### Scenario: Fast drag

- **WHEN** the user moves a finger quickly across the top card
- **THEN** visual updates are coalesced per frame and the card follows without jumping

#### Scenario: Release past threshold

- **WHEN** the user releases the card past a decision threshold
- **THEN** exactly one decision is recorded and the next card becomes interactive

### Requirement: Smooth decision animations

Decision animations SHALL use compositor-friendly properties and SHALL complete without delaying the next card's interactivity.

#### Scenario: Swipe left

- **WHEN** the user marks a file for deletion
- **THEN** the card animates out with transform and opacity while the next card is already interactive

#### Scenario: Reduced motion

- **WHEN** the system reduced-motion setting is enabled
- **THEN** animations are shortened or removed and the decision still completes

### Requirement: Bounded live deck

The UI SHALL keep only a bounded number of card elements and media elements alive, and SHALL tear down media when cards leave the deck.

#### Scenario: Swiping through many files

- **WHEN** the user swipes repeatedly through a large folder
- **THEN** the number of live card elements and media elements remains bounded

#### Scenario: Video card leaves the deck

- **WHEN** a video card is removed from the deck
- **THEN** playback stops and its media source is released

### Requirement: Bounded Review rendering

Opening Review with many marked files SHALL NOT render or request thumbnails for every tile at once; only a bounded working set or visible region SHALL be active.

#### Scenario: One thousand delete marks

- **WHEN** the user opens Review with 1,000 marked files
- **THEN** Review opens promptly and only visible or near-visible thumbnails are requested

#### Scenario: Scrolling Review

- **WHEN** the user scrolls through a large Review list
- **THEN** tiles are populated incrementally without long main-thread stalls

### Requirement: Updates independent of folder size

Filter counts, progress, the delete count, and Review lists SHALL update without scanning the entire file list on every swipe or render.

#### Scenario: Swipe in a 50,000-file folder

- **WHEN** the user makes a decision in a folder with 50,000 files
- **THEN** the time to update counts and present the next card does not grow with the total folder size

#### Scenario: Filter switch

- **WHEN** the user switches type filters in a large folder
- **THEN** the switch remains responsive and the counts are correct

### Requirement: Non-blocking startup

The UI SHALL remain interactive while scanning, showing cards as they become available and indicating progress instead of blocking on a full-screen spinner.

#### Scenario: First open of a large folder

- **WHEN** scanning takes longer than producing the first page
- **THEN** a card is shown and the controls respond while scanning continues

#### Scenario: Empty folder

- **WHEN** the folder contains no files
- **THEN** the empty state appears without waiting for a nonexistent scan

### Requirement: Android visual profile

On Android, the UI SHALL avoid backdrop filters, turbulence or grain overlays, large blurred background images, and heavy shadows.

#### Scenario: Android deck

- **WHEN** the app runs on Android
- **THEN** the deck uses the mobile visual profile and does not paint expensive blur or grain effects

#### Scenario: Desktop deck

- **WHEN** the app runs on desktop
- **THEN** the existing visual effects remain available

### Requirement: Safety model unchanged

Performance changes SHALL NOT alter the confirmation model: marks remain reversible until commit, and confirmed deletion still uses the system trash.

#### Scenario: Open and close Review

- **WHEN** the user opens and closes Review without committing
- **THEN** no file is modified

#### Scenario: Confirm deletion

- **WHEN** the user confirms deletion
- **THEN** files are moved to the system trash exactly as before

### Requirement: Desktop behavior preserved

The shared UI SHALL continue to support desktop keyboard controls, filters, sorting, the viewer, and Review.

#### Scenario: Desktop keyboard

- **WHEN** the user presses the arrow keys on desktop
- **THEN** the same keep, delete, favorite, and later decisions occur

#### Scenario: Desktop large folder

- **WHEN** the user opens a large folder on desktop
- **THEN** progressive loading works there as well without regressing existing controls
