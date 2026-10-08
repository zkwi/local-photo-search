# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.4.0] - 2026-10-08

### Added

- **Photos from this day**: in the preview (next to the date, or press `D`) and in the right-click menu. It shows everything taken that day; Back returns to where you were.
- Month headings on the home page are clickable: click one to see only that month, and searches then stay within it.
- “Select all extra copies” in the Identical category of the Duplicates view (removing an identical copy loses nothing; near-duplicates and bursts still need a look group by group).
- Settings: **Open** next to each photo folder and the index folder (where the log file is), plus **Check for updates** and **Report a problem**, which open the project's GitHub pages in your browser. The app itself still never goes online after setup.

### Changed

- Dates in the preview no longer break in the middle of the time, and the end of the list counts photos instead of tiles (bursts are folded into one tile).
- The README screenshots show the current interface, plus one of the Duplicates view.
- A UI test now checks that every interface string the app uses exists (a key missing in every language slipped past the per-language comparison).

## [0.3.0] - 2026-10-08

### Added

- Right-click menu on photos and in the preview: open, find similar, select, copy image, copy path, show in folder, open with the default app. The browser's own menu (Back, Refresh, Save as…) no longer appears; text boxes keep their cut/copy/paste menu.
- Drag a photo folder into the window to add it to your library.
- `Ctrl+C` copies the photo you are on (with several selected: their paths), and `Ctrl`+scroll or a touchpad pinch zooms in the preview.

### Changed

- Search by image and Find similar label results that look like the same photo as “Same photo”, using each thumbnail's dHash as well as the similarity: photos of the same scene from other days score above 90% too, so the similarity alone could not tell them apart.
- Screenshots are no longer suggested as near-duplicates. Screenshots of the same app screen taken days apart (with different balances or messages) look almost the same, so only identical screenshot files are flagged; the rest are listed under bursts and similar, without a suggestion.
- The first indexing shows photos as they are processed — at least every 20 seconds instead of every 1,000 photos — and the home page shows the progress instead of “No photos yet”. Indexing writes and reports every 8 photos, so on a PC without a GPU the progress moves and searches get the model within seconds instead of minutes.
- After closing the preview, the list stays on the photo you viewed last.
- Notifications appear above the selection bar instead of covering it, and long file names in the Duplicates view keep their distinguishing end visible.
- Dragging a picture straight from a web page explains to copy and paste it instead of doing nothing.

## [0.2.0] - 2026-10-08

### Added

- Search by image: drop a picture into the window, paste one with Ctrl+V, or pick a file with the image button in the search box. The same photo comes first, and matches of 90% or more show their similarity without hovering.
- Duplicates view (the Duplicates button, or Settings → Review duplicates): identical files, near-duplicates (resized or compressed copies, edited versions, bursts with no visible difference) and burst shots, group by group, with a suggested photo to keep, each file's resolution, size and folder, and how much space the others take. It is read-only: select what you don't need and delete it yourself with “Show in folder”. When you come back to the app, photos you deleted are marked as deleted and the folders are rescanned.
- “Show in folder” for several selected photos at once (one File Explorer window per folder, with the photos selected).
- A find-similar button on each thumbnail, shown on hover, and the `S` key in the preview.

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
