# Hermes Controller

A small always-handy Windows widget (360×268, dark; v1.1.0) that turns the local **Strata** LLM endpoint on and off:

- **ON**: optionally closes RAM-hungry apps/games you tick, then starts Strata with the launcher you chose
  (default `run-swift-iq3_s.bat` → `swift-1.5-iq3_s`) and waits until the model is loaded
  (`GET /health` → `loaded: true`).
- **OFF**: asks Strata to unload, stops its whole process tree, checks port 8080 is free, and shows the toast
  *"Hermes Endpoint Closed: <model> unloaded and memory freed."*

It also notices a Strata you started yourself (`run-*.bat`) and adopts it.

## Install

Run `dist\HermesController-Setup-<version>.exe` (currently 1.1.0). It installs for your user only (no admin prompt) to
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
version files → icon → PyInstaller one-folder `dist\HermesController\` → Inno Setup installer
`dist\HermesController-Setup-<version>.exe`.
Needs the project `.venv` (`pip install -r requirements.txt`) and Inno Setup 6 (`winget install JRSoftware.InnoSetup`).
The installer bundles the current `config.json` as the default.

**Version**: `version.py` is the only place to bump. `build.bat` first runs `tools\sync_version.py`, which
regenerates `installer\version_info.txt` (exe properties) and `installer\version.iss` (included by the Inno
script); the Settings dialog and the log show the same number.

## The window

| Area | Shows |
|---|---|
| Status line | LED + state + a short summary: Idle (grey) *2 apps to close · 6.8 GB* · Starting (amber) *Loading experts into RAM* + timer · Serving (green) *model* · Low-RAM (amber) · Error (red) *what failed*. While Strata works on a request it shows the model state, like Strata's Monitor: **Reading** (blue) *12% · 10,752 / 87,072* with a thin progress bar under the line, **Generating** (green) *412 tokens · 98.4 tok/s*, **Queued** (amber) *2 waiting*; then back to Serving. Read from `/metrics` once a second while serving (paused while minimised). Click it to show or hide the detail line (remembered) |
| ⚙ | **Settings**: Strata launcher, the Strata dashboard link + API address, Protected apps (see below). Disabled while starting/stopping |
| Detail line | Optional: idle = which apps would be closed; serving = *Up 12 min · last reply 85 tok/s*. Always shown when it matters: loading (phase, *usually 0:58*, progress bar), Low-RAM mode, errors (click opens the log), notes such as *Stopped · freed 47 GB*, and the one-click *Reopen …* offer |
| Button | **Turn on** · **Cancel** (while starting) · **Turn off** |
| Panels | **Memory** (bar + legend of physical RAM: other apps · Strata · free; "Freed" total for the session), **VRAM**, **Decode**, **Prompt reading** (see Stats). Each has a pin (shown on hover; filled when pinned): pinned panels stay visible while collapsed, in this order. Default: Memory pinned. Nothing pinned = just the status line and the button |
| ⌄ bar (bottom) | Shows / hides the panels that are not pinned; the window grows downward. Hidden when every panel is pinned. Pins and the open/closed state are remembered |

## Settings (⚙)

- **Strata launcher** — a dropdown of every `run-*.bat` in the Strata folder (other `.bat` files such as
  `SETUP.bat` are not listed) plus **Browse…** for a `.bat` anywhere. The line below shows what that launcher
  runs: *model · config file · port*.
  The app only **reads** the `.bat` — it never runs it. It takes the line that starts
  `python … serve\server.py …` (honouring `cd /d`, `set VAR=`, `%~dp0`, `^` continuations), drops `--open` (no
  browser tab) and starts that command itself, so the log, the progress bar and OFF keep working. A `.bat` that
  does not start `serve\server.py`, or whose python/config file is missing, is rejected with the reason shown
  in the dialog.
  **Save** writes the choice to `config.json`. If Strata is running, nothing is restarted: the main window shows
  *"run-….bat applies next start"* and the new launcher is used the next time you turn it on.
- **Endpoint** — **Open Strata dashboard** opens Strata's own web app (`http://127.0.0.1:<port>/`: chat, Monitor,
  its settings) in your browser; greyed out with *not running* while Strata is off. Below it the API address
  (`…/v1`) with **Copy**, for pasting into Hermes or other clients.
