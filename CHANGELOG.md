# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- Search by image: drop a picture into the window, paste one with Ctrl+V, or pick a file with the image button in the search box. The same photo comes first, and matches of 90% or more show their similarity without hovering.
- Duplicates view (the Duplicates button, or Settings → Review duplicates): identical files, near-duplicates (resized or compressed copies, edited versions, bursts with no visible difference) and burst shots, group by group, with a suggested photo to keep, each file's resolution, size and folder, and how much space the others take. It is read-only: select what you don't need and delete it yourself with “Show in folder”.
- “Show in folder” for several selected photos at once (one File Explorer window per folder, with the photos selected).
- A find-similar button on each thumbnail, shown on hover.

### Changed

- The index stores a small difference hash (dHash) of each thumbnail to recognize near-duplicates. Indexes made by 0.1.0 compute it on the first launch, from the thumbnails only (about a second per thousand photos).
- The toolbar wraps better in narrow windows, and the search box, image button and Search button share one outline.

## [0.1.0] - 2026-10-08

The first public release.

### Added

- Search local photos with a sentence using EmbeddingGemma 2; everything runs offline after the one-time model download.
- Dates in the query (“beach May 2025”, “去年5月 海边”) become a date filter; the date menu also filters by year.
- Month-by-month browsing, burst and duplicate grouping, find similar photos, slideshow and full screen preview.
- Multi-select to copy paths or export copies to a folder.
- Several photo folders, including NAS drives that may be offline; HEIC support; new photos are indexed at startup.
- Interface in English and Simplified Chinese, following the system language.
- Download-and-run Windows package: the first launch installs Python and PyTorch with the bundled uv and shows progress; an interrupted setup resumes on the next launch.
- Progress while the model downloads on first launch, and photo folders can already be added during the download.
- `Start with China mirrors.cmd` for a faster first launch in mainland China.
- Upgrade in place: extract a new version over the old folder; if its Python dependencies changed, the first launch updates them.
- Privacy and security: the background service only accepts requests addressed to this computer, the app window has a strict Content Security Policy, and no usage statistics are sent to Hugging Face.
