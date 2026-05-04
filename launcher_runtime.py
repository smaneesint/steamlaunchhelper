import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict

CONFIG_FILENAME = "launcher_config.json"
LOG_FILENAME = "launcher.log"
DEFAULT_PROCESS_DETECT_TIMEOUT_SECONDS = 600
DEFAULT_EXIT_GRACE_SECONDS = 4
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


def is_process_running(exe_name: str) -> bool:
    exe_name = exe_name.strip()
    if not exe_name:
        return False

    try:
        cmd = f'tasklist /FI "IMAGENAME eq {exe_name}" /NH'
        output = subprocess.check_output(
            cmd,
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first_column_names = []
        for line in output.splitlines():
            parts = line.split()
            if parts:
                first_column_names.append(parts[0].lower())
        return exe_name.lower() in first_column_names
    except Exception as exc:
        log(f"Process check failed for {exe_name}: {exc}")
        return False


def launch_game(config: Dict[str, Any]) -> None:
    mode = str(config["mode"]).strip()
    target = str(config["launch_target"]).strip()
    start_dir = str(config.get("start_dir", "")).strip()

    cwd = start_dir if start_dir and os.path.isdir(start_dir) else None
    log(f"Launching mode={mode} target={target} cwd={cwd}")

    if mode in ("gamepass", "epic_uri"):
        # Use Windows shell URL/file association. This is the correct path for
        # shell:AppsFolder and Epic URI launches.
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
        if is_process_running(game_exe):
            log(f"Detected process: {game_exe}")
            return True
        time.sleep(1)

    log(f"Timed out waiting for process: {game_exe}")
    return False


def wait_for_game_to_exit(game_exe: str, exit_grace_seconds: int, poll_seconds: int) -> None:
    log(f"Monitoring process: {game_exe}")
    last_seen = time.time()

    while True:
        if is_process_running(game_exe):
            last_seen = time.time()
        else:
            missing_for = time.time() - last_seen
            if missing_for >= exit_grace_seconds:
                log(f"Process gone for {missing_for:.1f}s; exiting helper: {game_exe}")
                return
        time.sleep(max(1, poll_seconds))


def main() -> int:
    log("Launcher started")

    try:
        config = load_config()
    except Exception as exc:
        log(f"Config error: {exc}")
        time.sleep(30)
        return 1

    game_exe = str(config["game_exe_name"]).strip()
    detect_timeout = int(config.get("process_detect_timeout_seconds", DEFAULT_PROCESS_DETECT_TIMEOUT_SECONDS))
    exit_grace = int(config.get("exit_grace_seconds", DEFAULT_EXIT_GRACE_SECONDS))
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

    wait_for_game_to_exit(game_exe, exit_grace, poll_seconds)
    log("Launcher exited normally")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
