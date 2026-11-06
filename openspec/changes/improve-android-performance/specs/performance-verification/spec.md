# Spec Delta

## Purpose

Make Android performance measurable and enforceable so slow loads, janky gestures, and thumbnail stampedes are caught before release.

## ADDED Requirements

### Requirement: Debug timing instrumentation

Debug builds SHALL record timings for first page, first card, full index, swipe-to-next-card, Review open, and thumbnail queue and decode counts without changing release behavior.

#### Scenario: Large-folder session

- **WHEN** a debug build opens and swipes through a large folder
- **THEN** the timings and counters are available in the debug log or a debug-only endpoint

#### Scenario: Review open

- **WHEN** Review opens with many marked files
- **THEN** the debug output reports the time to first rendered tile and the thumbnail queue depth

### Requirement: Reference budgets enforced in CI

The Android CI harness SHALL run against a generated folder of a known size on the reference emulator and SHALL fail when first-card time, full-index time, swipe-to-next latency, Review-open time, or concurrent thumbnail decodes exceed the recorded budgets.

#### Scenario: Budgets pass

- **WHEN** the app meets every budget on the reference emulator
- **THEN** the performance check passes and reports the measured values

#### Scenario: Budget regresses

- **WHEN** any measured value exceeds its budget
- **THEN** the CI check fails and names the metric and measured value

### Requirement: Failure diagnostics

When a performance check fails, the harness SHALL save the measured timings and relevant logs and screenshots as build artifacts.

#### Scenario: Failed swipe budget

- **WHEN** swipe-to-next latency exceeds its budget
- **THEN** the artifacts include the timing report and a screenshot or probe output from the failing state

### Requirement: Release neutrality

Performance instrumentation SHALL be disabled or a no-op in release builds and SHALL NOT add network access, telemetry, or user-visible diagnostics.

#### Scenario: Release build

- **WHEN** the app is built for release
- **THEN** instrumentation adds no network calls and no telemetry, and the UI exposes no debug timing output

### Requirement: Desktop contract coverage

The desktop backend SHALL be covered by index and paging tests that match the Android contract so the shared UI does not drift between platforms.

#### Scenario: Paging test suite

- **WHEN** the desktop test suite runs
- **THEN** it verifies page bounds, counts, scan state, ordering, and decision continuity against the documented contract
