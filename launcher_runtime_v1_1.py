import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

CONFIG_FILENAME = "launcher_config.json"
LOG_FILENAME = "launcher.log"
DEFAULT_PROCESS_DETECT_TIMEOUT_SECONDS = 600
DEFAULT_PROCESS_MISSING_GRACE_SECONDS = 4
DEFAULT_WINDOW_MISSING_GRACE_SECONDS = 8
DEFAULT_STARTUP_GRACE_SECONDS = 3
DEFAULT_POLL_SECONDS = 1


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
CONFIG_PATH = os.path.join(APP_DIR, CONFIG_FILENAME)
LOG_PATH = os.path.join(APP_DIR, LOG_FILENAME)


def log(message: str) -> None:
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} | {message}\n")
    except Exception:
        pass


def load_config() -> Dict[str, Any]:
    if not os.path.isfile(CONFIG_PATH):
        raise FileNotFoundError(f"Missing config file: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    required = ["mode", "launch_target", "game_exe_name", "start_dir"]
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise ValueError("Missing config fields: " + ", ".join(missing))

    return config


def process_stem(exe_name: str) -> str:
    exe_name = exe_name.strip()
    if exe_name.lower().endswith(".exe"):
        return exe_name[:-4]
    return exe_name


def run_hidden(cmd: List[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def get_process_snapshot(exe_name: str) -> Tuple[bool, bool, List[int]]:
    """Return (is_running, has_main_window, pids) for an exact executable/process name.

    Uses PowerShell Get-Process so we can inspect MainWindowHandle. This is more reliable
    than loose `tasklist` substring checks and lets us exit when a GDK game leaves a
    background/suspended process without a game window.
    """
    stem = process_stem(exe_name)
    if not stem:
        return False, False, []

    ps_script = (
        "$name = " + repr(stem) + "; "
        "$items = @(Get-Process -Name $name -ErrorAction SilentlyContinue | "
        "ForEach-Object { [PSCustomObject]@{ Id=$_.Id; MainWindowHandle=$_.MainWindowHandle } }); "
        "if ($items.Count -eq 0) { Write-Output '[]' } "
        "else { $items | ConvertTo-Json -Compress }"
    )

    try:
        result = run_hidden(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], timeout=10)
        if result.returncode != 0:
            log(f"PowerShell process check failed for {exe_name}: {result.stderr.strip()}")
            return fallback_tasklist_snapshot(exe_name)

        raw = result.stdout.strip()
        if not raw or raw == "[]":
            return False, False, []

        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = [parsed]

        pids: List[int] = []
        has_window = False
        for item in parsed:
            try:
                pids.append(int(item.get("Id", 0)))
                if int(item.get("MainWindowHandle", 0)) != 0:
                    has_window = True
            except Exception:
                continue

        return bool(pids), has_window, pids
    except Exception as exc:
        log(f"PowerShell process check exception for {exe_name}: {exc}")
        return fallback_tasklist_snapshot(exe_name)


def fallback_tasklist_snapshot(exe_name: str) -> Tuple[bool, bool, List[int]]:
    """Exact-name fallback. Window state is unknown, so has_window follows running."""
    exe_name = exe_name.strip()
    try:
        cmd = f'tasklist /FI "IMAGENAME eq {exe_name}" /NH /FO CSV'
        output = subprocess.check_output(
            cmd,
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        running = False
        pids: List[int] = []
        for line in output.splitlines():
            columns = [x.strip().strip('"') for x in line.split(',')]
            if len(columns) >= 2 and columns[0].lower() == exe_name.lower():
                running = True
                try:
                    pids.append(int(columns[1]))
                except Exception:
                    pass
        return running, running, pids
    except Exception as exc:
        log(f"Fallback tasklist check failed for {exe_name}: {exc}")
        return False, False, []


def launch_game(config: Dict[str, Any]) -> None:
    mode = str(config["mode"]).strip()
    target = str(config["launch_target"]).strip()
    start_dir = str(config.get("start_dir", "")).strip()

    cwd = start_dir if start_dir and os.path.isdir(start_dir) else None
    log(f"Launching mode={mode} target={target} cwd={cwd}")

    if mode in ("gamepass", "epic_uri"):
        os.startfile(target)  # type: ignore[attr-defined]
        return

    if mode == "direct_exe":
        subprocess.Popen(
            [target],
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return

    raise ValueError(f"Unknown launch mode: {mode}")


def wait_for_game_to_appear(game_exe: str, timeout_seconds: int) -> bool:
    log(f"Waiting for process to appear: {game_exe}")
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        running, has_window, pids = get_process_snapshot(game_exe)
        if running:
            log(f"Detected process: {game_exe} pids={pids} has_window={has_window}")
            return True
        time.sleep(1)

    log(f"Timed out waiting for process: {game_exe}")
    return False


def wait_for_game_to_exit(
    game_exe: str,
    process_missing_grace_seconds: int,
    window_missing_grace_seconds: int,
    startup_grace_seconds: int,
    poll_seconds: int,
) -> None:
    log(
        "Monitoring process: "
        f"{game_exe} process_missing_grace={process_missing_grace_seconds}s "
        f"window_missing_grace={window_missing_grace_seconds}s poll={poll_seconds}s"
    )

    time.sleep(max(0, startup_grace_seconds))

    last_process_seen = time.time()
    last_window_seen = time.time()
    ever_saw_window = False

    while True:
        running, has_window, pids = get_process_snapshot(game_exe)
        now = time.time()

        if running:
            last_process_seen = now
            if has_window:
                last_window_seen = now
                ever_saw_window = True
        else:
            missing_for = now - last_process_seen
            if missing_for >= process_missing_grace_seconds:
                log(f"Process gone for {missing_for:.1f}s; exiting helper: {game_exe}")
                return

        # GDK/Epic games can leave a background process after the visible game is closed.
        # Only use this rule after we have seen a real window once, so loading screens
        # or headless startup do not cause an early exit.
        if running and ever_saw_window and not has_window:
            window_missing_for = now - last_window_seen
            if window_missing_for >= window_missing_grace_seconds:
                log(
                    f"No main window for {window_missing_for:.1f}s while process still exists; "
                    f"exiting helper: {game_exe} pids={pids}"
                )
                return

        time.sleep(max(1, poll_seconds))


def main() -> int:
    log("Launcher v1.1 started")

    try:
        config = load_config()
    except Exception as exc:
        log(f"Config error: {exc}")
        time.sleep(30)
        return 1

    game_exe = str(config["game_exe_name"]).strip()
    detect_timeout = int(config.get("process_detect_timeout_seconds", DEFAULT_PROCESS_DETECT_TIMEOUT_SECONDS))
    process_missing_grace = int(
        config.get(
            "process_missing_grace_seconds",
            config.get("exit_grace_seconds", DEFAULT_PROCESS_MISSING_GRACE_SECONDS),
        )
    )
    window_missing_grace = int(config.get("window_missing_grace_seconds", DEFAULT_WINDOW_MISSING_GRACE_SECONDS))
    startup_grace = int(config.get("startup_grace_seconds", DEFAULT_STARTUP_GRACE_SECONDS))
    poll_seconds = int(config.get("poll_seconds", DEFAULT_POLL_SECONDS))

    try:
        launch_game(config)
    except Exception as exc:
        log(f"Launch failed: {exc}")
        time.sleep(30)
        return 2

    if not wait_for_game_to_appear(game_exe, detect_timeout):
        time.sleep(10)
        return 3

    wait_for_game_to_exit(
        game_exe,
        process_missing_grace,
        window_missing_grace,
        startup_grace,
        poll_seconds,
    )
    log("Launcher exited normally")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
