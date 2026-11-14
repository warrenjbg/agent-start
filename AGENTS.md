# Working on Agent Start

For anyone changing this code, person or model. Read this before the first edit. `README.md` says what the app
does; `SPEC.md` holds the interface contracts; this file holds the rules that keep a change from hurting the PC it
runs on.

Agent Start is a Windows 11 tray app (Python, CustomTkinter). It starts and stops a local model server (Strata or
llama.cpp), can close apps to free memory, and updates itself. It runs on the same PC as the user's work, so a bug
here can end the wrong process or install the wrong file.

## Rules that are never broken

1. **Never contact a running engine from a test, a tool or a script.** No request to the engine's port
   (`127.0.0.1:8080`, `127.0.0.1:9931` or whatever the config names). Tests use the fake servers in `tests/` on
   spare ports. Whether an engine runs is read from the process table only.
2. **Never start, stop or kill the user's engine or apps.** Before running any suite, check that no `strata.exe`
   or `llama-server.exe` is running (`tasklist /FI "IMAGENAME eq llama-server.exe" /NH`). If one is, ask first: the
   installed app watches every process on the PC and can react to what a test starts.
3. **Tests never touch the real data folder.** Every suite sets `AGENT_START_HOME` to a temporary folder before
   it imports an app module. Never read or write `%APPDATA%\AgentStart` from a test.
4. **A process is only ended when its identity is proven.** Never "whatever listens on the port", never by name
   alone, never `taskkill`, `os.kill` or `/T`. Targets are (pid, create time) pairs from the scan the user saw.
5. **A file is only deleted or overwritten when the app made it, or when the user picked that one file and
   confirmed.** In folders the user also uses (a models folder, Downloads) the app marks its own partial files
   first; a file the user picked and confirmed is deleted through `model_files.delete` and nowhere else.
6. **Text from files, the network or window titles never becomes part of a command.** Process arguments are
   lists, never a shell string; a launcher `.bat` is parsed, never run.
7. **Private keys and secrets are never read or printed.** That covers the release keys, the `api_key` in a
   config file and anything in `.env` files. A model never types or receives a passphrase.
8. **Nothing is installed, pushed, published or signed without the owner saying so.** No `pip install` either:
   the dependencies are pinned in `requirements.txt` and `requirements-dev.txt`.

## Files that need a stronger reviewer

A change to one of these is read line by line by a second, stronger reviewer before it is merged, whoever wrote it.
`tools\protected_changes.py` lists the ones a branch touches.

| File | Why |
|---|---|
| `detector.py` | decides which processes may be closed, and closes them |
| `server_manager.py` | starts the engine and decides which process is the engine before stopping it |
| `engines/*.py` | each engine's identity check for the stop path, and its launch plan |
| `launcher_bat.py` | reads a launcher file the user picked; it must be parsed, never run |
| `reopen.py` | starts apps again after a turn off |
| `mem_watch.py` | acts by itself when memory runs low |
| `game_watch.py` | acts by itself when a game starts or closes |
| `idle_watch.py` | turns the engine off by itself |
| `watch_controller.py` | carries out what the watchers decide |
| `updater.py` | downloads and starts an installer |
| `update_sig.py` | decides whether a release is signed by the publisher |
| `update_controller.py` | drives the in-app update |
| `hf_download.py` | writes, resumes and deletes files in a folder the user also uses |
| `model_files.py` | deletes model files the user picked |
| `hermes_alias.py` | the one place that runs hermes.exe and changes its configuration |
| `toasts.py` | passes text to PowerShell |
| `tools/sign_release.py` | signs a release |
| `tools/check_release_keys.py` | the build's guard against shipping without release keys |
| `build.bat` | builds what users install |
| `installer/*.iss` | what the installer does on a user's PC |
| `.github/workflows/*` | what runs on every push |

If your task needs a change in one of them, make it, say so in your report, and do not merge it yourself.

## Commands

Run everything from the project folder with the project's own interpreter.

