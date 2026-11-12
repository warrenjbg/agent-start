# Agent Start — Spec & Interface Contracts (v1, locked after advisor review)

Windows 11 desktop widget (Python 3.11+, tested on 3.14, CustomTkinter, dark mode, 360x260) that:
1. **ON**: optionally closes RAM-hungry apps/games (user-confirmed), then starts the local **Strata** inference
   server (`swift-1.5-iq3_s`, OpenAI-compatible on `http://127.0.0.1:8080/v1`) and waits until it is ready.
2. **OFF**: unloads the model, kills the Strata process tree, verifies port 8080 is released, shows a Windows toast.

Host facts: Ryzen 9800X3D, 64 GB RAM, RTX 5070 Ti (WDDM). RAM is the binding constraint for Strata (the IQ3_S
model streams ~70 GB; it wants "little else open"); VRAM only changes speed. Only ONE Strata may run at a time.

Project dir: `C:\Users\<user>\Downloads\Agent Start` · venv `.venv` (customtkinter 6.0, psutil 7.2,
pywin32, nvidia-ml-py (`import pynvml`), winotify). No TODOs, no placeholders, no `pass` bodies in shipped code.

## Files & ownership

| File | Owner | Purpose |
|---|---|---|
| `config.json` | Phase 1 (done) | Settings; `user_whitelist` is edited by the UI |
| `config_store.py` | Phase 1 (done, fully implemented) | `load_config()`, `save_config(cfg)` — thread-safe, atomic |
| `detector.py` | Subagent 1 | `ProcessDetector` |
| `server_manager.py` | Subagent 2 | `EndpointManager` |
| `main.py`, `whitelist_modal.py`, `sweep_dialog.py` | Subagent 3 | UI |
| `requirements.txt`, `README.md`, `run.bat` | Phase 3 (integrator) | |

Subagents must NOT edit files they do not own. If a contract is insufficient, they say so in their final report
instead of changing another module.

## Strata facts (verified against Strata 0.2.10 `serve/server.py`)

- Launch command = `config["launch_cmd"]` (the body of `run-swift-iq3_s.bat` minus `--open`, which would open a
  browser tab). cwd = `config["strata_dir"]`. The python server spawns the engine child `engine\strata.exe`.
- `GET /health` → 200 `{"status":"ok","loaded":bool,"model":str,...,"service":"strata"}`.
  `GET /v1/models` → 200 `{"data":[{"id":...}]}`. Both live at the server root `http://127.0.0.1:8080`
  (derive root from `endpoint_url` by stripping a trailing `/v1`).
- `POST /unload` with header `Content-Type: application/json` and body `{}` (and NO `Origin` header) frees the
  model between requests → 200 `{"status":...}`, or 409 `{"status":"busy"}` if a turn is running. `?wait=<s>`
  waits up to s seconds for the running turn. Anything else → treat as "unload unavailable" and just kill.
- If port 8080 is already in use at startup, `server.py` exits with "port 8080 is already in use". So: never
  launch when the port is already listening; adopt the running server instead.
- Typical cold load: 30–90 s. `ready_timeout_s` = 180.
- Strata may have been started OUTSIDE this app (user double-clicked `run-*.bat`): then the listener PID is a
  `python.exe` whose cmdline contains `serve\server.py`, often under a `cmd.exe /c "...run-*.bat"` parent.

## Contract 1 — `detector.py` → `class ProcessDetector`

```python
class ProcessDetector:
    def __init__(self, config: dict | None = None, protected_pids_provider: Callable[[], set[int]] | None = None): ...
        # config: loaded via config_store.load_config() when None.
        # protected_pids_provider: called on every scan; returns extra PIDs that must never be touched
        #   (main.py passes EndpointManager.protected_pids). Exceptions from it are swallowed (treated as empty).
    def scan_killable_processes(self) -> list[dict]: ...
    def sweep_processes(self, dry_run: bool = False, pids: list[int] | None = None) -> dict: ...
    def get_whitelist(self) -> list[str]: ...
    def update_whitelist(self, apps: list[str]) -> None: ...
    def list_whitelist_candidates(self) -> list[dict]: ...
    def close(self) -> None: ...   # nvmlShutdown if initialised; idempotent
```

**scan_killable_processes()** returns a list sorted by `ram_mb` desc of dicts with EXACTLY these keys:
`pid:int, name:str, exe:str, create_time:float, ram_mb:float, vram_mb:float|None, reason:str,
category:"game"|"gpu"|"window", preselect:bool`.
- One entry per *app*, not per process: the candidate is the top-most ancestor in the same app family (same exe
  name), e.g. browser main process; `ram_mb` = RSS of that process + all recursive children (MB, 1 decimal).
