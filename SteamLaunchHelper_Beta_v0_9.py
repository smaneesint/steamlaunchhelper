import os
import sys
import json
import glob
import zlib
import shutil
import subprocess
import requests
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import vdf


STEAMGRIDDB_API_KEY = "ec6a77308546cd7f86bfc36d711df2d6".strip()
STEAMGRIDDB_BASE_URL = "https://www.steamgriddb.com/api/v2"


def resource_path(relative_path: str) -> str:
    base_path = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base_path, relative_path)


def get_app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def is_path_within(child_path: str, parent_path: str) -> bool:
    child = normalize_path(child_path)
    parent = normalize_path(parent_path)
    return child == parent or child.startswith(parent + os.sep)


def sanitize_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'
    result = "".join("_" if c in invalid else c for c in name).strip()
    return result or "game"


def find_manifest_upwards(start_path: str, max_levels: int = 6) -> str | None:
    current = os.path.abspath(start_path)
    if os.path.isfile(current):
        current = os.path.dirname(current)

    for _ in range(max_levels + 1):
        manifest = os.path.join(current, "AppxManifest.xml")
        if os.path.isfile(manifest):
            return manifest
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    return None


def get_apps_from_manifest(manifest_path: str) -> list[dict]:
    tree = ET.parse(manifest_path)
    root = tree.getroot()

    ns = {"a": "http://schemas.microsoft.com/appx/manifest/foundation/windows10"}
    identity = root.find("a:Identity", ns)
    apps_node = root.find("a:Applications", ns)

    if identity is None or apps_node is None:
        raise RuntimeError("Could not find Identity or Applications in AppxManifest.xml")

    package_name = identity.attrib.get("Name")
    if not package_name:
        raise RuntimeError("Package Identity Name not found in AppxManifest.xml")

    apps = []
    for app in apps_node.findall("a:Application", ns):
        app_id = app.attrib.get("Id")
        executable = app.attrib.get("Executable", "")
        entrypoint = app.attrib.get("EntryPoint", "")
        visual = app.find(
            "{http://schemas.microsoft.com/appx/manifest/uap/windows10}VisualElements"
        )
        display_name = visual.attrib.get("DisplayName", "") if visual is not None else ""

        if app_id:
            apps.append(
                {
                    "package_name": package_name,
                    "app_id": app_id,
                    "executable": executable,
                    "entrypoint": entrypoint,
                    "display_name": display_name,
                }
            )

    if not apps:
        raise RuntimeError("No Application entries found in AppxManifest.xml")

    return apps


def choose_best_app(apps: list[dict], selected_game_exe: str) -> dict:
    exe_name = os.path.basename(selected_game_exe).lower()

    for app in apps:
        manifest_exe = os.path.basename(app.get("executable", "")).lower()
        if manifest_exe and manifest_exe == exe_name:
            return app

    exe_stem = os.path.splitext(exe_name)[0]
    for app in apps:
        manifest_exe = os.path.basename(app.get("executable", "")).lower()
        if manifest_exe and exe_stem in manifest_exe:
            return app

    for app in apps:
        if "shipping" in app["app_id"].lower():
            return app

    return apps[0]


def build_shell_app_id(selected_game_exe: str) -> tuple[str, dict, str]:
    manifest_path = find_manifest_upwards(selected_game_exe)
    if not manifest_path:
        raise RuntimeError("Could not find AppxManifest.xml near the selected game executable.")

    tree = ET.parse(manifest_path)
    root = tree.getroot()

    ns = {"a": "http://schemas.microsoft.com/appx/manifest/foundation/windows10"}
    identity = root.find("a:Identity", ns)
    if identity is None:
        raise RuntimeError("Could not find Identity in AppxManifest.xml")

    package_name = identity.attrib.get("Name")
    if not package_name:
        raise RuntimeError("Package Name missing in AppxManifest.xml")

    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-AppxPackage | "
            f"Where-Object {{$_.Name -eq '{package_name}'}} | "
            "Select-Object -First 1 -ExpandProperty PackageFamilyName"
        ),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    package_family_name = result.stdout.strip()

    if not package_family_name:
        raise RuntimeError(f"Could not resolve PackageFamilyName for package '{package_name}'.")

    apps = get_apps_from_manifest(manifest_path)
    chosen = choose_best_app(apps, selected_game_exe)
    shell_app_id = f"shell:AppsFolder\\{package_family_name}!{chosen['app_id']}"
    return shell_app_id, chosen, manifest_path


def get_epic_manifest_candidates() -> list[str]:
    candidates = []
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")

    manifest_dir_1 = os.path.join(program_data, "Epic", "EpicGamesLauncher", "Data", "Manifests")
    manifest_dir_2 = os.path.join(program_data, "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat")

    if os.path.isdir(manifest_dir_1):
        candidates.extend(glob.glob(os.path.join(manifest_dir_1, "*.item")))
        candidates.extend(glob.glob(os.path.join(manifest_dir_1, "*.json")))

    if os.path.isfile(manifest_dir_2):
        candidates.append(manifest_dir_2)

    return candidates


