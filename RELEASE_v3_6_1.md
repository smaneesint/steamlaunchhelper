# SteamLaunchHelper v3.6.1

Extract the entire Windows release ZIP and run `SteamLaunchHelper_v3_6_1.exe`. Keep `launcher`, including its `_internal` folder, beside the application. Fully exit Steam before adding games or applying artwork.

## Build on Windows

Install 64-bit Python 3.14 from python.org and run `python -m pip install -r requirements-build.txt`. Build using either the complete working v3.6 launcher folder or the v3.6 Windows release ZIP.

```bat
build_v3_6_1.bat
build_v3_6_1.bat --launcher-zip "C:\path\to\SteamLaunchHelper_v3_6.zip"
```

Set `SLH_PYTHON` to the full path to your Python executable if needed. The build validates the frozen executable?s TLS initialization and bundled CA file, the launcher template, and the final ZIP before writing `dist\SteamLaunchHelper_v3_6_1.zip`.

## Diagnostics

SteamGridDB failures are logged under `%LOCALAPPDATA%\SteamLaunchHelper\logs\steamgriddb.log`.

Use `SteamLaunchHelper_v3_6_1.exe --check-steamgriddb` to run a live search and artwork download check without opening the GUI or writing to Steam. The executable returns exit code 0 on success and 1 on failure. In PowerShell, use `(Start-Process .\SteamLaunchHelper_v3_6_1.exe -ArgumentList '--check-steamgriddb' -Wait -PassThru).ExitCode` to read the result.