- `vram_mb` = summed NVML usage for the tree, or `None` when NVML can't report it (normal on Windows WDDM —
  `usedGpuMemory` is None/NOT_AVAILABLE). Never report 0 when the true value is unknown.
- `preselect` = True only for category "game".
- `reason` is human-readable, e.g. `"game dir: steamapps"`, `"GPU 612 MB VRAM"`, `"window, 1.2 GB RAM"`.

Eligibility = NOT protected AND NOT whitelisted AND at least one of:
- **(a) window**: owns a *real* visible top-level window AND tree RAM > `min_ram_mb`. Real = `IsWindowVisible`,
  non-empty title, NOT `WS_EX_TOOLWINDOW`, no owner window (`GetWindow(h, GW_OWNER)==0`), NOT DWM-cloaked
  (`DwmGetWindowAttribute(h, DWMWA_CLOAKED=14)` via ctypes `dwmapi` ≠ 0 → skip), non-zero rect.
  (Without the cloak/toolwindow filters, background UWP hosts like PhoneExperienceHost/TextInputHost show up.)
- **(b) game**: exe path (case-insensitive) contains a `\<game_dir>\` segment from `config["game_dirs"]`
  — category "game", regardless of RAM/window.
- **(b) gpu**: NVML-reported tree VRAM > `min_vram_mb` — category "gpu".
Priority when several apply: game > gpu > window.

**Protection (2 tiers)** — a process is protected if ANY of:
- `SYSTEM_PROTECTED` (case-insensitive names): System, Idle, Registry, smss, csrss, wininit, winlogon, services,
  lsass, lsaiso, svchost, fontdrvhost, dwm, explorer, sihost, ctfmon, taskhostw, RuntimeBroker, dllhost,
  conhost, OpenConsole, ShellExperienceHost, StartMenuExperienceHost, SearchHost, SearchIndexer, TextInputHost,
  ApplicationFrameHost, SystemSettings, LockApp, SecurityHealthSystray, SecurityHealthService, MsMpEng,
  NisSrv, audiodg, spoolsv, WmiPrvSE, Taskmgr, WindowsTerminal, wt, OpenConsole, powershell, pwsh, cmd, bash,
  mintty, Code, Code - Insiders, cursor, devenv, claude, node, Hermes, hermes, python, pythonw, py, pyw,
  nvcontainer, NVDisplay.Container, nvsphelper64, (all `.exe`).
- PID is this process, any ANCESTOR of this process (walk `psutil.Process().parents()`), or any descendant of
  this process.
- PID in `protected_pids_provider()`; or exe path under `config["strata_dir"]` (Strata server + engine).
- Process session id differs from ours / owned by another user (AccessDenied on `exe()` ⇒ skip silently).
- Name (case-insensitive) in `user_whitelist`. Whitelisting a name protects its whole tree.
Children of a protected process that would be candidates only via the parent's window are not candidates.

**sweep_processes(dry_run=False, pids=None)** → `{"closed_apps": list[str], "ram_freed_mb": float,
"vram_freed_mb": float, "failed": list[dict{"name","pid","error"}], "dry_run": bool}`.
- Re-scans; `pids=None` ⇒ all current candidates; else only candidates whose pid is in `pids` (anything not a
  current candidate is silently ignored — the UI list may be stale; protection is re-checked here).
- Re-validate `create_time` equality before killing (PID reuse guard).
- dry_run: kill nothing; return what WOULD be closed with the estimated totals.
- Real run: for each app, kill its tree children-first: `terminate()` all, `psutil.wait_procs(timeout=3)`, then
  `kill()` survivors, wait 2 s. Catch `psutil.NoSuchProcess` (counts as closed), `psutil.AccessDenied`
  (→ failed), `pynvml.NVMLError` (→ treat VRAM as unknown). Freed totals = sum of estimates of apps that
  actually ended (unknown VRAM counts as 0 in the total). Never raises.

**get_whitelist()** → current `user_whitelist` (fresh copy). **update_whitelist(apps)** → de-dupe
case-insensitively (keep first spelling), sort case-insensitively, persist via `config_store.save_config`,
update in-memory config.

**list_whitelist_candidates()** → for the Whitelist modal: `[{"name": str, "whitelisted": bool, "running": bool}]`
= union of (names of processes that are eligible by heuristics (a)/(b) ignoring the user whitelist, still
excluding SYSTEM_PROTECTED/self/strata) and (current `user_whitelist` entries). Sorted by name, case-insensitive.

Thread-safety: methods may be called from worker threads; guard internal state with a `threading.Lock`.
Performance: one scan should finish < 1.5 s (one `process_iter(["pid","name","exe","ppid","create_time",
"memory_info"])` pass, one EnumWindows pass, one NVML pass). NVML init lazily once; if init fails, VRAM =
None for all and never retry more than once per 60 s.

## Contract 2 — `server_manager.py` → `class EndpointManager`

```python
class EndpointManager:
    def __init__(self, config: dict | None = None): ...
    def start_endpoint(self, on_ready_callback: Callable[[], None],
                       on_error_callback: Callable[[str], None]) -> bool: ...
    def stop_endpoint(self, on_stopped_callback: Callable[[dict], None]) -> bool: ...
    def is_running(self) -> bool: ...
    def status(self) -> dict: ...
    def protected_pids(self) -> set[int]: ...
