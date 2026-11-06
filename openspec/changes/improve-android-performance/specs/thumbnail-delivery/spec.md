# Spec Delta

## Purpose

Deliver card and review previews within a bounded work budget, with caches and prioritization that keep decks and large review grids responsive.

## ADDED Requirements

### Requirement: Bounded concurrency

The system SHALL limit how many thumbnail generations run concurrently and SHALL queue excess requests instead of starting unbounded work.

#### Scenario: Large review grid

- **WHEN** Review requests more thumbnails than the configured concurrency limit
- **THEN** at most the configured number are generated at once and the remainder wait

#### Scenario: Deck and Review at once

- **WHEN** the deck and Review request thumbnails at the same time
- **THEN** visible deck requests are served ahead of offscreen Review requests

### Requirement: Visible-first priority

Thumbnail requests for the top card and visible Review tiles SHALL take priority over prefetch and offscreen requests.

#### Scenario: New top card

- **WHEN** the user swipes and a new card becomes the top card
- **THEN** its thumbnail is scheduled ahead of queued offscreen thumbnails

#### Scenario: Review scroll

- **WHEN** the user scrolls Review
- **THEN** newly visible tiles are prioritized over tiles that remain offscreen

### Requirement: Bounded caches

Generated thumbnails SHALL be cached in memory and on disk, both caches SHALL be bounded, and unchanged files SHALL be served without re-decoding the source.

#### Scenario: Repeat view

- **WHEN** the same unchanged file is viewed again
- **THEN** its thumbnail is served from memory or disk without decoding the source again

#### Scenario: Cache pressure

- **WHEN** the memory cache reaches its bound
- **THEN** least-recently-used entries are evicted without affecting correctness

### Requirement: Versioned cacheable URLs

Thumbnail URLs SHALL include a version derived from the source file's identity and modification state, so changed files bypass stale caches and unchanged thumbnails can be cached by the WebView.

#### Scenario: File changes

- **WHEN** a file's content or modification time changes
- **THEN** a new thumbnail version is requested and the old URL is not reused for the new content

#### Scenario: Repeat request

- **WHEN** the same unchanged thumbnail is requested again
- **THEN** the response is cacheable and does not trigger regeneration

### Requirement: Right-sized decoding

The system SHALL decode thumbnails at a size appropriate to their display context and SHALL NOT serve full-resolution originals as thumbnails.

#### Scenario: Large original

- **WHEN** a large image is requested as a thumbnail
- **THEN** a downscaled thumbnail is generated instead of serving the original

#### Scenario: Small original

- **WHEN** an image is already within the target bounds
- **THEN** it may be served directly without an unnecessary re-encode

### Requirement: Graceful placeholders and failures

When a thumbnail is queued, unavailable, or fails, the UI SHALL show a stable placeholder, keep the card interactive, and avoid unbounded retries.

#### Scenario: Unsupported format

- **WHEN** a thumbnail cannot be generated
- **THEN** the card shows a placeholder and remains usable

#### Scenario: Source disappears

- **WHEN** a requested file no longer exists
- **THEN** the request fails cleanly and the UI does not retry indefinitely

### Requirement: Cancellation and teardown

When a card or tile leaves view, pending thumbnail work for it SHALL be cancelled or deprioritized and its live image memory SHALL be released.

#### Scenario: Card swiped away

- **WHEN** a card leaves the deck before its thumbnail finishes
- **THEN** its pending request is dropped or deprioritized and its image memory is released

#### Scenario: Review closed

- **WHEN** the user closes Review
- **THEN** offscreen thumbnail requests stop

### Requirement: Cross-platform cache semantics

The Android and desktop backends SHALL honor the same thumbnail versioning and cache semantics for the shared UI.

#### Scenario: Versioned request on both backends

- **WHEN** the UI requests a thumbnail with the same version parameter from either backend
- **THEN** both treat the version as the cache identity and invalidate on source change
