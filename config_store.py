"""Thread-safe, atomic access to config.json for Hermes Controller."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path

import shutil
import sys

FROZEN = bool(getattr(sys, "frozen", False))
# Code/bundle dir. Frozen (PyInstaller one-folder): read-only files live in sys._MEIPASS (…\_internal).
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# Writable data dir. Installed under Program Files the bundle is read-only, so config + logs live in
# %APPDATA%\HermesController. From source they stay next to the code. HERMES_CONTROLLER_HOME overrides both.
if os.environ.get("HERMES_CONTROLLER_HOME"):
    DATA_DIR = Path(os.environ["HERMES_CONTROLLER_HOME"])
elif FROZEN:
    DATA_DIR = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "HermesController"
else:
    DATA_DIR = APP_DIR
CONFIG_PATH = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"
STATE_PATH = DATA_DIR / "state.json"          # small runtime memory (e.g. last cold-load duration)
BUNDLED_CONFIG = APP_DIR / "config.default.json"


def ensure_data_dir() -> None:
    """Create DATA_DIR and, on first run of an installed build, seed config.json from the bundled default."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists() and BUNDLED_CONFIG.exists():
        shutil.copyfile(BUNDLED_CONFIG, CONFIG_PATH)


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(update: dict) -> None:
    """Merge `update` into state.json (best effort; never raises)."""
    try:
        state = load_state()
        state.update(update)
        save_config(state, STATE_PATH)
    except Exception:  # noqa: BLE001
        pass

DEFAULTS: dict = {
    "target_os": "windows",
    "backend": "strata",
    "model_name": "swift-1.5-iq3_s",
    "strata_dir": "",
    "launcher_bat": "",
    "launch_cmd": [],
    "endpoint_url": "http://127.0.0.1:8080/v1",
    "port": 8080,
    "ready_timeout_s": 180,
    "stop_timeout_s": 20,
    "min_ram_mb": 250,
    "min_vram_mb": 200,
    "min_game_ram_mb": 50,
    "game_exclude": ["wallpaper32.exe", "wallpaper64.exe"],
    "game_dirs": ["steamapps", "Epic Games", "Riot Games", "XboxGames", "GOG Galaxy", "Battle.net"],
    "user_whitelist": [],
}

_lock = threading.RLock()


def load_config(path: Path | str | None = None) -> dict:
    """Return config.json merged over DEFAULTS (a fresh deep copy; callers may mutate it)."""
    p = Path(path) if path else CONFIG_PATH
    with _lock:
        cfg = copy.deepcopy(DEFAULTS)
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            return cfg
        if not isinstance(data, dict):
            raise ValueError(f"{p} must contain a JSON object")
        cfg.update(data)
        return cfg


def save_config(cfg: dict, path: Path | str | None = None) -> None:
    """Atomically write cfg to config.json (temp file in the same dir + os.replace)."""
    p = Path(path) if path else CONFIG_PATH
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def server_root(endpoint_url: str) -> str:
    """'http://127.0.0.1:8080/v1' -> 'http://127.0.0.1:8080'."""
    url = endpoint_url.rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url
