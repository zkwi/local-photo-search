# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/).

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
