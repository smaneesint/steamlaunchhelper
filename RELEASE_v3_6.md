# SteamLaunchHelper v3.6

Fixes Steam installation and account detection while retaining the v3.5 launcher runtime and game/artwork behavior.

- Detect Steam using Windows Registry paths, with default installation paths as fallbacks.
- Offer installation folder selection when automatic detection fails.
- Use the most recently used valid account, or ask when multiple accounts are ambiguous.
- Keep shortcut and artwork operations on the selected account for the session.
- Explain missing userdata or account configuration; resolve the destination before copying launcher files.

## Run the packaged app

Extract the entire release ZIP into a folder, then run SteamLaunchHelper_v3_6.exe. Keep the included launcher folder and all of its _internal files beside the application. Python is not required for the packaged app. Fully exit Steam before adding games or applying artwork.

## Run from source or build on Windows

Use Python 3.10 or later. Install dependencies with `python -m pip install -r requirements-build.txt`. Run `python SteamLaunchHelper_v3_6.py` or build with `build_v3_6.bat`.

The build requires the complete existing working launcher folder, including _internal, next to the source. GitHub currently tracks launcher.exe but excludes launcher/_internal; a fresh clone alone is insufficient to create a working release. Restore the support files from the working v3.5 distribution. Do not rebuild using the experimental launcher.spec or v1.2 runtime sources for this release.

The build script creates dist/SteamLaunchHelper_v3_6.zip with the application and unchanged launcher template. Upload the ZIP as a GitHub Release asset; generated executables and build folders are not part of this source change.

## Validation

Run `python test_steam_detection.py` from the repository root. Tests use temporary files and simulated Steam metadata and do not modify a real Steam installation. Live Steam interaction, game launching, and artwork downloads require a manual check before publishing a release.