```

- **start_endpoint**: returns immediately (non-blocking; work happens on an internal daemon thread). Returns
  False (and does NOT call either callback) if a start/stop is already in progress. If port is already
  listening with a healthy Strata → adopt it: call `on_ready_callback()` and return True. Otherwise: verify
  `launch_cmd[0]` exists (else `on_error_callback("...")`, return True), `subprocess.Popen(launch_cmd,
  cwd=strata_dir, stdin=DEVNULL, stdout=<log file>, stderr=STDOUT, creationflags=CREATE_NO_WINDOW |
  CREATE_NEW_PROCESS_GROUP)` where log = `logs\strata-YYYYmmdd-HHMMSS.log` in the project dir (append,
  keep newest 5). Poll every 1 s: process exited ⇒ `on_error_callback("Strata exited (code N) — see <log>")`;
  `GET /health` 200 with `loaded` true ⇒ ready; if health is 200 but `loaded` stays false > 15 s, send one
  `POST /load` (same JSON rules as /unload). Also accept `GET /v1/models` 200 with non-empty `data` as ready.
  Timeout `ready_timeout_s` ⇒ kill the tree we started, `on_error_callback("timed out after N s")`.
  Callbacks are invoked on the worker thread (the UI marshals them). Callback exceptions are caught + logged.
- **stop_endpoint**: non-blocking (daemon thread). Returns False if start/stop already in progress, or if nothing
  is running (then it calls nothing). Steps: (1) `POST /unload?wait=10` (timeout 15 s; ignore failures);
  (2) find the server: our Popen PID if alive, else the PID listening on `port`
  (`psutil.net_connections(kind="tcp")`, status LISTEN; on AccessDenied fall back to scanning processes whose
  cmdline contains `serve\server.py` AND whose exe/cwd is under `strata_dir`); (3) kill tree children-first:
  terminate → `wait_procs(5)` → kill survivors → `wait_procs(5)`; if the server's parent is `cmd.exe` whose
  cmdline contains `run-` and `.bat`, terminate that parent too (else its window sits at `pause`);
  (4) verify the port is released (poll ≤ `stop_timeout_s`); (5) toast via winotify:
  title "Strata stopped", msg "Strata stopped: <model_name> unloaded and memory freed."
  (toast failure is non-fatal); (6) `on_stopped_callback(result)` with
  `result = {"ok": bool, "port_released": bool, "killed_pids": list[int], "ram_freed_mb": float,
  "unload": "ok"|"busy"|"unavailable", "error": str|None}`. `ram_freed_mb` = summed RSS of the killed tree
  measured just before killing. Never raises out of the thread.
- **is_running()**: True iff something is LISTENING on `port` (fast; no HTTP). Thread-safe, < 50 ms.
- **status()**: `{"state": "idle"|"starting"|"active"|"stopping", "pid": int|None, "model_id": str|None,
  "adopted": bool, "log_path": str|None}`; `model_id` comes from the last successful /health or /v1/models.
- **protected_pids()**: our Popen PID + its recursive children + the current port-listener PID and its
  children + its `cmd.exe` launcher parent if any. Must never raise; empty set on errors.
- HTTP via `urllib.request` only (no `requests` dependency); every call has a timeout (≤ 3 s except unload).
- Must work without the UI (importable, usable from a plain script).

## Contract 3 — UI (`main.py`, `whitelist_modal.py`, `sweep_dialog.py`)

- `main.py`: `class AgentStartApp(ctk.CTk)`, `def main() -> None`, `if __name__ == "__main__": main()`.
  `ctk.set_appearance_mode("dark")`, geometry `360x260`, not resizable, title "Agent Start".
  Single instance via a named Windows mutex (`win32event.CreateMutex(None, False, "AgentStart.Singleton")`
  + `GetLastError()==ERROR_ALREADY_EXISTS` ⇒ message box + exit).
- Layout (everything must fit in 360x260 without clipping at 100% and 125% DPI scaling):
  - **Top bar**: title label "Agent Start"; status pill (CTkLabel with fg_color + corner_radius) —
    IDLE `#4B5563`, STARTING / STOPPING `#D97706`, ACTIVE `#10B981`, ERROR `#DC2626`; gear button "⚙"
    opening the Whitelist modal (disabled while STARTING/STOPPING).
  - **Center**: ON/OFF toggle button exactly 180x56, corner_radius 28. Text "ON" in IDLE/ERROR, "OFF" in
    ACTIVE, "…" + disabled in STARTING/STOPPING. Below it the dry-run preview label (wraplength ~330):
    in IDLE/ERROR it refreshes every 5 s from `detector.sweep_processes(dry_run=True)` run on a worker thread
    (skip a tick if the previous scan is still running) showing count, up to 3 names (+N more),
    and RAM/VRAM to be freed ("VRAM n/a" when unknown); in ACTIVE it shows the model id.
  - **Footer**: endpoint URL (left) and telemetry (right): "Freed: X.X GB" (sum of sweep + server stop this
    session) and current free system RAM (`psutil.virtual_memory().available`), updated every 5 s.