- **Protected apps…** — the whitelist: apps that are never offered for closing.

## Stats section

Three compact graphs (drawn by the app itself, no extra libraries). Whether the section is open is remembered.

| Graph | Shows |
|---|---|
| VRAM · 5 min | GPU memory used vs total over the last 5 minutes (whole card, sampled every 2 s via NVIDIA NVML). The stretch where Strata was running is green, the rest grey. "VRAM unavailable" without an NVIDIA driver |
| Decode · per request | Generation speed (tok/s) of the last 30 finished requests, newest right. While a reply is being generated its live speed is the green bar and the value marked *live*; otherwise the value is the *last* request |
| Prompt reading · per request | Prompt-reading speed (tokens read ÷ reading time) of the last 30 requests; live while a prompt is being read. Requests answered from the prompt cache (fewer than 64 tokens read) are left out so they do not distort the scale |

The two speed graphs come from Strata's own `GET /metrics`, polled every 2 s **only** while Strata is serving and
the section is open; the label on the left of each graph is its scale maximum. Strata reports just its 12 newest
requests per poll, so the bars build up while the section is open; they stay after OFF and reset at the next ON.

## Closing apps (the sweep)

Nothing is closed automatically. Pressing **ON** opens a list of candidates; **games are pre-ticked**, plus
whatever the full-speed RAM check below adds. Choose *Close selected & start*, *Start without closing*, or *Cancel*. Closing sends the window a normal close
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

### Full-speed RAM check

Strata is fast only when its whole **expert arena** fits in RAM (46.84 GiB for swift-iq3_s, 39.97 GiB for
swift-iq3_xxs). With less available it waits 20 s, then falls back to **Low-RAM mode**: experts are read from the
SSD and decode drops from ~90–100 to ~15–20 tok/s. The arena size is learned from Strata's engine log (the `"log"`
file named in the launcher's `--config` JSON), so the check works per model and only after that model has loaded
once.

- **Start dialog:** "Full speed needs ~50 GB free · you have 40 GB". If closing apps can close the gap, the fewest
  apps are pre-ticked (largest first; with similar sizes, background/GPU apps before ones with a window; protected
  apps never). A live line updates as you tick: *Ticked apps free ~X GB → full speed* or *Still ~Y GB short →
  Low-RAM mode (slower)*. If even closing everything is not enough, nothing extra is ticked and it says so.
- **While loading:** the engine log is read alongside the progress: *Waiting for RAM · needs 47 GB, 40 GB free*, then
  *Low-RAM mode · reading experts from SSD · slower* in amber if it falls back.
- **While serving:** the status reads **Serving · Low-RAM mode** with an amber LED and the shortfall, e.g.
  *Low-RAM mode: reading from SSD (slower) · ~7.3 GB short*. Turn off, close apps, turn on again for full speed.

Target = arena + 2.5 GiB headroom, measured against Windows *available* RAM (calibrated on real launches: ~49.6 GiB
after a sweep still reached full RAM, after Strata waited ~13 s for the freed memory). Freed RAM per app is its working set,
so the numbers are estimates (shown with "~"). Code: `engine_log.py`, `ram_plan.py`.

### Reopening closed apps

The start dialog has **Reopen these when Strata stops** (ticked by default, remembered). Before the sweep closes an
app it records how that app was started (program, arguments, folder; Store apps by their app ID). After **Turn off**
releases the memory, those apps are started again one at a time, a second apart: *Reopening apps 1/3 · Spotify
started*, then *Reopened 3 apps*.

- **Games are not restarted automatically** (launchers and anti-cheat expect to be started their own way). They stay
  as a one-click **Reopen eldenring** line on the idle screen; it clears when you turn Strata on again.
- **Skipped:** apps that ran as administrator, programs that no longer exist, Windows programs, and background Store
  apps that Windows restarts itself. Apps you already reopened by hand are not started twice.
