import os
import re
import sys
import json
import glob
import zlib
import shutil
import subprocess
import hashlib
from io import BytesIO
import requests
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from dataclasses import dataclass

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except Exception:
    Image = None
    ImageTk = None
    PIL_AVAILABLE = False

import vdf


APP_NAME = "SteamLaunchHelper v3.5"
STEAMGRIDDB_API_KEY = "ec6a77308546cd7f86bfc36d711df2d6".strip()
STEAMGRIDDB_BASE_URL = "https://www.steamgriddb.com/api/v2"
HELPER_FOLDER_NAME = "steamlaunchhelper"
LAUNCHER_EXE_NAME = "launcher.exe"
CONFIG_FILENAME = "launcher_config.json"


# -----------------------------
# Paths / basic helpers
# -----------------------------

def resource_path(relative_path: str) -> str:
    base_path = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base_path, relative_path)


def center_window(window: tk.Toplevel | tk.Tk, parent: tk.Tk, width: int, height: int) -> None:
    """Center a child window inside the visible bounds of the main app window.

    Tk can sometimes report winfo_rootx()/winfo_rooty() as 0 while another
    Toplevel is active. Parsing the parent's geometry string is more reliable
    for our modal positioning on Windows.
    """
    try:
        parent.update_idletasks()
        window.update_idletasks()
    except Exception:
        pass

    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    pw = max(parent.winfo_width(), 1)
    ph = max(parent.winfo_height(), 1)

    try:
        geom = parent.winfo_geometry()
        match = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geom)
        if match:
            gw, gh, gx, gy = map(int, match.groups())
            if gw > 100 and gh > 100:
                pw, ph, px, py = gw, gh, gx, gy
    except Exception:
        pass

    x = px + max((pw - width) // 2, 0)
    y = py + max((ph - height) // 2, 0)
    window.geometry(f"{width}x{height}+{x}+{y}")


def resize_image_to_cover(img, size: tuple[int, int]):
    """Resize without distortion and crop center so the image fills the container."""
    target_w, target_h = size
    if target_w <= 0 or target_h <= 0:
        return img
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max((new_w - target_w) // 2, 0)
    top = max((new_h - target_h) // 2, 0)
    return img.crop((left, top, left + target_w, top + target_h))


def load_fitted_photo(path: str, max_width: int, max_height: int) -> tk.PhotoImage | None:
    """Load artwork and fill a fixed container without stretching."""
    if not path or not os.path.isfile(path):
        return None
    try:
        if PIL_AVAILABLE:
            img = Image.open(path).convert("RGBA")
            if img.width < 120 or img.height < 120:
                return None
            img = resize_image_to_cover(img, (max_width, max_height))
            return ImageTk.PhotoImage(img)
        img = tk.PhotoImage(file=path)
        factor = max(1, int(max(img.width() / max_width, img.height() / max_height)))
        if factor > 1:
            img = img.subsample(factor, factor)
        return img
    except Exception:
        return None


def is_good_cover_image(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    if not PIL_AVAILABLE:
        return True
    try:
        img = Image.open(path)
        return img.width >= 300 and img.height >= 300
    except Exception:
        return False


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


def prettify_game_name(raw: str) -> str:
    raw = os.path.splitext(os.path.basename(raw))[0]
    raw = raw.replace("_", " ").replace("-", " ")
    raw = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", raw)
    raw = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or "Non-Steam Game"


def to_unsigned_appid(appid: int) -> int:
    return appid & 0xFFFFFFFF


def compute_shortcut_appid(exe_path: str, app_name: str) -> int:
    unique_name = (exe_path + app_name).encode("utf-8")
    appid = zlib.crc32(unique_name) | 0x80000000
    if appid > 0x7FFFFFFF:
        appid -= 0x100000000
    return appid


def is_steam_running() -> bool:
    try:
        output = subprocess.check_output("tasklist", shell=True, text=True).lower()
        return "steam.exe" in output
    except Exception:
        return False


# -----------------------------
# Steam shortcuts / grid
# -----------------------------

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


def load_shortcuts() -> dict:
    shortcuts_path = get_steam_shortcuts_path()
    if os.path.exists(shortcuts_path) and os.path.getsize(shortcuts_path) > 0:
        try:
            with open(shortcuts_path, "rb") as f:
                return vdf.binary_load(f)
        except Exception:
            return {"shortcuts": {}}
    return {"shortcuts": {}}


def save_shortcuts(data: dict) -> None:
    shortcuts_path = get_steam_shortcuts_path()
    os.makedirs(os.path.dirname(shortcuts_path), exist_ok=True)
    with open(shortcuts_path, "wb") as f:
        vdf.binary_dump(data, f)


def get_steam_grid_dir() -> str:
    config_dir = os.path.dirname(get_steam_shortcuts_path())
    grid_dir = os.path.join(config_dir, "grid")
    os.makedirs(grid_dir, exist_ok=True)
    return grid_dir


def get_grid_image_for_appid(appid: int) -> str | None:
    grid_dir = get_steam_grid_dir()
    unsigned = to_unsigned_appid(appid)
    ids_to_try = [str(unsigned), str(appid)]
    image_exts = (".png", ".jpg", ".jpeg", ".webp")

    def usable(path: str) -> bool:
        name = os.path.basename(path).lower()
        if not name.endswith(image_exts):
            return False
        if "_logo" in name or "_hero" in name:
            return False
        return os.path.isfile(path)

    # Prefer portrait capsules first: <appid>p.png / jpg / webp
    for grid_id in ids_to_try:
        for ext in image_exts:
            candidate = os.path.join(grid_dir, f"{grid_id}p{ext}")
            if usable(candidate):
                return candidate

    # Then fall back to header/card images.
    for grid_id in ids_to_try:
        for ext in image_exts:
            candidate = os.path.join(grid_dir, f"{grid_id}{ext}")
            if usable(candidate):
                return candidate

    # Last-resort glob fallback in case SteamGridDB returned uncommon extensions.
    for grid_id in ids_to_try:
        for pattern in (os.path.join(grid_dir, f"{grid_id}p.*"), os.path.join(grid_dir, f"{grid_id}.*")):
            for match in glob.glob(pattern):
                if usable(match):
                    return match
    return None


def add_or_update_steam_shortcut(helper_exe_path: str, app_name: str) -> tuple[str, int]:
    start_dir = os.path.dirname(helper_exe_path)
    expected_appid = compute_shortcut_appid(helper_exe_path, app_name)
    data = load_shortcuts()
    shortcuts = data.setdefault("shortcuts", {})

    for entry in shortcuts.values():
        existing_exe = str(entry.get("Exe", "")).strip('"').lower()
        if existing_exe == helper_exe_path.lower():
            appid = int(entry.get("appid", 0)) or expected_appid
            entry.update({
                "appid": appid,
                "AppName": app_name,
                "Exe": helper_exe_path,
                "StartDir": start_dir,
                "LaunchOptions": "",
            })
            save_shortcuts(data)
            return "Updated existing Steam shortcut.", appid

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
    save_shortcuts(data)
    return f'Added "{app_name}" to Steam Library.', expected_appid


def set_shortcut_icon(helper_exe_path: str, icon_path: str) -> None:
    data = load_shortcuts()
    shortcuts = data.setdefault("shortcuts", {})
    updated = False
    for entry in shortcuts.values():
        existing_exe = str(entry.get("Exe", "")).strip('"').lower()
        if existing_exe == helper_exe_path.lower():
            entry["icon"] = icon_path
            updated = True
            break
    if updated:
        save_shortcuts(data)


def update_shortcut_display_name(helper_exe_path: str, appid: int, new_name: str) -> None:
    """Update the visible Steam shortcut name without changing its AppID."""
    data = load_shortcuts()
    shortcuts = data.setdefault("shortcuts", {})
    target_exe = helper_exe_path.lower()
    changed = False
    for entry in shortcuts.values():
        existing_exe = str(entry.get("Exe", "")).strip('"').lower()
        existing_appid = int(entry.get("appid", 0) or 0)
        if existing_exe == target_exe or existing_appid == int(appid):
            entry["AppName"] = new_name
            changed = True
            break
    if not changed:
        raise RuntimeError("Could not find matching Steam shortcut to update the game name.")
    save_shortcuts(data)


# -----------------------------
# Launcher template / config
# -----------------------------

def get_template_launcher_dir() -> str:
    candidates = [
        os.path.join(get_app_base_dir(), "launcher"),
        resource_path("launcher"),
    ]
    checked = []
    for folder in candidates:
        folder = os.path.abspath(folder)
        checked.append(folder)
        if os.path.isfile(os.path.join(folder, LAUNCHER_EXE_NAME)):
            return folder
    raise RuntimeError(
        "Bundled launcher template not found. Expected launcher\\launcher.exe next to SteamLaunchHelper.exe.\n\n"
        "Checked:\n" + "\n".join(checked)
    )


def copy_launcher_template_to_game_folder(destination_parent: str) -> str:
    template_folder = get_template_launcher_dir()
    destination_folder = os.path.join(destination_parent, HELPER_FOLDER_NAME)
    os.makedirs(destination_folder, exist_ok=True)
    shutil.copytree(template_folder, destination_folder, dirs_exist_ok=True)
    destination_exe = os.path.join(destination_folder, LAUNCHER_EXE_NAME)
    if not os.path.isfile(destination_exe):
        raise RuntimeError("Launcher copy completed, but launcher.exe was not found.")
    return destination_exe


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
        "launcher_runtime_version": "1.1",
    }


def write_game_config(launcher_exe_path: str, config: dict) -> str:
    config_path = os.path.join(os.path.dirname(launcher_exe_path), CONFIG_FILENAME)
    with open(config_path, "w", encoding="utf-8", newline="\r\n") as f:
        json.dump(config, f, indent=4)
    return config_path


# -----------------------------
# Game Pass / Epic detection
# -----------------------------

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
    apps = []
    for app in apps_node.findall("a:Application", ns):
        app_id = app.attrib.get("Id")
        executable = app.attrib.get("Executable", "")
        visual = app.find("{http://schemas.microsoft.com/appx/manifest/uap/windows10}VisualElements")
        display_name = visual.attrib.get("DisplayName", "") if visual is not None else ""
        if app_id:
            apps.append({"package_name": package_name, "app_id": app_id, "executable": executable, "display_name": display_name})
    if not apps:
        raise RuntimeError("No Application entries found in AppxManifest.xml")
    return apps


def choose_best_app(apps: list[dict], selected_game_exe: str) -> dict:
    exe_name = os.path.basename(selected_game_exe).lower()
    exe_stem = os.path.splitext(exe_name)[0]
    for app in apps:
        manifest_exe = os.path.basename(app.get("executable", "")).lower()
        if manifest_exe == exe_name:
            return app
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
    package_name = identity.attrib.get("Name") if identity is not None else ""
    if not package_name:
        raise RuntimeError("Package Name missing in AppxManifest.xml")
    cmd = ["powershell", "-NoProfile", "-Command", "Get-AppxPackage | " f"Where-Object {{$_.Name -eq '{package_name}'}} | " "Select-Object -First 1 -ExpandProperty PackageFamilyName"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    package_family_name = result.stdout.strip()
    if not package_family_name:
        raise RuntimeError(f"Could not resolve PackageFamilyName for package '{package_name}'.")
    apps = get_apps_from_manifest(manifest_path)
    chosen = choose_best_app(apps, selected_game_exe)
    return f"shell:AppsFolder\\{package_family_name}!{chosen['app_id']}", chosen, manifest_path


def get_epic_manifest_candidates() -> list[str]:
    candidates = []
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    manifest_dir_1 = os.path.join(program_data, "Epic", "EpicGamesLauncher", "Data", "Manifests")
    manifest_file_2 = os.path.join(program_data, "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat")
    if os.path.isdir(manifest_dir_1):
        candidates.extend(glob.glob(os.path.join(manifest_dir_1, "*.item")))
        candidates.extend(glob.glob(os.path.join(manifest_dir_1, "*.json")))
    if os.path.isfile(manifest_file_2):
        candidates.append(manifest_file_2)
    return candidates


def extract_epic_app_name_from_item(data: dict) -> str | None:
    for key in ("AppName", "MainGameAppName", "CatalogItemId", "NamespaceId", "ArtifactId"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None




EPIC_UTILITY_FOLDER_NAMES = {
    "directxredist",
    "redist",
    "redistributable",
    "redistributables",
    "prereq",
    "prereqs",
    "support",
    "engine",
    "launcher",
    "epic games launcher",
    "epiconlineservices",
    "epic online services",
}

EPIC_UTILITY_NAME_TERMS = (
    "redist", "redistributable", "directx", "vcredist", "vc_redist",
    "prereq", "support", "launcher", "online services", "eos",
)


def is_likely_epic_utility_folder(path: str) -> tuple[bool, str]:
    name = os.path.basename(os.path.normpath(path)).strip().lower()
    if not name:
        return False, ""
    if name in EPIC_UTILITY_FOLDER_NAMES:
        return True, "Epic utility/prerequisite folder"
    if any(term in name for term in EPIC_UTILITY_NAME_TERMS):
        return True, "folder name looks like Epic utility/prerequisite content"
    return False, ""


def find_epic_app_name(game_exe_path: str) -> tuple[str | None, str | None]:
    game_exe_path = normalize_path(game_exe_path)
    game_dir = os.path.dirname(game_exe_path)
    for manifest_file in get_epic_manifest_candidates():
        try:
            with open(manifest_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            continue
        if manifest_file.lower().endswith("launcherinstalled.dat"):
            for item in data.get("InstallationList", []):
                install_location = item.get("InstallLocation")
                app_name = item.get("AppName") or item.get("ArtifactId")
                if install_location and app_name and (is_path_within(game_exe_path, install_location) or is_path_within(game_dir, install_location)):
                    return app_name, manifest_file
        else:
            install_location = data.get("InstallLocation") or data.get("ManifestLocation") or data.get("StagingLocation")
            app_name = extract_epic_app_name_from_item(data)
            if install_location and app_name and (is_path_within(game_exe_path, install_location) or is_path_within(game_dir, install_location)):
                return app_name, manifest_file
    return None, None


def find_main_exe_in_folder(folder: str) -> str | None:
    bad_words = ("launcher", "gamelaunchhelper", "steamlaunchhelper", "crash", "report", "redist", "setup", "unins", "unitycrash", "eos", "easyanticheat", "vc_redist")
    candidates = []
    for root, dirs, files in os.walk(folder):
        # Keep scanning bounded so Program Files isn't painful.
        depth = os.path.relpath(root, folder).count(os.sep)
        if depth > 5:
            dirs[:] = []
            continue
        for file in files:
            if not file.lower().endswith(".exe"):
                continue
            name = file.lower()
            full = os.path.join(root, file)
            if any(w in name for w in bad_words):
                penalty = 100
            else:
                penalty = 0
            score = penalty
            if "shipping" in name:
                score -= 40
            if "win64" in root.lower() or "binaries" in root.lower():
                score -= 20
            try:
                size = os.path.getsize(full)
            except Exception:
                size = 0
            candidates.append((score, -size, full))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]



XBOX_DLC_NAME_TERMS = (
    "dlc", "add-on", "addon", "expansion", "pack", "season pass", "skin",
    "costume", "outfit", "soundtrack", "artbook", "bonus", "deluxe content",
    "weapon", "armor", "armour", "cosmetic", "currency", "the order of giants",
    "lion tamer whip", "traveling suit", "travelling suit",
)


def is_helper_exe_path(path: str) -> bool:
    name = os.path.basename(path).lower()
    return name in {"gamelaunchhelper.exe", "steamlaunchhelper.exe", "launcher.exe"}


def xbox_folder_has_base_sibling(game_dir: str) -> bool:
    """DLC folders in XboxGames often use: '<Base Game> - <DLC Name>'.

    If '<Base Game>' exists as a sibling folder, treat this folder as add-on content.
    This prevents items like 'Indiana Jones ... - Lion Tamer Whip' from being added.
    """
    name = os.path.basename(os.path.normpath(game_dir)).strip()
    if " - " not in name:
        return False
    base = name.split(" - ", 1)[0].strip().lower()
    parent = os.path.dirname(os.path.normpath(game_dir))
    try:
        siblings = {x.lower() for x in os.listdir(parent) if os.path.isdir(os.path.join(parent, x))}
    except Exception:
        return False
    return base in siblings


def read_text_safely(path: str, max_chars: int = 200000) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            return f.read(max_chars)
    except Exception:
        return ""


def microsoft_config_looks_like_dlc(config_path: str) -> bool:
    text = read_text_safely(config_path).lower()
    if not text:
        return False
    # Conservative text markers only. Folder-name/sibling checks are still the primary filter.
    markers = (
        "packagetype=\"dlc", "packagetype='dlc", "<dlc", "isdlc=\"true", "isdlc='true",
        "add-on", "addon", "downloadablecontent", "downloadable content",
    )
    return any(m in text for m in markers)


def is_likely_xbox_dlc_folder(game_dir: str) -> tuple[bool, str]:
    name = os.path.basename(os.path.normpath(game_dir)).strip()
    lower_name = name.lower()

    if xbox_folder_has_base_sibling(game_dir):
        return True, "folder looks like '<base game> - <add-on>' and base game exists"

    # Only apply broad DLC terms when they are clearly in an add-on-style suffix.
    suffix = lower_name.split(" - ", 1)[1] if " - " in lower_name else lower_name
    if any(term in suffix for term in XBOX_DLC_NAME_TERMS):
        return True, "folder name looks like DLC/add-on content"

    config_path = os.path.join(game_dir, "Content", "MicrosoftGame.config")
    if os.path.isfile(config_path) and microsoft_config_looks_like_dlc(config_path):
        return True, "MicrosoftGame.config looks like DLC/add-on content"

    return False, ""


@dataclass
class GamePlan:
    name: str
    game_exe: str
    mode: str
    launch_target: str
    start_dir: str
    source: str
    search_term: str


def build_game_plan_from_exe(game_exe: str) -> GamePlan:
    game_exe = os.path.abspath(game_exe)
    game_exe_name = os.path.basename(game_exe)
    start_dir = os.path.dirname(game_exe)
    manifest_path = find_manifest_upwards(game_exe)
    if manifest_path:
        shell_app_id, chosen, _ = build_shell_app_id(game_exe)
        content_dir = os.path.dirname(manifest_path)
        return GamePlan(
            name=get_xbox_game_name_from_path(game_exe),
            game_exe=game_exe,
            mode="gamepass",
            launch_target=shell_app_id,
            start_dir=content_dir,
            source="Xbox/Game Pass",
            search_term=get_xbox_game_name_from_path(game_exe),
        )
    epic_app_name, epic_source = find_epic_app_name(game_exe)
    if epic_app_name:
        root_name = get_epic_game_name_from_path(game_exe)
        return GamePlan(
            name=root_name,
            game_exe=game_exe,
            mode="epic_uri",
            launch_target=f"com.epicgames.launcher://apps/{epic_app_name}?action=launch",
            start_dir=os.path.dirname(game_exe),
            source="Epic Games Store",
            search_term=root_name,
        )
    return GamePlan(
        name=prettify_game_name(os.path.basename(game_exe)),
        game_exe=game_exe,
        mode="direct_exe",
        launch_target=game_exe,
        start_dir=os.path.dirname(game_exe),
        source="Direct EXE",
        search_term=prettify_game_name(os.path.basename(game_exe)),
    )


def get_xbox_game_name_from_path(path: str) -> str:
    parts = os.path.normpath(path).split(os.sep)
    for i, part in enumerate(parts):
        if part.lower() == "xboxgames" and i + 1 < len(parts):
            return prettify_game_name(parts[i + 1])
    return prettify_game_name(os.path.basename(os.path.dirname(path)))


def get_epic_game_name_from_path(path: str) -> str:
    parts = os.path.normpath(path).split(os.sep)
    for i, part in enumerate(parts):
        if part.lower() == "epic games" and i + 1 < len(parts):
            return prettify_game_name(parts[i + 1])
    return prettify_game_name(os.path.basename(os.path.dirname(path)))


# -----------------------------
# SteamGridDB
# -----------------------------

class SteamGridDBClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    def _headers(self) -> dict:
        if not self.api_key:
            raise RuntimeError("SteamGridDB API key is missing.")
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json", "User-Agent": "SteamLaunchHelper/2.1"}

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{STEAMGRIDDB_BASE_URL}{endpoint}"
        response = requests.get(url, headers=self._headers(), params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"SteamGridDB request failed: {endpoint}")
        return payload

    def search_games(self, term: str) -> list[dict]:
        return self._get(f"/search/autocomplete/{term}").get("data", [])

    def get_grids(self, game_id: int) -> list[dict]:
        return self._get(f"/grids/game/{game_id}").get("data", [])

    def get_heroes(self, game_id: int) -> list[dict]:
        return self._get(f"/heroes/game/{game_id}").get("data", [])

    def get_logos(self, game_id: int) -> list[dict]:
        return self._get(f"/logos/game/{game_id}").get("data", [])

    def get_icons(self, game_id: int) -> list[dict]:
        return self._get(f"/icons/game/{game_id}").get("data", [])


def get_file_extension_from_url(url: str) -> str:
    path = url.split("?")[0]
    ext = os.path.splitext(path)[1].lower()
    return ext if ext else ".png"


def download_file(url: str, target_path: str) -> None:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    with open(target_path, "wb") as f:
        f.write(response.content)


def format_asset_option(asset: dict, index: int) -> str:
    return f"{index + 1}. {asset.get('width', '?')}x{asset.get('height', '?')} | score={asset.get('score', '?')} | {asset.get('style', '')}"


def fetch_asset_lists(sgdb: SteamGridDBClient, search_term: str) -> tuple[dict, dict]:
    results = sgdb.search_games(search_term)
    if not results:
        raise RuntimeError(f"No SteamGridDB result found for: {search_term}")
    selected = results[0]
    game_id = selected["id"]
    grids = sgdb.get_grids(game_id)
    heroes = sgdb.get_heroes(game_id)
    logos = sgdb.get_logos(game_id)
    icons = sgdb.get_icons(game_id)
    portrait_grids = [x for x in grids if int(x.get("height", 0)) > int(x.get("width", 0))]
    wide_grids = [x for x in grids if int(x.get("width", 0)) >= int(x.get("height", 0))]
    return selected, {
        "cover": portrait_grids[:25],
        "wide_cover": wide_grids[:25],
        "background": heroes[:25],
        "logo": logos[:25],
        "icon": icons[:25],
        "client_icon": icons[:25],
    }


def apply_assets_for_app(sgdb: SteamGridDBClient, helper_exe_path: str, appid: int, search_term: str, selections: dict | None = None) -> None:
    grid_dir = get_steam_grid_dir()
    unsigned = to_unsigned_appid(appid)
    _, asset_lists = fetch_asset_lists(sgdb, search_term)
    if selections is None:
        selections = {k: 0 for k in asset_lists.keys()}

    mapping = {
        "cover": f"{unsigned}p",
        "wide_cover": f"{unsigned}",
        "background": f"{unsigned}_hero",
        "logo": f"{unsigned}_logo",
    }
    for asset_name, base in mapping.items():
        assets = asset_lists.get(asset_name, [])
        idx = int(selections.get(asset_name, 0))
        if not assets or idx >= len(assets):
            continue
        asset = assets[idx]
        url = asset.get("url")
        if not url:
            continue
        ext = get_file_extension_from_url(url)
        download_file(url, os.path.join(grid_dir, base + ext))

    icon_assets = asset_lists.get("client_icon") or asset_lists.get("icon") or []
    if icon_assets:
        idx = int(selections.get("client_icon", 0))
        idx = min(idx, len(icon_assets) - 1)
        url = icon_assets[idx].get("url")
        if url:
            local_icon_dir = os.path.join(get_app_base_dir(), "icons")
            os.makedirs(local_icon_dir, exist_ok=True)
            ext = get_file_extension_from_url(url)
            icon_path = os.path.join(local_icon_dir, sanitize_filename(search_term) + ext)
            download_file(url, icon_path)
            set_shortcut_icon(helper_exe_path, icon_path)



# -----------------------------
# UI - CustomTkinter v3.0
# -----------------------------

@dataclass
class LibraryGame:
    name: str
    exe: str
    appid: int
    cover: str | None
    search_term: str

try:
    import customtkinter as ctk
    CTK_AVAILABLE = True
except Exception:
    ctk = None
    CTK_AVAILABLE = False


class UIColors:
    BG = "#151515"
    SURFACE = "#202020"
    CARD = "#262626"
    CARD_HOVER = "#303030"
    FIELD = "#343434"
    BORDER = "#3f3f3f"
    TEXT = "#f4f4f4"
    MUTED = "#a8a8a8"
    ACCENT = "#34aadc"
    ACCENT_HOVER = "#4bbcec"
    DANGER = "#e65f5c"




def get_artwork_preview_size(asset_type: str) -> tuple[int, int]:
    return {
        "cover": (180, 270),
        "background": (360, 120),
        "wide_cover": (360, 168),
        "logo": (360, 110),
        "client_icon": (120, 120),
        "icon": (120, 120),
    }.get(asset_type, (220, 160))


def get_asset_display_name(asset_type: str) -> str:
    return {
        "cover": "Cover",
        "background": "Background",
        "wide_cover": "Wide Cover",
        "logo": "Logo",
        "client_icon": "Client Icon",
        "icon": "Icon",
    }.get(asset_type, asset_type.replace("_", " ").title())


def get_current_grid_artwork_path(appid: int, asset_type: str) -> str | None:
    try:
        grid_dir = get_steam_grid_dir()
        unsigned = to_unsigned_appid(appid)
        base = {
            "cover": f"{unsigned}p",
            "wide_cover": f"{unsigned}",
            "background": f"{unsigned}_hero",
            "logo": f"{unsigned}_logo",
        }.get(asset_type)
        if not base:
            return None
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            candidate = os.path.join(grid_dir, base + ext)
            if os.path.isfile(candidate):
                return candidate
    except Exception:
        pass
    return None


def make_ctk_image_from_url(url: str | None, size: tuple[int, int], allow_small: bool = True):
    if not CTK_AVAILABLE or not PIL_AVAILABLE or not url:
        return None
    try:
        cache_dir = os.path.join(get_app_base_dir(), "cache", "sgdb")
        os.makedirs(cache_dir, exist_ok=True)
        ext = get_file_extension_from_url(url)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        path = os.path.join(cache_dir, digest + ext)
        if not os.path.isfile(path):
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            with open(path, "wb") as f:
                f.write(response.content)
        return make_ctk_image(path, size, allow_small=allow_small)
    except Exception:
        return None


def apply_selected_assets_for_app(helper_exe_path: str, appid: int, search_term: str, selected_assets: dict[str, dict | None]) -> None:
    grid_dir = get_steam_grid_dir()
    unsigned = to_unsigned_appid(appid)
    mapping = {
        "cover": f"{unsigned}p",
        "wide_cover": f"{unsigned}",
        "background": f"{unsigned}_hero",
        "logo": f"{unsigned}_logo",
    }
    for asset_name, base in mapping.items():
        asset = selected_assets.get(asset_name)
        if not asset:
            continue
        url = asset.get("url")
        if not url:
            continue
        ext = get_file_extension_from_url(url)
        download_file(url, os.path.join(grid_dir, base + ext))

    icon_asset = selected_assets.get("client_icon") or selected_assets.get("icon")
    if icon_asset and icon_asset.get("url"):
        local_icon_dir = os.path.join(get_app_base_dir(), "icons")
        os.makedirs(local_icon_dir, exist_ok=True)
        ext = get_file_extension_from_url(icon_asset["url"])
        icon_path = os.path.join(local_icon_dir, sanitize_filename(search_term) + ext)
        download_file(icon_asset["url"], icon_path)
        set_shortcut_icon(helper_exe_path, icon_path)


def make_ctk_image(path: str | None, size: tuple[int, int], allow_small: bool = False, fill: bool = True):
    if not CTK_AVAILABLE or not PIL_AVAILABLE or not path or not os.path.isfile(path):
        return None
    try:
        img = Image.open(path).convert("RGBA")
        if not allow_small and (img.width < 120 or img.height < 120):
            return None
        if fill:
            # Fill the preview/card area without distortion. This crops edges instead of letterboxing.
            img = resize_image_to_cover(img, size)
            canvas = img
        else:
            # Keep full image visible, useful only for transparent logos/icons when needed.
            canvas = Image.new("RGBA", size, (38, 38, 38, 255))
            img.thumbnail(size, Image.Resampling.LANCZOS)
            x = (size[0] - img.width) // 2
            y = (size[1] - img.height) // 2
            canvas.alpha_composite(img, (x, y))
        return ctk.CTkImage(light_image=canvas, dark_image=canvas, size=size)
    except Exception:
        return None


class LoadingDialog:
    def __init__(self, parent, message: str = "Processing..."):
        self.parent = parent
        self.window = ctk.CTkToplevel(parent)
        self.window.withdraw()
        self.window.title("Processing")
        self.window.configure(fg_color=UIColors.SURFACE)
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.message_var = tk.StringVar(value=message)
        self.detail_var = tk.StringVar(value="Please wait...")

        frame = ctk.CTkFrame(self.window, fg_color=UIColors.SURFACE, corner_radius=18)
        frame.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(frame, textvariable=self.message_var, text_color=UIColors.TEXT, font=("Segoe UI", 15, "bold"), anchor="w", justify="left").pack(fill="x", anchor="w")
        ctk.CTkLabel(frame, textvariable=self.detail_var, text_color=UIColors.MUTED, font=("Segoe UI", 11), anchor="w", justify="left").pack(fill="x", anchor="w", pady=(8, 14))
        self.progress = ctk.CTkProgressBar(frame, mode="indeterminate", progress_color=UIColors.ACCENT)
        self.progress.pack(fill="x")
        self.progress.start()

        self.window.update_idletasks()
        center_window(self.window, parent, 520, 160)
        self.window.deiconify()
        try:
            self.window.grab_set()
            self.window.attributes("-topmost", True)
            self.window.after(300, lambda: self.window.attributes("-topmost", False))
        except Exception:
            pass
        self.window.lift()
        self.window.focus_force()
        self.window.update()

    def update(self, message: str, detail: str | None = None):
        self.message_var.set(message)
        if detail is not None:
            self.detail_var.set(detail)
        center_window(self.window, self.parent, 520, 160)
        try:
            self.window.attributes("-topmost", True)
            self.window.after(300, lambda: self.window.attributes("-topmost", False))
        except Exception:
            pass
        self.window.lift()
        self.window.update()

    def close(self):
        try:
            self.progress.stop()
            self.window.grab_release()
            self.window.destroy()
        except Exception:
            pass


class SteamLaunchHelperApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1280x820")
        self.root.minsize(1040, 680)
        self.root.configure(fg_color=UIColors.BG)

        self.sgdb = SteamGridDBClient(STEAMGRIDDB_API_KEY)
        self.status_var = tk.StringVar(value="Ready")
        self.loading_dialog: LoadingDialog | None = None
        self.games: list[LibraryGame] = []
        self.card_images = []
        self._grid_redraw_after_id = None
        self._last_grid_columns = None
        self.build_ui()
        self.refresh_library_view(show_errors=False)

    def show_loading(self, text: str = "Processing..."):
        if self.loading_dialog is None:
            self.loading_dialog = LoadingDialog(self.root, text)
        else:
            self.loading_dialog.update(text)
        self.root.update()

    def hide_loading(self):
        if self.loading_dialog is not None:
            self.loading_dialog.close()
            self.loading_dialog = None
        self.root.update()

    def set_progress(self, text: str, value: float | None = None):
        self.status_var.set(text)
        if self.loading_dialog is not None:
            detail = None if value is None else f"Progress: {int(value)}%"
            self.loading_dialog.update(text, detail)
        self.root.update()

    def build_ui(self):
        outer = ctk.CTkFrame(self.root, fg_color=UIColors.BG, corner_radius=0)
        outer.pack(fill="both", expand=True)

        header = ctk.CTkFrame(outer, fg_color=UIColors.BG, corner_radius=0)
        header.pack(fill="x", padx=28, pady=(22, 12))
        ctk.CTkLabel(header, text="Games", text_color=UIColors.TEXT, font=("Segoe UI", 28, "bold")).pack(side="left")
        ctk.CTkButton(header, text="Refresh", width=100, height=38, fg_color=UIColors.CARD, hover_color=UIColors.CARD_HOVER, command=self.on_refresh).pack(side="right", padx=(8, 0))
        ctk.CTkButton(header, text="Add Game", width=110, height=38, fg_color=UIColors.ACCENT, hover_color=UIColors.ACCENT_HOVER, text_color="#000000", command=self.show_add_game_modal).pack(side="right")

        self.grid_frame = ctk.CTkScrollableFrame(outer, fg_color=UIColors.BG, corner_radius=0)
        self.grid_frame.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        self.grid_frame.bind("<Configure>", self.on_grid_configure)

        footer = ctk.CTkFrame(outer, fg_color=UIColors.BG, corner_radius=0, height=28)
        footer.pack(fill="x", padx=28, pady=(0, 12))
        ctk.CTkLabel(footer, textvariable=self.status_var, text_color=UIColors.MUTED, font=("Segoe UI", 11)).pack(side="left")


    def on_grid_configure(self, event=None):
        """Debounce resize redraws.

        CTkScrollableFrame fires <Configure> whenever children are created/destroyed.
        Redrawing immediately from that event creates a feedback loop and causes
        visible flicker. Only redraw when the available width changes enough to
        alter the number of card columns.
        """
        try:
            width = max(self.grid_frame.winfo_width(), 900)
            columns = max(3, width // 210)
            if columns == self._last_grid_columns:
                return
            self._last_grid_columns = columns
            if self._grid_redraw_after_id is not None:
                self.root.after_cancel(self._grid_redraw_after_id)
            self._grid_redraw_after_id = self.root.after(180, self.draw_game_grid)
        except Exception:
            pass

    def refresh_library_view(self, show_errors: bool = True):
        try:
            self.set_progress("Loading Steam shortcuts...", 20)
            self.games = self.load_existing_helper_games()
            self._last_grid_columns = None
            self.draw_game_grid()
            self.set_progress(f"Loaded {len(self.games)} SteamLaunchHelper games", 100)
        except Exception as exc:
            self.set_progress("Ready", 0)
            if show_errors:
                messagebox.showerror("Refresh failed", str(exc))

    def load_existing_helper_games(self) -> list[LibraryGame]:
        data = load_shortcuts()
        result = []
        for entry in data.get("shortcuts", {}).values():
            exe = str(entry.get("Exe", "")).strip('"')
            name = str(entry.get("AppName", "Non-Steam Game"))
            appid = int(entry.get("appid", 0)) or compute_shortcut_appid(exe, name)
            if HELPER_FOLDER_NAME.lower() not in exe.lower() and os.path.basename(exe).lower() != LAUNCHER_EXE_NAME:
                continue
            result.append(LibraryGame(name=name, exe=exe, appid=appid, cover=get_grid_image_for_appid(appid), search_term=name))
        result.sort(key=lambda g: g.name.lower())
        return result

    def draw_game_grid(self):
        if not hasattr(self, "grid_frame"):
            return
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.card_images.clear()
        if not self.games:
            ctk.CTkLabel(self.grid_frame, text="No non-Steam games found in your Steam library. Use Add Game or Refresh.", text_color=UIColors.MUTED, font=("Segoe UI", 14)).grid(row=0, column=0, padx=8, pady=30, sticky="w")
            return
        width = max(self.grid_frame.winfo_width(), 900)
        columns = max(3, width // 210)
        for idx, game in enumerate(self.games):
            row, col = divmod(idx, columns)
            self.create_game_card(self.grid_frame, game, row, col)

    def create_game_card(self, parent, game: LibraryGame, row: int, col: int):
        card = ctk.CTkFrame(parent, fg_color=UIColors.CARD, corner_radius=8, border_width=1, border_color=UIColors.BORDER, width=176, height=282)
        card.grid(row=row, column=col, padx=10, pady=10, sticky="nw")
        card.grid_propagate(False)
        img = make_ctk_image(game.cover, (176, 240)) if game.cover else None
        if img is not None:
            self.card_images.append(img)
            image_label = ctk.CTkLabel(card, text="", image=img, width=176, height=240)
        else:
            image_label = ctk.CTkLabel(card, text=game.name[:36], width=176, height=240, text_color=UIColors.MUTED, wraplength=145, font=("Segoe UI", 13, "bold"))
        image_label.pack(fill="both", expand=True, padx=0, pady=0)
        footer = ctk.CTkFrame(card, fg_color="#111111", corner_radius=0, height=36)
        footer.pack(fill="x", side="bottom")
        ctk.CTkLabel(footer, text=game.name, text_color=UIColors.TEXT, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", padx=8, pady=7)
        for widget in (card, image_label, footer):
            widget.bind("<Button-1>", lambda _e, g=game: self.show_game_modal(g))
            widget.bind("<Enter>", lambda _e, c=card: c.configure(fg_color=UIColors.CARD_HOVER))
            widget.bind("<Leave>", lambda _e, c=card: c.configure(fg_color=UIColors.CARD))

    def require_steam_closed(self) -> bool:
        if is_steam_running():
            messagebox.showwarning("Steam is running", "Please fully exit Steam before adding games, refreshing, or applying artwork. Steam must be closed so shortcuts.vdf can be updated safely.")
            return False
        return True

    def show_add_game_modal(self):
        modal = ctk.CTkToplevel(self.root)
        modal.title("Add Game")
        modal.configure(fg_color=UIColors.SURFACE)
        center_window(modal, self.root, 590, 360)
        modal.transient(self.root)
        modal.grab_set()

        content = ctk.CTkFrame(modal, fg_color=UIColors.SURFACE, corner_radius=16)
        content.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(content, text="Note for manually adding games", text_color=UIColors.TEXT, font=("Segoe UI", 18, "bold"), anchor="w").pack(fill="x", pady=(0, 10))
        msg = (
            "SteamLaunchHelper can auto-detect installed games with Refresh.\n\n"
            "Refresh scans these locations:\n"
            "• C:\\XboxGames and D:\\XboxGames\n"
            "• C:\\Program Files\\Epic Games and D:\\Program Files\\Epic Games\n\n"
            "To manually add a game, pick the main executable of the game.\n"
            "Steam must be fully closed before adding games."
        )
        ctk.CTkLabel(content, text=msg, text_color=UIColors.TEXT, justify="left", anchor="w", wraplength=530, font=("Segoe UI", 12)).pack(fill="x", anchor="w")
        buttons = ctk.CTkFrame(content, fg_color=UIColors.SURFACE)
        buttons.pack(side="bottom", fill="x", pady=(20, 0))
        ctk.CTkButton(buttons, text="Cancel", fg_color=UIColors.CARD, hover_color=UIColors.CARD_HOVER, command=modal.destroy, width=130, height=42).pack(side="right")
        ctk.CTkButton(buttons, text="Add Game", fg_color=UIColors.ACCENT, hover_color=UIColors.ACCENT_HOVER, text_color="#000000", command=lambda: self.manual_add_game(modal), width=130, height=42).pack(side="right", padx=(0, 10))

    def manual_add_game(self, modal):
        if not self.require_steam_closed():
            return
        game_exe = filedialog.askopenfilename(title="Select main game executable", filetypes=[("Executable files", "*.exe"), ("All files", "*.*")])
        if not game_exe:
            return
        try:
            self.show_loading("Adding game...")
            plan = build_game_plan_from_exe(game_exe)
            self.create_launcher_from_plan(plan, apply_artwork=True)
            modal.destroy()
            self.refresh_library_view(show_errors=False)
            self.hide_loading()
            messagebox.showinfo("Added", f"Added {plan.name} to Steam.")
        except Exception as exc:
            self.hide_loading()
            messagebox.showerror("Add Game failed", str(exc))

    def create_launcher_from_plan(self, plan: GamePlan, apply_artwork: bool = False) -> tuple[str, int]:
        self.set_progress(f"Copying launcher for {plan.name}...", 20)
        destination_parent = plan.start_dir
        helper_exe = copy_launcher_template_to_game_folder(destination_parent)
        self.set_progress("Writing launcher config...", 40)
        config = make_game_config(plan.mode, plan.launch_target, os.path.basename(plan.game_exe), plan.start_dir)
        write_game_config(helper_exe, config)
        self.set_progress("Adding Steam shortcut...", 60)
        _, appid = add_or_update_steam_shortcut(helper_exe, plan.name)
        if apply_artwork:
            self.set_progress("Fetching and applying SteamGridDB artwork...", 80)
            try:
                apply_assets_for_app(self.sgdb, helper_exe, appid, plan.search_term)
            except Exception as exc:
                print(f"Artwork failed for {plan.name}: {exc}")
        self.set_progress("Done", 100)
        return helper_exe, appid

    def on_refresh(self):
        if not self.require_steam_closed():
            return
        try:
            self.show_loading("Refreshing game library...")
            self.set_progress("Scanning XboxGames and Epic Games folders...", 10)
            plans = self.scan_games()
            if not plans:
                self.set_progress("No new games detected", 0)
                self.hide_loading()
                messagebox.showinfo("Refresh", "No games were detected in the configured folders.")
                return
            added = 0
            skipped: list[str] = []
            total = len(plans)
            for i, plan in enumerate(plans, start=1):
                self.set_progress(f"Adding {plan.name} ({i}/{total})...", 10 + (i / total) * 80)
                try:
                    self.create_launcher_from_plan(plan, apply_artwork=True)
                    added += 1
                except PermissionError as exc:
                    skipped.append(f"{plan.name}: access denied")
                    print(f"Skipped {plan.name}: {exc}")
                except Exception as exc:
                    skipped.append(f"{plan.name}: {exc}")
                    print(f"Skipped {plan.name}: {exc}")
            self.refresh_library_view(show_errors=False)
            self.hide_loading()
            msg = f"Added or updated {added} games."
            if skipped:
                preview = "\n".join(skipped[:8])
                more = f"\n...and {len(skipped) - 8} more." if len(skipped) > 8 else ""
                msg += f"\n\nSkipped {len(skipped)} item(s):\n{preview}{more}"
            messagebox.showinfo("Refresh complete", msg)
        except Exception as exc:
            self.hide_loading()
            self.set_progress("Ready", 0)
            messagebox.showerror("Refresh failed", str(exc))

    def scan_games(self) -> list[GamePlan]:
        plans: list[GamePlan] = []
        seen = set()
        xbox_roots = [r"C:\XboxGames", r"D:\XboxGames"]
        epic_roots = [r"C:\Program Files\Epic Games", r"D:\Program Files\Epic Games"]

        for root in xbox_roots:
            if not os.path.isdir(root):
                continue
            for game_dir in glob.glob(os.path.join(root, "*")):
                content_dir = os.path.join(game_dir, "Content")
                if not os.path.isdir(content_dir):
                    continue
                is_dlc, reason = is_likely_xbox_dlc_folder(game_dir)
                if is_dlc:
                    print(f"Skipped Xbox add-on/DLC {game_dir}: {reason}")
                    continue
                exe = find_main_exe_in_folder(content_dir)
                if not exe or is_helper_exe_path(exe):
                    print(f"Skipped Xbox folder without a real main executable: {game_dir}")
                    continue
                try:
                    plan = build_game_plan_from_exe(exe)
                    plan.name = prettify_game_name(os.path.basename(game_dir))
                    plan.search_term = plan.name
                    key = normalize_path(exe)
                    if key not in seen:
                        plans.append(plan); seen.add(key)
                except Exception as exc:
                    print(f"Skipped Xbox game {game_dir}: {exc}")

        for manifest_file in get_epic_manifest_candidates():
            try:
                with open(manifest_file, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except Exception:
                continue
            items = data.get("InstallationList", []) if manifest_file.lower().endswith("launcherinstalled.dat") else [data]
            for item in items:
                install = item.get("InstallLocation") or item.get("ManifestLocation") or item.get("StagingLocation")
                if not install or not os.path.isdir(install):
                    continue
                is_utility, reason = is_likely_epic_utility_folder(install)
                if is_utility:
                    print(f"Skipped Epic utility folder {install}: {reason}")
                    continue
                exe = find_main_exe_in_folder(install)
                if not exe:
                    continue
                try:
                    plan = build_game_plan_from_exe(exe)
                    plan.name = prettify_game_name(os.path.basename(install))
                    plan.search_term = plan.name
                    key = normalize_path(exe)
                    if key not in seen:
                        plans.append(plan); seen.add(key)
                except Exception as exc:
                    print(f"Skipped Epic game {install}: {exc}")

        for root in epic_roots:
            if not os.path.isdir(root):
                continue
            for game_dir in glob.glob(os.path.join(root, "*")):
                if not os.path.isdir(game_dir):
                    continue
                is_utility, reason = is_likely_epic_utility_folder(game_dir)
                if is_utility:
                    print(f"Skipped Epic utility folder {game_dir}: {reason}")
                    continue
                exe = find_main_exe_in_folder(game_dir)
                if not exe:
                    continue
                try:
                    plan = build_game_plan_from_exe(exe)
                    plan.name = prettify_game_name(os.path.basename(game_dir))
                    plan.search_term = plan.name
                    key = normalize_path(exe)
                    if key not in seen:
                        plans.append(plan); seen.add(key)
                except Exception as exc:
                    print(f"Skipped Epic folder {game_dir}: {exc}")
        return plans

    def show_game_modal(self, game: LibraryGame):
        modal = ctk.CTkToplevel(self.root)
        modal.title(game.name)
        modal.configure(fg_color=UIColors.SURFACE)
        modal.minsize(980, 720)
        center_window(modal, self.root, 980, 720)
        modal.transient(self.root)
        modal.grab_set()
        modal.grid_rowconfigure(1, weight=1)
        modal.grid_columnconfigure(0, weight=1)

        name_area = ctk.CTkFrame(modal, fg_color=UIColors.SURFACE, corner_radius=0)
        name_area.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 10))
        name_area.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(name_area, text="Name", text_color=UIColors.TEXT, font=("Segoe UI", 13, "bold"), anchor="w").grid(row=0, column=0, sticky="ew")
        name_var = tk.StringVar(value=game.name)
        name_entry = ctk.CTkEntry(name_area, textvariable=name_var, height=42, fg_color=UIColors.FIELD, border_color=UIColors.BORDER, text_color=UIColors.TEXT)
        name_entry.grid(row=1, column=0, sticky="ew", pady=(7, 0))

        body = ctk.CTkScrollableFrame(modal, fg_color=UIColors.SURFACE, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 12))
        body.grid_columnconfigure(0, weight=1)

        asset_order = [("cover", "Cover"), ("background", "Background"), ("logo", "Logo"), ("wide_cover", "Wide Cover"), ("client_icon", "Client Icon")]
        asset_lists: dict[str, list[dict]] = {}
        selected_assets: dict[str, dict | None] = {key: None for key, _ in asset_order}
        preview_labels: dict[str, ctk.CTkLabel] = {}
        info_labels: dict[str, ctk.CTkLabel] = {}
        modal._asset_images = {}

        def asset_summary(asset: dict | None) -> str:
            if not asset:
                return "Current Steam artwork" if True else "No image selected"
            return f"{asset.get('width', '?')}x{asset.get('height', '?')} | score={asset.get('score', '?')} | {asset.get('style', '')}"

        def set_preview(key: str):
            size = get_artwork_preview_size(key)
            asset = selected_assets.get(key)
            img = None
            text = "No image found"
            if asset and asset.get("url"):
                img = make_ctk_image_from_url(asset.get("url"), size, allow_small=True)
                text = "" if img else "Preview unavailable"
                info_labels[key].configure(text=asset_summary(asset))
            else:
                local = game.cover if key == "cover" else get_current_grid_artwork_path(game.appid, key)
                img = make_ctk_image(local, size, allow_small=True) if local else None
                text = "" if img else "No image found"
                info_labels[key].configure(text="Current Steam artwork" if img else "No image selected")
            modal._asset_images[key] = img
            preview_labels[key].configure(image=img, text=text)

        def reset_asset(key: str):
            selected_assets[key] = None
            set_preview(key)

        def open_asset_picker(key: str):
            assets = asset_lists.get(key, [])
            if not assets:
                try:
                    self.show_loading("Fetching SteamGridDB images...")
                    _, lists = fetch_asset_lists(self.sgdb, name_var.get().strip())
                    asset_lists.clear(); asset_lists.update(lists)
                    self.hide_loading()
                    assets = asset_lists.get(key, [])
                except Exception as exc:
                    self.hide_loading()
                    messagebox.showerror("SteamGridDB", str(exc), parent=modal)
                    return
            if not assets:
                messagebox.showinfo("No images", f"No {get_asset_display_name(key)} images found.", parent=modal)
                return

            picker = ctk.CTkToplevel(modal)
            picker.title(f"Choose {get_asset_display_name(key)}")
            picker.configure(fg_color=UIColors.SURFACE)
            picker.minsize(820, 620)
            center_window(picker, self.root, 840, 640)
            picker.transient(modal)
            picker.grab_set()
            picker.grid_rowconfigure(1, weight=1)
            picker.grid_columnconfigure(0, weight=1)

            header = ctk.CTkFrame(picker, fg_color=UIColors.SURFACE, corner_radius=0)
            header.grid(row=0, column=0, sticky="ew", padx=22, pady=(20, 10))
            ctk.CTkLabel(header, text=f"Choose {get_asset_display_name(key)}", text_color=UIColors.TEXT, font=("Segoe UI", 20, "bold")).pack(side="left")

            grid = ctk.CTkScrollableFrame(picker, fg_color=UIColors.SURFACE, corner_radius=0)
            grid.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 10))
            selected_index = tk.IntVar(value=-1)
            picker._thumbs = []
            rendered_count = {"value": 0}
            card_refs = []
            loading_label = ctk.CTkLabel(grid, text="Loading images...", text_color=UIColors.MUTED, font=("Segoe UI", 14, "bold"))
            loading_label.grid(row=0, column=0, padx=20, pady=40, sticky="nsew")
            picker.update_idletasks()
            picker.lift()
            picker.update()

            def render_items():
                for w in grid.winfo_children():
                    w.destroy()
                card_refs.clear()
                shown = min(rendered_count["value"], len(assets))
                cols = 5 if key in ("cover", "client_icon", "icon", "logo") else 3
                thumb_size = {
                    "cover": (120, 180),
                    "background": (200, 70),
                    "wide_cover": (200, 94),
                    "logo": (200, 70),
                    "client_icon": (100, 100),
                    "icon": (100, 100),
                }.get(key, (160, 120))
                for idx in range(shown):
                    asset = assets[idx]
                    card = ctk.CTkFrame(grid, fg_color=UIColors.CARD, corner_radius=10, border_width=1, border_color=UIColors.CARD)
                    card.grid(row=idx // cols, column=idx % cols, padx=8, pady=8, sticky="n")
                    img = make_ctk_image_from_url(asset.get("url"), thumb_size, allow_small=True)
                    picker._thumbs.append(img)
                    label = ctk.CTkLabel(card, image=img, text="" if img else "No preview", width=thumb_size[0], height=thumb_size[1], fg_color=UIColors.FIELD, corner_radius=8, text_color=UIColors.MUTED)
                    label.pack(padx=8, pady=(8, 4))
                    ctk.CTkLabel(card, text=f"{idx + 1}. {asset.get('width', '?')}x{asset.get('height', '?')}", text_color=UIColors.TEXT, font=("Segoe UI", 10, "bold")).pack(padx=8)
                    ctk.CTkLabel(card, text=str(asset.get("style", ""))[:22], text_color=UIColors.MUTED, font=("Segoe UI", 9)).pack(padx=8, pady=(0, 8))
                    card_refs.append((idx, card))

                    def choose(_e=None, i=idx):
                        selected_index.set(i)
                        for j, c in card_refs:
                            c.configure(border_color=UIColors.ACCENT if j == i else UIColors.CARD, border_width=2 if j == i else 1)
                    card.bind("<Button-1>", choose)
                    label.bind("<Button-1>", choose)

            rendered_count["value"] = min(10, len(assets))

            bottom = ctk.CTkFrame(picker, fg_color=UIColors.SURFACE, corner_radius=0)
            bottom.grid(row=2, column=0, sticky="ew", padx=22, pady=(6, 20))
            bottom.grid_columnconfigure(0, weight=1)

            def load_more():
                rendered_count["value"] = min(rendered_count["value"] + 10, len(assets))
                render_items()
                if rendered_count["value"] >= len(assets):
                    load_more_btn.configure(state="disabled")

            load_more_btn = ctk.CTkButton(bottom, text="Load more", width=140, height=38, fg_color=UIColors.CARD, hover_color=UIColors.CARD_HOVER, command=load_more)
            load_more_btn.grid(row=0, column=0)
            if rendered_count["value"] >= len(assets):
                load_more_btn.configure(state="disabled")

            def select_image():
                idx = selected_index.get()
                if idx < 0:
                    messagebox.showinfo("Select image", "Please select an image first.", parent=picker)
                    return
                selected_assets[key] = assets[idx]
                set_preview(key)
                picker.destroy()

            ctk.CTkButton(bottom, text="Cancel", width=110, height=38, fg_color=UIColors.CARD, hover_color=UIColors.CARD_HOVER, command=picker.destroy).grid(row=0, column=2, sticky="e")
            ctk.CTkButton(bottom, text="Select", width=110, height=38, fg_color=UIColors.ACCENT, hover_color=UIColors.ACCENT_HOVER, text_color="#000000", command=select_image).grid(row=0, column=1, sticky="e", padx=(0, 10))

            # Let the picker paint immediately before network image downloads begin.
            picker.after(80, render_items)

        for row, (key, label_text) in enumerate(asset_order):
            section = ctk.CTkFrame(body, fg_color=UIColors.SURFACE, corner_radius=0)
            section.grid(row=row, column=0, sticky="ew", pady=(0, 18))
            section.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(section, text=label_text, text_color=UIColors.TEXT, font=("Segoe UI", 13, "bold"), anchor="w").grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
            size = get_artwork_preview_size(key)
            preview = ctk.CTkLabel(section, text="No image found", width=size[0], height=size[1], fg_color=UIColors.CARD, corner_radius=8, text_color=UIColors.MUTED)
            preview.grid(row=1, column=0, sticky="w", padx=(0, 16))
            preview_labels[key] = preview
            info = ctk.CTkLabel(section, text="No image selected", text_color=UIColors.MUTED, anchor="w", justify="left", wraplength=360)
            info.grid(row=1, column=1, sticky="w")
            info_labels[key] = info
            btns = ctk.CTkFrame(section, fg_color=UIColors.SURFACE, corner_radius=0)
            btns.grid(row=1, column=2, sticky="ne")
            ctk.CTkButton(btns, text="Change", width=90, height=34, fg_color=UIColors.CARD, hover_color=UIColors.CARD_HOVER, command=lambda k=key: open_asset_picker(k)).pack(side="left", padx=(0, 6))
            ctk.CTkButton(btns, text="Reset", width=80, height=34, fg_color=UIColors.CARD, hover_color=UIColors.CARD_HOVER, command=lambda k=key: reset_asset(k)).pack(side="left")
            set_preview(key)

        bottom = ctk.CTkFrame(modal, fg_color=UIColors.SURFACE, corner_radius=0)
        bottom.grid(row=2, column=0, sticky="ew", padx=28, pady=(8, 24))
        bottom.grid_columnconfigure(0, weight=1)

        def refresh_assets():
            try:
                self.show_loading("Fetching SteamGridDB images...")
                self.set_progress(f"Searching for {name_var.get().strip()}...", 30)
                _, lists = fetch_asset_lists(self.sgdb, name_var.get().strip())
                asset_lists.clear(); asset_lists.update(lists)
                for key, _ in asset_order:
                    assets = asset_lists.get(key, [])
                    selected_assets[key] = assets[0] if assets else None
                    set_preview(key)
                self.hide_loading()
                self.set_progress("Artwork options loaded", 100)
            except Exception as exc:
                self.hide_loading()
                messagebox.showerror("SteamGridDB", str(exc), parent=modal)

        refresh_btn = ctk.CTkButton(bottom, text="↻", width=46, height=40, fg_color=UIColors.CARD, hover_color=UIColors.CARD_HOVER, command=refresh_assets)
        refresh_btn.grid(row=0, column=0, sticky="w")
        refresh_btn.bind("<Enter>", lambda _e: self.status_var.set("Fetch SteamGridDB images"))
        refresh_btn.bind("<Leave>", lambda _e: self.status_var.set("Ready"))

        def save_artwork():
            if not self.require_steam_closed():
                return
            try:
                new_name = name_var.get().strip() or game.name
                self.show_loading("Saving game artwork...")
                self.set_progress("Updating Steam shortcut name...", 20)
                if new_name != game.name:
                    update_shortcut_display_name(game.exe, game.appid, new_name)
                    game.name = new_name
                    game.search_term = new_name
                    modal.title(new_name)
                self.set_progress("Downloading and applying artwork...", 60)
                if any(selected_assets.values()):
                    apply_selected_assets_for_app(game.exe, game.appid, new_name, selected_assets)
                self.refresh_library_view(show_errors=False)
                self.hide_loading()
                self.set_progress("Artwork applied", 100)
                messagebox.showinfo("Saved", "Game name and artwork saved.", parent=modal)
            except Exception as exc:
                self.hide_loading()
                messagebox.showerror("Save failed", str(exc), parent=modal)

        ctk.CTkButton(bottom, text="Close", width=120, height=42, fg_color=UIColors.CARD, hover_color=UIColors.CARD_HOVER, command=modal.destroy).grid(row=0, column=3, sticky="e")
        ctk.CTkButton(bottom, text="Save", width=120, height=42, fg_color=UIColors.ACCENT, hover_color=UIColors.ACCENT_HOVER, text_color="#000000", command=save_artwork).grid(row=0, column=2, sticky="e", padx=(0, 10))
        refresh_assets()


# -----------------------------
# Main
# -----------------------------

def main() -> None:
    if os.name != "nt":
        print("This tool is for Windows only.")
        raise SystemExit(1)
    if not CTK_AVAILABLE:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Missing dependency", "CustomTkinter is required for SteamLaunchHelper v3.x.\n\nInstall it with:\npip install customtkinter")
        raise SystemExit(1)
    if not PIL_AVAILABLE:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Missing dependency", "Pillow is required to display game covers in SteamLaunchHelper v3.x.\n\nInstall it with:\npip install pillow")
        raise SystemExit(1)
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    try:
        icon_path = resource_path("steamlaunchhelper.ico")
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
            try:
                root.wm_iconbitmap(icon_path)
            except Exception:
                pass
    except Exception:
        pass
    app = SteamLaunchHelperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