- **ON flow**: state → STARTING; worker thread runs `scan_killable_processes()`. If candidates exist, open
  `SweepDialog` (CTkToplevel, modal: `transient` + `grab_set`) listing them with checkboxes — `preselect`
  rows ticked, others unticked — each row "name — 1.2 GB RAM · VRAM n/a · reason". Buttons:
  "Close selected & start", "Start without closing", "Cancel" (Cancel/close-X ⇒ back to IDLE). Then on a
  worker thread: `sweep_processes(dry_run=False, pids=selected)` (only if any ticked), then
  `start_endpoint(on_ready, on_error)`. on_ready ⇒ ACTIVE; on_error ⇒ ERROR + message in preview label.
- **OFF flow**: state → STOPPING; `stop_endpoint(on_stopped)`; on_stopped ⇒ IDLE (or ERROR if not
  `port_released`), add `ram_freed_mb` to telemetry.
- **Startup**: if `endpoint.is_running()` ⇒ ACTIVE (adopted). A 5 s monitor also reconciles: in ACTIVE, if
  the port stops listening ⇒ IDLE; in IDLE/ERROR, if it starts listening ⇒ ACTIVE (someone launched Strata).
  Monitor never fights an in-progress STARTING/STOPPING.
- **Thread rule**: NO Tk/CTk call off the main thread. Worker threads and manager callbacks call
  `self.post(fn, *args)` which enqueues onto a `queue.SimpleQueue`; the main thread drains it every 50 ms via
  `self.after(50, self._pump)`. (Equivalent to `self.after(0, cb)` marshalling, but safe from non-Tk threads.)
- **WhitelistModal** (`whitelist_modal.py`, `class WhitelistModal(ctk.CTkToplevel)`, ctor
  `(master, detector)`): modal, ~320x300, `CTkScrollableFrame` of `CTkCheckBox` per
  `detector.list_whitelist_candidates()` (checked = whitelisted; label suffix "(not running)" when
  `running` False), an entry + "Add" button for typing an exe name (must end with `.exe`, non-empty), "Save"
  (→ `detector.update_whitelist(checked_names)`, close) and "Cancel". Candidate listing runs on a worker
  thread with a "Loading…" placeholder.
- **SweepDialog** (`sweep_dialog.py`, `class SweepDialog(ctk.CTkToplevel)`, ctor
  `(master, candidates: list[dict], on_decision: Callable[[str, list[int]], None])` where decision ∈
  `"close_and_start" | "start_only" | "cancel"`; `on_decision` called exactly once on the main thread.
- **WM_DELETE_WINDOW**: if STARTING/STOPPING ⇒ ignore close until done (show note in preview label). If ACTIVE
  ⇒ ask (CTk dialog or `tkinter.messagebox.askyesnocancel`): "Stop Strata before exit?" Yes ⇒ run OFF
  teardown, then destroy when `on_stopped` fires; No ⇒ exit leaving Strata running; Cancel ⇒ stay.
  IDLE/ERROR ⇒ exit. On exit call `detector.close()`.

## Phase 3 — QA rules (integrator)
- Import check of every module; `python -m py_compile` on all files; grep for TODO/FIXME/placeholder/`pass$`.
- Detector tested ONLY with `sweep_processes(dry_run=True)` and `scan_killable_processes()`; assert no
  protected/self/ancestor/whitelisted PID appears; real kills only against a throwaway child process spawned by
  the test itself (e.g. a hidden `python -c "import time; time.sleep(60)"` passed explicitly via `pids=` after
  temporarily treating it as candidate through a test subclass) — never against user apps.
- EndpointManager tested against a fake local HTTP server on a spare port (health/models/unload), including
  timeout, early-exit, adopt-existing, and port-release verification. Real Strata ON/OFF only after the user
  explicitly approves at that moment.
- UI smoke test: launch, verify geometry/widgets, drive state transitions with stub managers, screenshot.
