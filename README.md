# Hermes Controller

A small always-handy Windows widget (360×260, dark) that turns the local **Strata** LLM endpoint on and off:

- **ON**: optionally closes RAM-hungry apps/games you tick, then starts Strata (`swift-1.5-iq3_s`) and waits
  until the model is loaded (`GET /health` → `loaded: true`).
- **OFF**: asks Strata to unload, stops its whole process tree, checks port 8080 is free, and shows the toast
  *"Hermes Endpoint Closed: <model> unloaded and memory freed."*

It also notices a Strata you started yourself (`run-*.bat`) and adopts it.

## Install

Run `dist\HermesController-Setup-1.0.0.exe`. It installs for your user only (no admin prompt) to
`%LOCALAPPDATA%\Programs\Hermes Controller`, adds a Start-menu shortcut (desktop shortcut optional) and an
uninstaller. The exe is unsigned, so Windows SmartScreen may ask once: *More info → Run anyway*.

Settings and logs live in `%APPDATA%\HermesController` (`config.json`, `logs\`, `state.json`); the first launch
copies the bundled default config there. Uninstalling keeps that folder.

From source instead: `run.bat` (or `.venv\Scripts\python.exe main.py` with a console). Then config and logs stay
in this folder. Only one instance can run either way.

## Build

```bat
build.bat
```
icon → PyInstaller one-folder `dist\HermesController\` → Inno Setup installer `dist\HermesController-Setup-1.0.0.exe`.
Needs the project `.venv` (with `pyinstaller`, `pillow`) and Inno Setup 6 (`winget install JRSoftware.InnoSetup`).
The installer bundles the current `config.json` as the default.

## The window

| Area | Shows |
|---|---|
| Status line | LED + state: Idle (grey) · Starting / Loading / Stopping (amber) · Serving (green) · Error (red); elapsed timer while starting |
| ⚙ | Protected apps (whitelist) — apps that are never offered for closing |
| Detail | Idle: what ON would close (refreshed every 5 s). Starting: load phase from Strata's log (*Reading weights → Loading experts into RAM → Filling GPU cache → Almost ready*) with a progress bar and "usually about m:ss" from the last cold load. Serving: model id and endpoint |
| Button | **Turn on** · **Cancel** (while starting) · **Turn off** |
| Memory | Bar + legend of physical RAM: other apps · Strata · free; "Freed" total for the session |

## Closing apps (the sweep)

Nothing is closed automatically. Pressing **ON** opens a list of candidates; **only games are pre-ticked**.
Choose *Close selected & start*, *Start without closing*, or *Cancel*. Closing sends the window a normal close
first, waits up to 3 s, then force-closes what is left — **unsaved work in those apps is lost**.

Candidates are apps that are not protected and match one of:

| Category | Rule | Pre-ticked |
|---|---|---|
| game | exe under `steamapps`, `Epic Games`, `Riot Games`, `XboxGames`, `GOG Galaxy`, `Battle.net` and ≥ 50 MB | yes |
| gpu | > 200 MB dedicated VRAM (Windows GPU counters) | no |
| window | has a real visible window and > 250 MB RAM | no |
| background | no window, > 1 GB RAM (tray apps) | no |

Never offered (built in, cannot be unticked): Windows/system processes and anything under `C:\Windows`,
terminals/shells/IDEs, `python`/`node`/`claude`/`hermes`, Strata itself, FanControl, LibreHardwareMonitor,
Tailscale, this app and the program that launched it. Your editable whitelist (`config.json` →
`user_whitelist`, default includes Brave, Discord, FanControl, OpenRGB, mVolt+, NVIDIA App/Overlay, Obsidian,
VIA, Moonlight, Hermes) protects those apps and everything they started.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `launch_cmd`, `strata_dir` | How Strata is started (the body of `run-swift-iq3_s.bat` without `--open`) |
| `endpoint_url`, `port` | `http://127.0.0.1:8080/v1`, 8080 |
| `ready_timeout_s` / `stop_timeout_s` | 180 / 20 |
| `min_ram_mb`, `min_vram_mb`, `min_game_ram_mb` | 250 / 200 / 50 |
| `game_dirs`, `game_exclude` | Game folders; exe names never treated as games (Wallpaper Engine) |
| `user_whitelist` | Edited by the ⚙ dialog |

To serve another Strata model, point `launch_cmd` at that model's `strata-*.json` (copy the line from its
`run-*.bat`) and set `model_name`.

## Known limitations

- **Launchers count as games.** Epic Games Launcher, Riot Client and Battle.net live in game folders, so they are
  pre-ticked. Untick them or add them to the whitelist.
- **VRAM per app is an estimate.** NVIDIA's NVML reports nothing per process on Windows; the app uses Windows' GPU
  memory counters, capped at the card's real usage. *VRAM freed* after a sweep is measured on the whole card.
- **Stopping during a reply**: if the model is mid-answer, OFF asks before force-stopping. Stopping Strata ends any
  Hermes session using the `local` model.
- **First scan** after launch takes ~2 s (later ~0.3 s); it runs in the background.
- Windows only.

## Tests

Run from this folder with `.venv\Scripts\python.exe`:

| Script | What it covers |
|---|---|
| `tests\test_detector.py` | Real-system scan + dry run (asserts nothing protected is ever listed), synthetic graph rules, kill test on its own throwaway process only |
| `tests\test_server_manager.py` | Fake Strata on a random port: start/ready/timeout/lazy-load/adopt/busy+force/cancel/foreign-listener/launcher topology |
| `tests\test_ui_smoke.py` | UI with stub backends, every state, screenshots `tests\ui_*.png` |
| `tests\test_identity.py` | Strict Strata identity for the stop path (a process that merely *mentions* `serve\server.py` is never killed) |
| `tests\test_load_progress.py` | Strata log → phase/progress (incl. the newest real log) |
| `tests\run_real_idle.py` | Real backends, IDLE only, no clicks |
| `tests\run_real_onoff.py` | **Real** Strata ON → OFF (only with your OK; closes no apps) |

None of the tests touch the real Strata, port 8080, or any app they did not start.

Design and contracts: `SPEC.md` (v1 and the advisor review history in `docs\`).