def extract_epic_app_name_from_item(data: dict) -> str | None:
    for key in ("AppName", "MainGameAppName", "CatalogItemId", "NamespaceId", "ArtifactId"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def find_epic_app_name(game_exe_path: str) -> tuple[str | None, str | None]:
    game_exe_path = normalize_path(game_exe_path)
    game_dir = os.path.dirname(game_exe_path)

    for manifest_file in get_epic_manifest_candidates():
        try:
            with open(manifest_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            continue

        if manifest_file.lower().endswith("launcherininstalled.dat"):
            installs = data.get("InstallationList", [])
            for item in installs:
                install_location = item.get("InstallLocation")
                app_name = item.get("AppName") or item.get("ArtifactId")

                if not install_location or not app_name:
                    continue

                if is_path_within(game_exe_path, install_location) or is_path_within(game_dir, install_location):
                    return app_name, manifest_file
        else:
            install_location = (
                data.get("InstallLocation")
                or data.get("ManifestLocation")
                or data.get("StagingLocation")
            )
            app_name = extract_epic_app_name_from_item(data)

            if not install_location or not app_name:
                continue

            if is_path_within(game_exe_path, install_location) or is_path_within(game_dir, install_location):
                return app_name, manifest_file

    return None, None


def make_runtime_helper_content() -> str:
    """Generate the generic runtime helper.

    v0.9 uses one reusable helper EXE plus a per-game JSON config file.
    The helper reads steamlaunchhelper_config.json from its own folder.
    """
    runtime_code = r"""
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime

CONFIG_FILENAME = "steamlaunchhelper_config.json"


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    return os.path.join(app_dir(), CONFIG_FILENAME)


def load_config() -> dict:
    path = config_path()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()


def configured_start_dir() -> str:
    start_dir = CONFIG.get("start_dir") or app_dir()
    return start_dir if os.path.isdir(start_dir) else app_dir()


LOG_PATH = os.path.join(configured_start_dir(), "steamlaunchhelper.log")


def log(message: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_matching_process_ids(exe_name: str) -> list[int]:
    exe_name = (exe_name or "").lower().strip()
    if not exe_name:
        return []

    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log(f"Process check failed for {exe_name}: {exc}")
        return []

    pids = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.upper().startswith("INFO:"):
            continue

        try:
            row = next(csv.reader([line]))
        except Exception:
            continue

        if len(row) < 2:
            continue

        image_name = row[0].strip().strip('"').lower()
        pid_text = row[1].strip().strip('"')

        if image_name != exe_name:
            continue

        try:
            pids.append(int(pid_text))
        except ValueError:
            continue

    return pids


def has_visible_window_for_pids(pids: list[int]) -> bool:
    if not pids:
        return False

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        target_pids = set(int(pid) for pid in pids)
        found = {"value": False}

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            if int(pid.value) in target_pids:
                found["value"] = True
                return False

            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return bool(found["value"])
    except Exception as exc:
        log(f"Window check failed; falling back to process-only tracking: {exc}")
        return False


def launch_game() -> None:
    mode = CONFIG["mode"]
    target = CONFIG["launch_target"]
    start_dir = configured_start_dir()

    log(f"Launching mode={mode} target={target} start_dir={start_dir}")

    if mode in ("gamepass", "epic_uri"):
        os.startfile(target)
        return

    if mode == "direct_exe":
        subprocess.Popen(
            [target],
            cwd=start_dir,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return

    raise RuntimeError(f"Unknown launch mode: {mode}")


def wait_until_game_appears(game_exe: str, timeout_seconds: int) -> bool:
    log(f"Waiting for process: {game_exe}")
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if get_matching_process_ids(game_exe):
            log(f"Detected process: {game_exe}")
            return True
        time.sleep(1)

    log(f"Timed out waiting for process: {game_exe}")
    return False


def monitor_until_game_exits(
    game_exe: str,
    poll_seconds: int,
    process_missing_grace_seconds: int,
    window_missing_grace_seconds: int,
) -> None:
    last_process_seen = time.time()
    last_window_seen = time.time()
    ever_saw_window = False

    log(f"Keeping Steam session alive while {game_exe} is running")

    while True:
        pids = get_matching_process_ids(game_exe)

        if pids:
            last_process_seen = time.time()

            if has_visible_window_for_pids(pids):
                if not ever_saw_window:
                    log(f"Detected visible game window for {game_exe}")
                ever_saw_window = True
                last_window_seen = time.time()
        else:
            missing_for = time.time() - last_process_seen
            if missing_for >= process_missing_grace_seconds:
                log(f"Game process missing for {missing_for:.1f}s; exiting helper")
                break

        if ever_saw_window:
            window_missing_for = time.time() - last_window_seen
            if window_missing_for >= window_missing_grace_seconds:
                log(f"Game window missing for {window_missing_for:.1f}s; exiting helper")
                break

        time.sleep(poll_seconds)


def main() -> int:
    game_exe = CONFIG["game_exe_name"]
    detect_timeout = int(CONFIG.get("process_detect_timeout_seconds", 600))
    poll_seconds = int(CONFIG.get("poll_seconds", 1))
    startup_grace = int(CONFIG.get("startup_grace_seconds", 3))
    process_missing_grace = int(CONFIG.get("process_missing_grace_seconds", 4))
    window_missing_grace = int(CONFIG.get("window_missing_grace_seconds", 8))

    log("Helper started")
    log(f"Loaded config from: {config_path()}")

    try:
        launch_game()
    except Exception as exc:
        log(f"Launch failed: {exc}")
        time.sleep(60)
        return 1

    if not wait_until_game_appears(game_exe, detect_timeout):
        time.sleep(30)
        return 2

    time.sleep(startup_grace)
    monitor_until_game_exits(game_exe, poll_seconds, process_missing_grace, window_missing_grace)

    log(f"Helper exited normally for: {game_exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""
    return runtime_code


def make_game_config(mode: str, launch_target: str, game_exe_name: str, start_dir: str) -> dict:
    return {
        "mode": mode,
        "launch_target": launch_target,
        "game_exe_name": game_exe_name,
        "start_dir": start_dir,
        "startup_grace_seconds": 3,
        "process_detect_timeout_seconds": 600,
        "process_missing_grace_seconds": 4,
        "window_missing_grace_seconds": 8,
        "poll_seconds": 1,
    }

def cleanup_helper_build_artifacts(helper_py_path: str) -> None:
    """Remove temporary files created only for building the Steam helper."""
    helper_dir = os.path.dirname(helper_py_path)
    for path in (
        helper_py_path,
        os.path.join(helper_dir, "steamlaunchhelper.spec"),
    ):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass

    build_dir = os.path.join(helper_dir, "build_steamlaunchhelper")
    try:
        if os.path.isdir(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)
    except Exception:
        pass


def get_pyinstaller_command(helper_py_path: str, helper_dir: str) -> list[str]:
    """Return a PyInstaller command that works both from source and from a frozen app.

    When this app is packaged with PyInstaller, sys.executable points to
    SteamLaunchHelper itself. Using it with `-m PyInstaller` relaunches this app,
    which creates the extra-window bug. In frozen mode, use the system Python
    launcher/interpreter instead.
    """
    pyinstaller_args = [
        "-m",
        "PyInstaller",
        "--noconsole",
        "--onedir",
        "--clean",
        "--noconfirm",
        "--name",
        "steamlaunchhelper",
        "--distpath",
        helper_dir,
        "--workpath",
        os.path.join(helper_dir, "build_steamlaunchhelper"),
        "--specpath",
        helper_dir,
        helper_py_path,
    ]

    if not getattr(sys, "frozen", False):
        return [sys.executable] + pyinstaller_args

    candidates = [
        ["py", "-3"],
        ["python"],
        ["python3"],
    ]

    for candidate in candidates:
        try:
            result = subprocess.run(
                candidate + ["--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return candidate + pyinstaller_args
        except Exception:
            pass

    raise RuntimeError(
        "Could not find a usable Python interpreter to build the Steam helper.\n\n"
        "For this beta build, install Python and PyInstaller, then try again:\n\n"
        "pip install pyinstaller\n\n"
        "Production v1.0 should avoid this requirement by shipping a prebuilt helper template."
    )


def build_runtime_helper_exe(helper_py_path: str) -> str:
    """Build the generated runtime helper into an EXE using PyInstaller."""
    helper_dir = os.path.dirname(helper_py_path)
    helper_exe_path = os.path.join(helper_dir, "steamlaunchhelper", "steamlaunchhelper.exe")

    cmd = get_pyinstaller_command(helper_py_path, helper_dir)

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=helper_dir)
    if result.returncode != 0:
        quoted_cmd = " ".join(f'"{part}"' if " " in part else part for part in cmd)
        raise RuntimeError(
            "PyInstaller failed. Install it with:\n\n"
            "pip install pyinstaller\n\n"
            "Beta v0.9 builds the reusable Steam-tracked helper with PyInstaller --onedir because Steam can lose tracking with --onefile. "
            "If it still fails, open CMD in the selected launcher folder and run this command manually:\n\n"
            + quoted_cmd
            + "\n\nSTDOUT:\n"
            + result.stdout[-2000:]
            + "\n\nSTDERR:\n"
            + result.stderr[-2000:]
        )

    if not os.path.isfile(helper_exe_path):
        raise RuntimeError("PyInstaller completed, but the onedir steamlaunchhelper.exe was not found.")

    cleanup_helper_build_artifacts(helper_py_path)
    return helper_exe_path



def get_template_helper_dir() -> str:
    folder = os.path.join(get_app_base_dir(), "steamlaunchhelper_template")
    os.makedirs(folder, exist_ok=True)
    return folder


def ensure_template_helper_exe() -> str:
    """Build the reusable helper once and return its EXE path."""
    template_dir = get_template_helper_dir()
    helper_exe_path = os.path.join(template_dir, "steamlaunchhelper", "steamlaunchhelper.exe")

    if os.path.isfile(helper_exe_path):
        return helper_exe_path

    helper_py_path = os.path.join(template_dir, "steamlaunchhelper_runtime.py")
    with open(helper_py_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(make_runtime_helper_content())

    return build_runtime_helper_exe(helper_py_path)


def copy_helper_template_to_game_folder(destination_parent: str) -> str:
    """Copy the reusable onedir helper into the selected game's folder."""
    template_exe = ensure_template_helper_exe()
    template_folder = os.path.dirname(template_exe)
    destination_folder = os.path.join(destination_parent, "steamlaunchhelper")

    os.makedirs(destination_folder, exist_ok=True)
    shutil.copytree(template_folder, destination_folder, dirs_exist_ok=True)

    destination_exe = os.path.join(destination_folder, "steamlaunchhelper.exe")
    if not os.path.isfile(destination_exe):
        raise RuntimeError("Reusable helper copy completed, but steamlaunchhelper.exe was not found.")

    return destination_exe


def write_game_config(helper_exe_path: str, config: dict) -> str:
    config_path = os.path.join(os.path.dirname(helper_exe_path), "steamlaunchhelper_config.json")
    with open(config_path, "w", encoding="utf-8", newline="\r\n") as f:
        json.dump(config, f, indent=4)
    return config_path

def is_steam_running() -> bool:
    try:
        output = subprocess.check_output("tasklist", shell=True, text=True).lower()
        return "steam.exe" in output
    except Exception:
        return False


def get_steam_shortcuts_path() -> str:
    possible_userdata_paths = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Steam\userdata"),
        os.path.expandvars(r"%ProgramFiles%\Steam\userdata"),
    ]

    for userdata_root in possible_userdata_paths:
        if not os.path.isdir(userdata_root):
            continue

        user_ids = [d for d in os.listdir(userdata_root) if d.isdigit()]
        for user_id in user_ids:
            config_dir = os.path.join(userdata_root, user_id, "config")
            shortcuts_path = os.path.join(config_dir, "shortcuts.vdf")
            if os.path.isdir(config_dir):
                return shortcuts_path

    raise RuntimeError("Could not find Steam userdata config folder.")


def get_steam_grid_dir() -> str:
    shortcuts_path = get_steam_shortcuts_path()
    config_dir = os.path.dirname(shortcuts_path)
    grid_dir = os.path.join(config_dir, "grid")
    os.makedirs(grid_dir, exist_ok=True)
    return grid_dir


def get_game_name_from_path(path: str) -> str:
    normalized = os.path.normpath(path)
    parts = normalized.split(os.sep)

    try:
        xbox_index = next(i for i, p in enumerate(parts) if p.lower() == "xboxgames")
        if xbox_index + 1 < len(parts):
            return parts[xbox_index + 1]
    except StopIteration:
        pass

    basename = os.path.basename(normalized)
    common_subdirs = {"content", "win64", "binaries", "shipping"}
    if basename.lower() in common_subdirs:
        parent = os.path.basename(os.path.dirname(normalized))
        if parent:
            return parent

    return basename or "Non-Steam Game"


def compute_shortcut_appid(exe_path: str, app_name: str) -> int:
    unique_name = (exe_path + app_name).encode("utf-8")
    appid = zlib.crc32(unique_name) | 0x80000000

    # Convert to signed 32-bit int (fix overflow)
    if appid > 0x7FFFFFFF:
        appid -= 0x100000000

    return appid


def add_helper_to_steam_library(helper_exe_path: str, start_dir: str, app_name: str) -> tuple[str, int]:
    shortcuts_path = get_steam_shortcuts_path()
    expected_appid = compute_shortcut_appid(helper_exe_path, app_name)

    data = {"shortcuts": {}}

    if os.path.exists(shortcuts_path):
        try:
            if os.path.getsize(shortcuts_path) > 0:
                with open(shortcuts_path, "rb") as f:
                    data = vdf.binary_load(f)
        except Exception:
            data = {"shortcuts": {}}

    shortcuts = data.setdefault("shortcuts", {})

    for entry in shortcuts.values():
        existing_exe = str(entry.get("Exe", "")).strip('"').lower()
        if existing_exe == helper_exe_path.lower():
            existing_appid = int(entry.get("appid", 0)) or expected_appid
            entry["appid"] = existing_appid
            entry["Exe"] = helper_exe_path
            entry["StartDir"] = start_dir
            entry["LaunchOptions"] = ""
            with open(shortcuts_path, "wb") as f:
                vdf.binary_dump(data, f)
            return "This game is already in Steam Library. Updated shortcut paths.", existing_appid

    next_index = str(max([int(k) for k in shortcuts.keys()], default=-1) + 1)

    shortcuts[next_index] = {
        "appid": expected_appid,
        "AppName": app_name,
        "Exe": helper_exe_path,
        "StartDir": start_dir,
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": "",
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "tags": {},
    }

    with open(shortcuts_path, "wb") as f:
        vdf.binary_dump(data, f)

    return f'Added "{app_name}" to Steam Library.', expected_appid


def set_shortcut_icon(helper_exe_path: str, icon_path: str) -> None:
    shortcuts_path = get_steam_shortcuts_path()

    with open(shortcuts_path, "rb") as f:
        data = vdf.binary_load(f)

    shortcuts = data.setdefault("shortcuts", {})
    updated = False

    for entry in shortcuts.values():
        existing_exe = str(entry.get("Exe", "")).strip('"').lower()
        if existing_exe == helper_exe_path.lower():
            entry["icon"] = icon_path
            updated = True
            break

    if not updated:
        raise RuntimeError("Could not find matching Steam shortcut to update icon.")

    with open(shortcuts_path, "wb") as f:
        vdf.binary_dump(data, f)


def resolve_launcher_path(launcher_folder: str, game_exe: str) -> str | None:
    helper_exe_path = os.path.join(launcher_folder, "steamlaunchhelper.exe")

    if os.path.isfile(helper_exe_path):
        return helper_exe_path

    use_game_exe = messagebox.askyesno(
        "steamlaunchhelper.exe not found",
        "steamlaunchhelper.exe was not found in the selected folder.\n\n"
        f"Use this executable as the launcher instead?\n{game_exe}"
    )

    if use_game_exe:
        return game_exe

    return None


class SteamGridDBClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    def _headers(self) -> dict:
        key = self.api_key.strip()
        if not key:
            raise RuntimeError("SteamGridDB API key is missing.")
        return {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "SteamLaunchHelper/Beta-v0.9"
        }

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        if not self.api_key:
            raise RuntimeError("SteamGridDB API key is missing.")

        url = f"{STEAMGRIDDB_BASE_URL}{endpoint}"
        response = requests.get(url, headers=self._headers(), params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()

        if not payload.get("success"):
            raise RuntimeError(f"SteamGridDB request failed: {endpoint}")

        return payload

    def search_games(self, term: str) -> list[dict]:
        payload = self._get(f"/search/autocomplete/{term}")
        return payload.get("data", [])

    def get_grids(self, game_id: int) -> list[dict]:
        payload = self._get(f"/grids/game/{game_id}")
        return payload.get("data", [])

    def get_heroes(self, game_id: int) -> list[dict]:
        payload = self._get(f"/heroes/game/{game_id}")
        return payload.get("data", [])

    def get_logos(self, game_id: int) -> list[dict]:
        payload = self._get(f"/logos/game/{game_id}")
        return payload.get("data", [])

    def get_icons(self, game_id: int) -> list[dict]:
        payload = self._get(f"/icons/game/{game_id}")
        return payload.get("data", [])


def format_asset_option(asset: dict, index: int) -> str:
    width = asset.get("width", "?")
    height = asset.get("height", "?")
    score = asset.get("score", "?")
    style = asset.get("style", "")
    mime = asset.get("mime", "")
    return f"{index + 1}. {width}x{height} | score={score} | {style} | {mime}"


def get_file_extension_from_url(url: str) -> str:
    path = url.split("?")[0]
    ext = os.path.splitext(path)[1].lower()
    return ext if ext else ".png"


def download_file(url: str, target_path: str) -> None:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    with open(target_path, "wb") as f:
        f.write(response.content)


def copy_if_exists(src: str | None, dst: str) -> bool:
    if src and os.path.isfile(src):
        shutil.copy2(src, dst)
        return True
    return False

def to_unsigned_appid(appid: int) -> int:
        return appid & 0xFFFFFFFF

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SteamLaunchHelper Beta v0.9")
        self.root.geometry("980x780")
        self.root.minsize(980, 780)

        self.sgdb = SteamGridDBClient(STEAMGRIDDB_API_KEY)

        self.launcher_folder_var = tk.StringVar()
        self.game_exe_var = tk.StringVar()
        self.detected_appid_var = tk.StringVar()
        self.detected_manifest_var = tk.StringVar()
        self.detected_app_entry_var = tk.StringVar()
        self.search_term_var = tk.StringVar()

        self.last_helper_path = None
        self.last_start_dir = None
        self.last_game_name = None
        self.last_shortcut_appid = None

        self.sgdb_results = []
        self.selected_sgdb_game = None
        self.asset_lists = {
            "cover": [],
            "wide_cover": [],
            "background": [],
            "logo": [],
            "icon": [],
            "client_icon": [],
        }
        self.downloaded_assets = {}

        self.build_ui()
        self.update_create_button_state()

    def build_ui(self):
        pad = {"padx": 10, "pady": 6}

        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        v_scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=v_scrollbar.set)

        v_scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        main = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=main, anchor="nw")

        def _update_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _resize_inner_frame(event):
            canvas.itemconfigure(canvas_window, width=event.width)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        main.bind("<Configure>", _update_scroll_region)
        canvas.bind("<Configure>", _resize_inner_frame)
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        main.configure(padding=(12, 12, 12, 12))

        ttk.Label(main, text="Launcher folder").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(main, textvariable=self.launcher_folder_var, width=90).grid(
            row=1, column=0, sticky="ew", **pad
        )
        ttk.Button(main, text="Browse Folder", command=self.pick_launcher_folder).grid(
            row=1, column=1, sticky="ew", **pad
        )

        ttk.Label(main, text="Actual game executable").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(main, textvariable=self.game_exe_var, width=90).grid(
            row=3, column=0, sticky="ew", **pad
        )
        ttk.Button(main, text="Browse EXE", command=self.pick_game_exe).grid(
            row=3, column=1, sticky="ew", **pad
        )

        ttk.Separator(main).grid(row=4, column=0, columnspan=2, sticky="ew", pady=8)

        self.create_button = ttk.Button(
            main,
            text="Create steamlaunchhelper.exe",
            command=self.create_helper,
        )
        self.create_button.grid(row=5, column=0, sticky="ew", **pad)

        self.add_steam_button = ttk.Button(
            main,
            text="Add to Steam Library",
            command=self.add_to_steam,
            state="disabled",
        )
        self.add_steam_button.grid(row=5, column=1, sticky="ew", **pad)

        ttk.Label(main, text="Detected AppID / Launch Target").grid(row=6, column=0, sticky="w", **pad)
        ttk.Entry(main, textvariable=self.detected_appid_var, width=90).grid(
            row=7, column=0, columnspan=2, sticky="ew", **pad
        )

        ttk.Label(main, text="Manifest / Source").grid(row=8, column=0, sticky="w", **pad)
        ttk.Entry(main, textvariable=self.detected_manifest_var, width=90).grid(
            row=9, column=0, columnspan=2, sticky="ew", **pad
        )

        ttk.Label(main, text="Chosen application entry / Mode").grid(row=10, column=0, sticky="w", **pad)
        ttk.Entry(main, textvariable=self.detected_app_entry_var, width=90).grid(
            row=11, column=0, columnspan=2, sticky="ew", **pad
        )

        ttk.Separator(main).grid(row=12, column=0, columnspan=2, sticky="ew", pady=8)

        sgdb_frame = ttk.LabelFrame(main, text="SteamGridDB")
        sgdb_frame.grid(row=13, column=0, columnspan=2, sticky="nsew", padx=10, pady=8)

        ttk.Label(sgdb_frame, text="Search").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(sgdb_frame, textvariable=self.search_term_var, width=70).grid(
            row=0, column=1, sticky="ew", **pad
        )
        ttk.Button(sgdb_frame, text="Search SteamGridDB", command=self.search_sgdb).grid(
            row=0, column=2, sticky="ew", **pad
        )

        results_frame = ttk.Frame(sgdb_frame)
        results_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=10, pady=6)
        self.results_listbox = tk.Listbox(results_frame, height=6, exportselection=False)
        results_scrollbar = ttk.Scrollbar(results_frame, orient="vertical", command=self.results_listbox.yview)
        self.results_listbox.configure(yscrollcommand=results_scrollbar.set)
        self.results_listbox.pack(side="left", fill="both", expand=True)
        results_scrollbar.pack(side="right", fill="y")
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)

        ttk.Button(sgdb_frame, text="Load Asset Options", command=self.load_asset_options).grid(
            row=2, column=2, sticky="ew", **pad
        )

        self.cover_combo = ttk.Combobox(sgdb_frame, state="readonly")
        self.wide_cover_combo = ttk.Combobox(sgdb_frame, state="readonly")
        self.background_combo = ttk.Combobox(sgdb_frame, state="readonly")
        self.logo_combo = ttk.Combobox(sgdb_frame, state="readonly")
        self.icon_combo = ttk.Combobox(sgdb_frame, state="readonly")
        self.client_icon_combo = ttk.Combobox(sgdb_frame, state="readonly")

        self._add_combo_row(sgdb_frame, 3, "Cover (Capsule)", self.cover_combo)
        self._add_combo_row(sgdb_frame, 4, "Wide Cover (Header)", self.wide_cover_combo)
        self._add_combo_row(sgdb_frame, 5, "Background (Hero)", self.background_combo)
        self._add_combo_row(sgdb_frame, 6, "Logo", self.logo_combo)
        self._add_combo_row(sgdb_frame, 7, "Icon", self.icon_combo)
        self._add_combo_row(sgdb_frame, 8, "Client Icon", self.client_icon_combo)

        ttk.Button(sgdb_frame, text="Download Selected Assets", command=self.download_selected_assets).grid(
            row=9, column=1, sticky="ew", padx=10, pady=8
        )

        self.apply_artwork_button = ttk.Button(
            sgdb_frame,
            text="Apply Artwork to Steam",
            command=self.apply_artwork,
            state="disabled",
        )
        self.apply_artwork_button.grid(row=9, column=2, sticky="ew", padx=10, pady=8)

        sgdb_frame.columnconfigure(1, weight=1)
        sgdb_frame.rowconfigure(1, weight=1)

        main.columnconfigure(0, weight=1)
        main.rowconfigure(13, weight=1)

        self.launcher_folder_var.trace_add("write", lambda *_: self.update_create_button_state())
        self.game_exe_var.trace_add("write", lambda *_: self.update_create_button_state())

    def _add_combo_row(self, parent, row, label_text, combo):
        ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="w", padx=10, pady=4)
        combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=10, pady=4)

    def update_create_button_state(self):
        launcher_folder = self.launcher_folder_var.get().strip()
        game_exe = self.game_exe_var.get().strip()
        self.create_button.config(state="normal" if launcher_folder and game_exe else "disabled")

    def pick_launcher_folder(self):
        folder = filedialog.askdirectory(title="Select launcher folder")
        if folder:
            self.launcher_folder_var.set(folder)

    def pick_game_exe(self):
        path = filedialog.askopenfilename(
            title="Select actual game executable",
            filetypes=[("Executable files", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.game_exe_var.set(path)

    def create_helper(self):
        launcher_folder = self.launcher_folder_var.get().strip()
        game_exe = self.game_exe_var.get().strip()

        if not launcher_folder or not os.path.isdir(launcher_folder):
            messagebox.showerror("Invalid folder", "Please select a valid launcher folder.")
            return

        if not game_exe or not os.path.isfile(game_exe):
            messagebox.showerror("Invalid game exe", "Please select a valid actual game executable.")
            return

        manifest_path = find_manifest_upwards(game_exe)
        game_exe_name = os.path.basename(game_exe)

        try:
            if manifest_path:
                shell_app_id, chosen, manifest_path = build_shell_app_id(game_exe)
                game_config = make_game_config("gamepass", shell_app_id, game_exe_name, launcher_folder)
                self.detected_appid_var.set(shell_app_id)
                self.detected_manifest_var.set(manifest_path)
                self.detected_app_entry_var.set(
                    f"Game Pass mode | Id={chosen['app_id']} | Executable={chosen.get('executable', '')} | DisplayName={chosen.get('display_name', '')}"
                )
            else:
                epic_app_name, epic_source = find_epic_app_name(game_exe)
                if epic_app_name:
                    launch_uri = f"com.epicgames.launcher://apps/{epic_app_name}?action=launch"
                    game_config = make_game_config("epic_uri", launch_uri, game_exe_name, launcher_folder)
                    self.detected_appid_var.set(launch_uri)
                    self.detected_manifest_var.set(epic_source or "Epic manifest")
                    self.detected_app_entry_var.set("Epic launcher URL mode")
                else:
                    game_config = make_game_config("direct_exe", game_exe, game_exe_name, launcher_folder)
                    self.detected_appid_var.set(game_exe)
                    self.detected_manifest_var.set("No AppxManifest.xml or Epic manifest match found")
                    self.detected_app_entry_var.set("Direct EXE mode")
        except Exception as e:
            messagebox.showerror("Creation failed", str(e))
            return

        output_dir = launcher_folder

        try:
            output_path = copy_helper_template_to_game_folder(output_dir)
            config_path = write_game_config(output_path, game_config)
        except Exception as e:
            messagebox.showerror("Creation failed", f"Could not create helper files:\n{e}")
            return

        self.last_helper_path = output_path
        self.last_start_dir = os.path.dirname(output_path)
        self.last_game_name = get_game_name_from_path(output_dir)
        self.last_shortcut_appid = None
        self.add_steam_button.config(state="normal")
        self.apply_artwork_button.config(state="disabled")

        if not self.search_term_var.get().strip():
            self.search_term_var.set(self.last_game_name)

        messagebox.showinfo(
            "Done",
            f"Created:\n{output_path}\n\n"
            f"Config:\n{config_path}\n\n"
            f"Game exe:\n{game_exe_name}\n\n"
            f"Log will be written to:\n{os.path.join(output_dir, 'steamlaunchhelper.log')}\n\n"
            "v0.9 uses one reusable helper EXE plus a per-game config file, so future creations should be much faster."
        )

    def add_to_steam(self):
        if not self.last_helper_path or not os.path.isfile(self.last_helper_path):
            messagebox.showerror("Missing helper EXE", "Please create steamlaunchhelper.exe first.")
            return

        while is_steam_running():
            if not messagebox.askyesno(
                "Steam is running",
                "Steam is still running.\n\nPlease fully exit Steam.\n\nClick Yes to check again or No to cancel."
            ):
                return

        try:
            message, appid = add_helper_to_steam_library(
                helper_exe_path=self.last_helper_path,
                start_dir=self.last_start_dir,
                app_name=self.last_game_name or "Non-Steam Game",
            )
            self.last_shortcut_appid = appid
            messagebox.showinfo("Steam Library", f"{message}\n\nShortcut AppID: {appid}")
        except Exception as e:
            messagebox.showerror("Steam Error", str(e))

    def search_sgdb(self):
        term = self.search_term_var.get().strip()
        if not term:
            messagebox.showerror("Missing search term", "Enter a game name to search.")
            return

        try:
            results = self.sgdb.search_games(term)
        except Exception as e:
            messagebox.showerror("SteamGridDB Error", str(e))
            return

        self.sgdb_results = results
        self.results_listbox.delete(0, tk.END)

        for item in results:
            game_id = item.get("id", "")
            name = item.get("name", "")
            types = item.get("types", [])
            self.results_listbox.insert(tk.END, f"{name} | id={game_id} | {', '.join(types)}")

        if not results:
            messagebox.showwarning("No results", "No SteamGridDB results found.")

    def load_asset_options(self):
        selection = self.results_listbox.curselection()
        if not selection:
            messagebox.showerror("No selection", "Select a SteamGridDB game result first.")
            return

        selected_index = selection[0]
        self.selected_sgdb_game = self.sgdb_results[selected_index]
        game_id = self.selected_sgdb_game["id"]

        try:
            grids = self.sgdb.get_grids(game_id)
            heroes = self.sgdb.get_heroes(game_id)
            logos = self.sgdb.get_logos(game_id)
            icons = self.sgdb.get_icons(game_id)
        except Exception as e:
            messagebox.showerror("SteamGridDB Error", str(e))
            return

        portrait_grids = [x for x in grids if int(x.get("height", 0)) > int(x.get("width", 0))]
        wide_grids = [x for x in grids if int(x.get("width", 0)) >= int(x.get("height", 0))]

        self.asset_lists["cover"] = portrait_grids[:25]
        self.asset_lists["wide_cover"] = wide_grids[:25]
        self.asset_lists["background"] = heroes[:25]
        self.asset_lists["logo"] = logos[:25]
        self.asset_lists["icon"] = icons[:25]
        self.asset_lists["client_icon"] = icons[:25]

        self._fill_combo(self.cover_combo, self.asset_lists["cover"])
        self._fill_combo(self.wide_cover_combo, self.asset_lists["wide_cover"])
        self._fill_combo(self.background_combo, self.asset_lists["background"])
        self._fill_combo(self.logo_combo, self.asset_lists["logo"])
        self._fill_combo(self.icon_combo, self.asset_lists["icon"])
        self._fill_combo(self.client_icon_combo, self.asset_lists["client_icon"])

        warnings = []
        if not self.asset_lists["cover"]:
            warnings.append("Cover")
        if not self.asset_lists["wide_cover"]:
            warnings.append("Wide Cover")
        if not self.asset_lists["background"]:
            warnings.append("Background")
        if not self.asset_lists["logo"]:
            warnings.append("Logo")
        if not self.asset_lists["icon"]:
            warnings.append("Icon")
        if not self.asset_lists["client_icon"]:
            warnings.append("Client Icon")

        if warnings:
            messagebox.showwarning("Missing asset types", "Missing: " + ", ".join(warnings))
        else:
            messagebox.showinfo("Assets loaded", "Asset options loaded successfully.")

    def _fill_combo(self, combo: ttk.Combobox, assets: list[dict]):
        values = [format_asset_option(asset, idx) for idx, asset in enumerate(assets)]
        combo["values"] = values
        if values:
            combo.current(0)
        else:
            combo.set("")

    def _get_selected_asset(self, asset_type: str, combo: ttk.Combobox) -> dict | None:
        index = combo.current()
        if index < 0:
            return None
        assets = self.asset_lists.get(asset_type, [])
        if index >= len(assets):
            return None
        return assets[index]

    def get_local_grid_folder(self) -> str:
        if not self.last_game_name:
            raise RuntimeError("Create the helper EXE first so the game name is known.")
        folder = os.path.join(get_app_base_dir(), "grid", sanitize_filename(self.last_game_name))
        os.makedirs(folder, exist_ok=True)
        return folder

    def download_selected_assets(self):
        if not self.selected_sgdb_game:
            messagebox.showerror("No game selected", "Search SteamGridDB and load asset options first.")
            return

        local_folder = self.get_local_grid_folder()

        selected_assets = {
            "cover": self._get_selected_asset("cover", self.cover_combo),
            "wide_cover": self._get_selected_asset("wide_cover", self.wide_cover_combo),
            "background": self._get_selected_asset("background", self.background_combo),
            "logo": self._get_selected_asset("logo", self.logo_combo),
            "icon": self._get_selected_asset("icon", self.icon_combo),
            "client_icon": self._get_selected_asset("client_icon", self.client_icon_combo),
        }

        warnings = [name for name, asset in selected_assets.items() if asset is None]
        if warnings:
            messagebox.showwarning("Missing selections", "No selection for: " + ", ".join(warnings))

        downloaded = {}
        for asset_name, asset in selected_assets.items():
            if not asset:
                continue

            url = asset.get("url")
            if not url:
                continue

            ext = get_file_extension_from_url(url)
            filename = {
                "cover": f"capsule{ext}",
                "wide_cover": f"header{ext}",
                "background": f"hero{ext}",
                "logo": f"logo{ext}",
                "icon": f"icon{ext}",
                "client_icon": f"client_icon{ext}",
            }[asset_name]

            target_path = os.path.join(local_folder, filename)

            try:
                download_file(url, target_path)
                downloaded[asset_name] = target_path
            except Exception as e:
                messagebox.showerror("Download failed", f"Failed to download {asset_name}:\n{e}")
                return

        self.downloaded_assets = downloaded

        if not downloaded:
            messagebox.showwarning("No assets downloaded", "Nothing was downloaded.")
            return

        self.apply_artwork_button.config(state="normal")
        messagebox.showinfo("Done", f"Assets saved to:\n{local_folder}")

    def apply_artwork(self):
        if not self.last_helper_path or not os.path.isfile(self.last_helper_path):
            messagebox.showerror("Missing helper EXE", "Create steamlaunchhelper.exe first.")
            return

        if not self.downloaded_assets:
            messagebox.showerror("Missing assets", "Download assets first.")
            return

        while is_steam_running():
            if not messagebox.askyesno(
                "Steam is running",
                "Steam is still running.\n\nPlease fully exit Steam.\n\nClick Yes to check again or No to cancel."
            ):
                return

        app_name = self.last_game_name or "Non-Steam Game"
        appid = self.last_shortcut_appid or compute_shortcut_appid(self.last_helper_path, app_name)
        grid_appid = to_unsigned_appid(appid)
        grid_dir = get_steam_grid_dir()

        copied = []
        warnings = []

        cover = self.downloaded_assets.get("cover")
        if cover:
            ext = os.path.splitext(cover)[1]
            if copy_if_exists(cover, os.path.join(grid_dir, f"{grid_appid}p{ext}")):
                copied.append("Cover")
        else:
            warnings.append("Cover")

        wide_cover = self.downloaded_assets.get("wide_cover")
        if wide_cover:
            ext = os.path.splitext(wide_cover)[1]
            if copy_if_exists(wide_cover, os.path.join(grid_dir, f"{grid_appid}{ext}")):
                copied.append("Wide Cover")
        else:
            warnings.append("Wide Cover")

        background = self.downloaded_assets.get("background")
        if background:
            ext = os.path.splitext(background)[1]
            if copy_if_exists(background, os.path.join(grid_dir, f"{grid_appid}_hero{ext}")):
                copied.append("Background")
        else:
            warnings.append("Background")

        logo = self.downloaded_assets.get("logo")
        if logo:
            ext = os.path.splitext(logo)[1]
            if copy_if_exists(logo, os.path.join(grid_dir, f"{grid_appid}_logo{ext}")):
                copied.append("Logo")
        else:
            warnings.append("Logo")

        icon_path = self.downloaded_assets.get("client_icon") or self.downloaded_assets.get("icon")
        if icon_path:
            try:
                set_shortcut_icon(self.last_helper_path, icon_path)
                copied.append("Shortcut Icon")
            except Exception as e:
                messagebox.showerror("Steam Error", str(e))
                return
        else:
            warnings.append("Shortcut Icon")

        msg = f"Applied artwork for AppID: {appid}"
        if copied:
            msg += "\n\nApplied: " + ", ".join(copied)
        if warnings:
            msg += "\n\nMissing: " + ", ".join(warnings)

        messagebox.showinfo("Artwork Applied", msg)


def main():
    if os.name != "nt":
        print("This tool is for Windows only.")
        sys.exit(1)

    root = tk.Tk()
    root.title("SteamLaunchHelper Beta v0.9")

    try:
        icon_path = resource_path("steamlaunchhelper.png")
        if os.path.exists(icon_path):
            icon_img = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, icon_img)
            root._icon_img = icon_img
    except Exception:
        pass

    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()