- If Strata stopped some other way (crash, closed outside the app), the idle screen offers **Reopen N closed apps**.
- Apps restart fresh: unsaved work in them is still lost (browsers usually restore their tabs).
- Launching never goes through a shell: only the recorded `.exe` (checked to exist and to match the recorded command
  line) or `explorer.exe shell:AppsFolder\<app id>`. Code: `reopen.py`; records are captured in `detector.py`.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `launcher_bat` | Full path of the chosen launcher `.bat` — set by ⚙ Settings. (A v1.0 config with just a file name still works: it is looked up in `strata_dir`) |
| `launch_cmd`, `launch_cwd`, `strata_dir` | Derived from that `.bat` on Save: the command the app starts (without `--open`), the folder it starts it in, the Strata install folder |
| `endpoint_url`, `port`, `model_name` | Also derived: `http://127.0.0.1:<--port>/v1`, the port (8080 if the `.bat` has none), the model name from the launcher's `--config` json (else its file name) |
| `ready_timeout_s` / `stop_timeout_s` | 180 / 20 |
| `min_ram_mb`, `min_vram_mb`, `min_game_ram_mb` | 250 / 200 / 50 |
| `game_dirs`, `game_exclude` | Game folders; exe names never treated as games (Wallpaper Engine) |
| `user_whitelist` | Edited by the ⚙ dialog |

To serve another Strata model, pick its `run-*.bat` in ⚙ Settings — no hand-editing needed. `state.json` (next to
`config.json`) remembers the last cold-load time and whether the stats section is open.

## Known limitations

- **Launchers count as games.** Epic Games Launcher, Riot Client and Battle.net live in game folders, so they are
  pre-ticked. Untick them or add them to the whitelist.
- **VRAM per app is an estimate.** NVIDIA's NVML reports nothing per process on Windows; the app uses Windows' GPU
  memory counters, capped at the card's real usage. *VRAM freed* after a sweep is measured on the whole card.
- **Stopping during a reply**: if the model is mid-answer, OFF asks before force-stopping. Stopping Strata ends any
  Hermes session using the `local` model.
- **First scan** after launch takes ~2 s (later ~0.3 s); it runs in the background.
- **Launcher is parsed when you save it.** If you later edit that `.bat` (or Strata's updater rewrites it), open
  ⚙ Settings and press Save again to pick up the change.
- **Stats need Strata's monitor data.** If Strata is started with an API key, `/metrics` answers 401 and the speed
  graphs show "Metrics unavailable". VRAM is the whole card (all apps), NVIDIA only.
- **Only run-\*.bat launchers that call `python … serve\server.py` directly** are understood; a `.bat` that
  delegates to another script is rejected.
- Windows only.

## Tests

Run from this folder with `.venv\Scripts\python.exe`:

| Script | What it covers |
|---|---|
| `tests\test_detector.py` | Real-system scan + dry run (asserts nothing protected is ever listed), synthetic graph rules, kill test on its own throwaway process only |
| `tests\test_server_manager.py` | Fake Strata on a random port: start/ready/timeout/lazy-load/adopt/busy+force/cancel/foreign-listener/launcher topology/launch from a parsed `.bat` |
| `tests\test_ui_smoke.py` | UI with stub backends, every state, Settings dialog, stats section (100 % and 150 % scaling), screenshots `tests\ui_*.png` |
| `tests\test_launcher_bat.py` | The `.bat` parser: the five real `run-*.bat` (read only), continuations, variables, relative paths, rejections |
| `tests\test_stats.py` | `/metrics` accumulation and speed maths, fetch against the fake Strata, VRAM sampler with and without NVML, version files in sync |
| `tests\test_e2e_fake.py` | The real app + real manager/detector/NVML against a fake Strata started from a parsed `.bat`: ON → graphs fill → change launcher → OFF → ON |
| `tests\test_identity.py` | Strict Strata identity for the stop path (a process that merely *mentions* `serve\server.py` is never killed) |
| `tests\test_load_progress.py` | Strata log → phase/progress (incl. the newest real log) |
| `tests\run_real_idle.py` | Real backends, IDLE only, no clicks |
| `tests\run_real_onoff.py` | **Real** Strata ON → OFF (only with your OK; closes no apps) |

None of the tests touch the real Strata, port 8080, or any app they did not start.

Design and contracts: `SPEC.md` (v1 and the advisor review history in `docs\`).