```bat
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe tools\run_tests.py --ci
.venv\Scripts\python.exe tools\run_tests.py --advisory
.venv\Scripts\python.exe tools\run_tests.py test_stats test_updater
.venv\Scripts\python.exe tools\run_hidden.py
.venv\Scripts\python.exe tools\run_hidden.py test_history_dialog
.venv\Scripts\python.exe tools\protected_changes.py
```

- `run_tests.py --ci` runs the suites that need no window; set `AGENTSTART_TEST_OFFLINE=1` and
  `AGENTSTART_TEST_NO_INNO=1` first, as CI does.
- `run_hidden.py` runs the window suites on a separate, hidden Windows desktop. Always use it for a window suite:
  run directly, those suites open real windows and take the keyboard.
- Each `tests\test_*.py` is a plain script that prints `[PASS]` / `[FAIL]` lines and exits 0 when all passed. There
  is no pytest.
- A change is done when ruff and mypy are clean, the headless suites pass, every window suite that covers a window
  you touched passes, and you have looked at the saved pictures (`tests\ui_hidden_*.png`) of those windows.

## How to change things

- **One thing per commit,** with a message that says what changed for the user.
- **Find the module first.** Most features live in their own file with their own suite (`idle_watch.py` and
  `tests\test_idle_watch.py`, `hf_download.py` and `tests\test_hf_download.py`, …). Open that pair, not `main.py`.
- **The app window is one class in several files.** Open the one your change belongs to:

  | File | What it holds |
  |---|---|
  | `main.py` | `AgentStartApp`: start-up, the on/off state machine, safe stop, model and engine switch, reopen, Settings wiring, exit. The hub: change it only for that logic or for wiring, read the methods around your change, and run the window suites afterwards |
  | `main_view.py` | `AppView`, a mixin of the app class: building and redrawing the window (layout, status and detail line, stats panels, pins) and the drawing constants |
  | `main_tray.py` | `AppTray`, a mixin of the app class: the app's side of the tray icon (`tray.py` is the icon itself) |
  | `app_workers.py` | functions the app runs on worker threads, and small pure helpers; no Tk |
  | `metrics_worker.py` | `MetricsWorker`: the polling thread (status, usage history, GPU energy, live GPU-fit check) |
  | `alias_controller.py` | `AliasController`: keeping Hermes' alias in step |
  | `watch_controller.py`, `update_controller.py` | what the watchers decide; the in-app update |
  | `app_consts.py`, `text_util.py` | constants and text helpers the files above share; no app imports |

  The mixins use the app's attributes freely: an attribute they read is created in `AgentStartApp.__init__` or
  in `_build_ui`. A controller (`XxxController` + `XxxHost`) reads the app only through its host, and `main.py`
  wires each host callable to look the app's method up when it is called, because tests swap app methods for
  recorders after the app is built.
- **A constant lives in the module whose code reads it.** `main.py` still offers the moved names
  (`main.LIVE_POLL_S`, `main.STATUS`, …) for reading, but they are copies there: to change one in a test, set it on
  its home module (`metrics_worker.LIVE_POLL_S = 0.2`). `tests\test_split_layout.py` fails when a test sets one on
  `main`.
- **None of these modules may import `main`.** Run as a script, `main.py` is `__main__`; `import main` would load a
  second copy of the app. Shared values go to `app_consts.py`.
- **A test that fails is information.** Fix the code, or, when the behaviour was changed on purpose, change the
  expectation and say so. Never loosen a check to make it pass, and never delete one.
- **A new suite that passes on its first run proves little.** Break the code in one line and watch a check fail.
- **Anything that can start a process gets its test switch in the same change:** an attribute that is off for the
  stub-built app the tests use, and a recorder in place of the real call.
- **Tk is single-threaded.** No Tk call from a worker thread: workers hand results to the window with
  `self.post(...)`. Never close a window in the first 0.4 s after it opened.
- **Keep each file's line endings** (most are CRLF) and stay within 120 columns.
- **Do not add a dependency.** Ask.
- **Version:** `version.py` is the only place; `tools\sync_version.py` writes the rest. Do not change it unless
  asked.

## What to put in your report

What changed and why; the exact commands you ran with their results; what you did not run; every protected file
you touched; anything you were unsure about. A report that says "all tests pass" without the command output is
not accepted.
