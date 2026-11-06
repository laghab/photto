# Spec Delta

## Purpose

Makes the review application the primary SIFT product and gives users a simple way to open it from the desktop, mobile launcher, or terminal.

## ADDED Requirements

### Requirement: SIFT is the visible product name
The installed application and its user-facing interface SHALL identify as SIFT across supported platforms.

#### Scenario: Open from an app menu
- **WHEN** the user launches the installed application from a desktop or Android app menu
- **THEN** its visible name and main window identify the product as SIFT

### Requirement: Minimal GUI launcher command
The `sift` command SHALL launch the GUI for an optional directory and SHALL NOT perform OCR scanning or file operations in the terminal.

#### Scenario: Launch with a directory
- **WHEN** the user runs `sift DIR`
- **THEN** the SIFT GUI opens scoped to that directory

#### Scenario: Launch without a directory
- **WHEN** the user runs `sift` without a path
- **THEN** the SIFT GUI opens its folder picker

#### Scenario: Legacy scan flags are supplied
- **WHEN** the user supplies an old OCR scanner flag such as `--video`, `--copy`, `--dry-run`, `--search`, or `--json`
- **THEN** the launcher does not scan or alter files from the terminal and explains that OCR actions are available in the GUI

### Requirement: Existing review state survives rebranding
The rebranded application SHALL preserve existing Photto review marks, preferences, and folder access across a supported upgrade or one-time data migration.

#### Scenario: Upgrade an existing Android installation
- **WHEN** an existing Photto Android user upgrades to the SIFT-branded build
- **THEN** the app retains the same installation identity and can access the user's existing app state

#### Scenario: First desktop launch after rebranding
- **WHEN** a desktop user launches SIFT with existing Photto state and no SIFT state yet
- **THEN** SIFT makes the existing marks and preferences available without overwriting an existing SIFT state store
