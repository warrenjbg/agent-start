# Agent Start — Spec & Interface Contracts (v2 — LOCKED after Fable 5.2 review; v1.1 amendments at the end)

Windows 11 desktop widget (Python 3.11+, venv is 3.14; CustomTkinter 6, dark mode, 360x260 logical px) that:
1. **ON**: optionally force-closes RAM-hungry apps/games (only those the user ticks), then starts the local
   **Strata** inference server (OpenAI-compatible, `http://127.0.0.1:8080/v1`) and waits until the model is loaded.
2. **OFF**: unloads the model, kills the Strata process tree, verifies port 8080 is released, shows a Windows toast.

Host: Ryzen 9800X3D, 64 GB RAM, RTX 5070 Ti (WDDM). RAM is the binding constraint for Strata (IQ3_S streams ~70 GB
and wants "little else open"); VRAM only changes speed. Only ONE Strata may run at a time.

Project dir: `C:\Users\<user>\Downloads\Agent Start` · interpreter `.venv\Scripts\python.exe`
(customtkinter 6.0, psutil 7.2, pywin32 (win32gui/win32process/win32pdh/win32event/win32api/win32con/winerror),
winotify; Pillow 13 is installed for test screenshots only). **No pynvml for per-process VRAM** (verified: NVML
reports `usedGpuMemory=None` for all 48 GPU processes on this WDDM box); v1.1 uses pynvml (nvidia-ml-py) only for
the device-level total in the stats section.
Shipped code has no TODO/FIXME/placeholder text and no stub `pass` bodies.

## Files & ownership

| File | Owner | Purpose |
|---|---|---|
| `config.json` | Phase 1 (done) | Settings; `user_whitelist` edited by the UI |
| `config_store.py` | Phase 1 (done) | `load_config()`, `save_config(cfg)`, `server_root(url)`, `APP_DIR`, `LOG_DIR` |
| `detector.py` | Subagent 1 | `ProcessDetector` (+ module helpers) |
| `server_manager.py` | Subagent 2 | `EndpointManager` |
| `main.py`, `whitelist_modal.py`, `sweep_dialog.py` | Subagent 3 | UI |
| `tests/test_detector.py`, `tests/test_server_manager.py`, `tests/test_ui_smoke.py` | owning subagent | self-checks runnable as `.venv\Scripts\python.exe tests\x.py` (no pytest) |
| `requirements.txt`, `README.md`, `run.bat` | Integrator | |

Subagents must NOT edit files they don't own. If a contract is insufficient, say so in the final report.

**Shared conventions (all modules)**
- Logging: `log = logging.getLogger("agent_start")`; never `print` in library code (under `pythonw`
  stdout/stderr are None). `main.py` attaches a rotating file handler at `logs\controller.log`.
- Name normalisation: `norm(name) = name.casefold()` with ONE trailing `.exe` stripped. All name lists (SYSTEM,
  TREE_PROTECTED, user_whitelist) are compared on normalised `proc.name()` only.
- Path comparison: `os.path.normcase(os.path.abspath(p))`; "under dir D" means startswith `normcase(D) + os.sep`;
  an empty/missing D disables that rule (never matches everything).
- Config: `main.py` calls `config_store.load_config()` once and passes the SAME dict to `ProcessDetector` and
  `EndpointManager`. Both default to `load_config()` when given None (standalone use/tests).
- **Strata identity** (both modules): a process is a *Strata server process* iff its cmdline (joined, normcased)
  contains `serve\server.py`. This matches both the venv launcher `.venv\Scripts\python.exe` AND its real child
  (VERIFIED: the venv python.exe is a redirector — `Popen.pid` is the launcher, which spawns
  `C:\Users\<user>\AppData\Local\Python\pythoncore-3.14-64\python.exe` (exe NOT under strata_dir) plus a
  `conhost.exe`; the real child is the one that listens). The engine child is `strata.exe` under `strata_dir\engine`.

## Strata facts (verified against Strata 0.2.10 `serve/server.py`)
- Launch = `config["launch_cmd"]` (body of `run-swift-iq3_s.bat` minus `--open`), cwd = `config["strata_dir"]`.
- `GET /health` → 200 `{"status":"ok","loaded":bool,"model":str,...,"service":"strata"}`.
  `GET /v1/models` → 200 `{"data":[{"id":...}]}`. Server root = `config_store.server_root(endpoint_url)`.
- `POST /unload` and `POST /load` both exist. Both require header `Content-Type: application/json`, a body (send
  `{}` with Content-Length), and NO `Origin` header. `/unload` → 200 `{"status":...}`, or **409**
  `{"status":"busy"}` when a turn is running; `/unload?wait=<s>` waits up to s s for the turn. `/load` → 200
  `{"status":"loaded"}` or 503.
- Port already in use ⇒ `server.py` exits immediately. Cold load ≈ 30–90 s; `ready_timeout_s` = 180.
- Externally started Strata: double-clicked `run-*.bat` ⇒ `cmd.exe /c "...\run-*.bat"` → venv `python.exe`
  launcher → real `python.exe` (listener) → `strata.exe`.

## Contract 1 — `detector.py` → `class ProcessDetector`

```python
class ProcessDetector:
    def __init__(self, config: dict | None = None,
                 protected_pids_provider: Callable[[], set[int]] | None = None) -> None: ...
    def scan_killable_processes(self) -> list[dict]: ...
    def sweep_processes(self, dry_run: bool = False,
                        targets: list[tuple[int, float]] | None = None) -> dict: ...
    def get_whitelist(self) -> list[str]: ...
    def update_whitelist(self, apps: list[str]) -> None: ...
    def list_whitelist_candidates(self) -> list[dict]: ...
    def gpu_dedicated_used_mb(self) -> float | None: ...   # adapter-wide, see VRAM
    def close(self) -> None: ...                           # release PDH handles; idempotent
    # test seam — the ONLY place candidates come from; sweep and scan both use it:
    def _scan_apps(self) -> list["App"]: ...
```
`App` = internal dataclass: root `psutil.Process`, root `create_time`, list of member `psutil.Process` objects
captured in this scan (the disjoint kill set), member window handles (`hwnds`), plus the public dict fields.
`protected_pids_provider` exceptions are swallowed (treated as empty set).

### Public candidate dict (`scan_killable_processes()`, sorted by `ram_mb` desc) — EXACTLY these keys
`pid:int` (family root), `name:str`, `exe:str`, `create_time:float`, `ram_mb:float` (1 decimal),
`vram_mb:float|None`, `reason:str`, `category:"game"|"gpu"|"window"|"background"`, `preselect:bool`
(True only for "game"), `member_count:int`.

### Process graph (one pass)
- One `psutil.process_iter(["pid","name","exe","ppid","create_time","memory_info","cmdline"])` pass → pid→info.
- **A parent→child edge is valid only if parent.create_time <= child.create_time** (Windows never reparents; a
  dead parent's PID may be reused). Same rule for every ancestor walk.
- **Family root** = a process whose valid parent is absent or has a different normalised name.
- **App tree** of an eligible family root = root + ALL valid descendants, minus subtrees rooted at (a) other
  eligible family roots and (b) protected processes (rules below). Evaluate family roots deepest-first so nested
  apps (Steam → game) are carved out before their ancestors are evaluated. Trees are disjoint; every process is in
  at most one app. `ram_mb` = sum of RSS over the tree's killable members.

### Protection
- **HARD_PIDS**: this process, its validated ancestors, its descendants, `protected_pids_provider()` PIDs, every
  Strata server process (identity above) and its descendants, any process whose exe is under `strata_dir`.
- **TREE_PROTECTED names** (normalised): windowsterminal, wt, openconsole, powershell, pwsh, cmd, bash, sh, mintty,
  wsl, wslhost, ssh, sshd, alacritty, wezterm-gui, tabby, conemu64, code, code - insiders, cursor, windsurf, zed,
  devenv, idea64, pycharm64, rider64, clion64, webstorm64, goland64, claude, node, hermes, python, pythonw, py,
  pyw, strata, fancontrol, librehardwaremonitor, tailscale-ipn, tailscaled — plus every `user_whitelist` entry.
- **SYSTEM_PROTECTED names** (protect the process ONLY; descendants stay eligible — UWP/Xbox games run under
  svchost, apps under explorer): system, idle, registry, smss, csrss, wininit, winlogon, services, lsass, lsaiso,
  svchost, fontdrvhost, dwm, explorer, sihost, ctfmon, taskhostw, runtimebroker, dllhost, conhost,
  shellexperiencehost, startmenuexperiencehost, searchhost, searchapp, searchindexer, textinputhost,
  applicationframehost, systemsettings, lockapp, securityhealthsystray, securityhealthservice, msmpeng, nissrv,
  audiodg, spoolsv, wmiprvse, taskmgr, memory compression, secure system, vmmem, vmmemwsl, wslservice, wudfhost,
  widgets, smartscreen, backgroundtaskhost, nvcontainer, nvdisplay.container, nvsphelper64, nvidia overlay,
  nvidia app.
- Blanket: exe under `%SystemRoot%` ⇒ SYSTEM_PROTECTED. exe None/empty/AccessDenied ⇒ never a candidate and never
  killed. Session id ≠ ours (`ctypes.windll.kernel32.ProcessIdToSessionId`; failure ⇒ protected).
- **Rule A**: validated descendants of a TREE_PROTECTED process or of a HARD_PID are protected.
- **Rule B**: a would-be app whose tree contains any HARD_PID or TREE_PROTECTED process is dropped entirely.
- **Rule C**: SYSTEM_PROTECTED processes inside a candidate tree are excluded from the kill set and the RAM sum.

### Eligibility (per family root that is not protected) — priority game > gpu > window > background
- **game**: root exe path contains a `\<game_dir>\` segment (case-insensitive) for any `config["game_dirs"]`.
  (Launchers inside those dirs, e.g. Battle.net/Epic, get preselected too — README documents it.)
- **gpu**: tree VRAM estimate > `min_vram_mb`.
- **window**: any tree member owns a *real* top-level window AND tree RAM > `min_ram_mb`. Real = `IsWindowVisible`,
  non-empty title, NOT `WS_EX_TOOLWINDOW`, `GetWindow(h, GW_OWNER) == 0`, NOT DWM-cloaked
  (`dwmapi.DwmGetWindowAttribute(h, 14 /*DWMWA_CLOAKED*/, byref(c_int), 4)` value ≠ 0 ⇒ skip), non-empty rect.
  (Without cloak/toolwindow filters, background UWP hosts like PhoneExperienceHost show up — verified.)
- **background**: no real window, tree RAM > 1000 MB (tray hogs: Steam, Wallpaper Engine…). Never preselected.
- `reason`: `"game dir: steamapps"`, `"GPU ~612 MB VRAM"`, `"window, 1.2 GB RAM"`, `"background, 1.4 GB RAM"`.

### VRAM (no NVML)
- Per-process estimate: PDH `\GPU Process Memory(*)\Dedicated Usage` via `win32pdh.AddEnglishCounter` +
  `GetFormattedCounterArray(..., PDH_FMT_LARGE)`; instances look like `pid_<pid>_luid_0x..._phys_0`; sum per pid.
  Clamp each pid value to the adapter-wide dedicated usage (VERIFIED: some instances report impossible values,
  e.g. 26 GB for NVIDIA Overlay on a 16 GB card). App `vram_mb` = clamped tree sum (clamped again to adapter
  total), or None if PDH fails.
- `gpu_dedicated_used_mb()` = max over instances of `\GPU Adapter Memory(*)\Dedicated Usage` (VERIFIED matches
  nvidia-smi: 4326 vs 4296 MiB), or None on failure. Measured cost ≈ 0.14 s per query.

### sweep_processes(dry_run=False, targets=None) → dict
`{"closed_apps": list[str], "ram_freed_mb": float, "vram_freed_mb": float|None,
"failed": list[{"name": str, "pid": int, "error": str}], "dry_run": bool, "candidates": int,
"preselected": int, "preselected_ram_mb": float, "preselected_vram_mb": float|None, "names": list[str]}`
- Candidates come ONLY from `self._scan_apps()`. `targets=None` ⇒ all current candidates; else an app is selected
  iff `(pid, create_time)` matches a target with `abs(Δcreate_time) < 0.01`. Non-matching targets are ignored
  (UI list may be stale; protection is re-checked by the fresh scan).
- dry_run: kill nothing; `closed_apps`/`ram_freed_mb` = what WOULD close; `vram_freed_mb` = sum of estimates or
  None if any unknown. `names` = root names of the selected set sorted by RAM desc. `candidates`, `preselected`,
  `preselected_ram_mb`, `preselected_vram_mb` are filled in both modes (over the full candidate list).
- Real run: (1) `gpu_dedicated_used_mb()` before. (2) `win32gui.PostMessage(h, WM_CLOSE, 0, 0)` to every real
  window of every selected app; ONE shared wait ≤ 3 s (`psutil.wait_procs` over all members). (3) Survivors:
  terminate roots first (prevents helper respawn), then remaining members; `wait_procs(timeout=3)`; `kill()` any
  still alive; `wait_procs(timeout=2)`. Kill ONLY via the `psutil.Process` objects captured in this scan, after
  `is_running()` (psutil re-checks pid+create_time). Never `os.kill`, `taskkill`, `/T`. Catch
  `psutil.NoSuchProcess` (= ended) and `psutil.AccessDenied` (→ failed). (4) An app is closed iff its root is
  gone. `ram_freed_mb` = sum of RAM estimates of closed apps. (5) `vram_freed_mb` = max(0, before − after) after a
  1 s settle, or None if either reading failed. Never raises (log + return a dict).
- Thread-safety: an internal `threading.Lock` guards scans/shared state; a real sweep does not hold it while waiting.
- Performance target: one scan < 1.5 s.

### Whitelist
- `get_whitelist()` → copy of `user_whitelist`.
- `update_whitelist(apps)` → keep non-empty strings ending in `.exe` (case-insensitive), de-dupe by normalised name
  (keep first spelling), sort case-insensitively; RE-LOAD config.json from disk, replace only `user_whitelist`,
  `config_store.save_config`, then update the shared in-memory dict in place.
- `list_whitelist_candidates()` → `[{"name": str, "whitelisted": bool, "running": bool}]` = union of (root names of
  apps eligible under game/gpu/window/background when the user whitelist is IGNORED — HARD/SYSTEM and the built-in
  TREE_PROTECTED names still apply) and current `user_whitelist` entries (`running` = a process with that name is
  running). Sorted case-insensitively, de-duped by normalised name.

## Contract 2 — `server_manager.py` → `class EndpointManager`

```python
class EndpointManager:
    def __init__(self, config: dict | None = None, notify: bool = True) -> None: ...
    def start_endpoint(self, on_ready_callback: Callable[[], None],
                       on_error_callback: Callable[[str], None]) -> bool: ...
    def stop_endpoint(self, on_stopped_callback: Callable[[dict], None], force: bool = False) -> bool: ...
    def is_running(self) -> bool: ...
    def status(self) -> dict: ...
    def protected_pids(self) -> set[int]: ...
```
- **Threading**: `start_endpoint`/`stop_endpoint` only do a locked state check-and-set and spawn a daemon worker,
  then return. If they return True, exactly ONE of their callbacks fires exactly once, on the worker thread. If
  False, none fires. All probing (empty launch_cmd, missing exe, port, health) runs on the worker. Callback
  exceptions are caught + logged.
- **start_endpoint**: returns False if state is starting/stopping. Worker:
  1. If the port is listening (`is_running()`): never launch. Poll identity/readiness up to `ready_timeout_s`:
     Strata identified (health `service=="strata"`, or a Strata server process owns the listener) and ready ⇒
     adopt (`adopted=True`) ⇒ `on_ready`. Listener not Strata ⇒ `on_error("port <port> held by <name> (pid N),
     not Strata")`. Timeout ⇒ `on_error(...)`; never kill an adopted server.
  2. Else validate `launch_cmd` non-empty and `launch_cmd[0]` exists, else `on_error`.
  3. `Popen(launch_cmd, cwd=strata_dir, stdin=DEVNULL, stdout=<log>, stderr=STDOUT,
     creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, env=os.environ + {PYTHONUTF8: "1",
     PYTHONUNBUFFERED: "1"})`; log = NEW file `LOG_DIR\strata-YYYYmmdd-HHMMSS.log` (keep newest 5 `strata-*.log`);
     close the parent's log handle after spawn. Never `DETACHED_PROCESS`.
  4. Poll every 1 s: spawned process exited ⇒ `on_error("Strata exited (code N) — see <log>")`.
     **Ready iff `/health` 200 AND `service=="strata"` AND `loaded is True`.** If health is 200 with `loaded`
     false for > 15 s, send ONE `POST /load` and keep polling. `/v1/models` is only for `model_id` (first
     `data[].id`; fall back to health `model`).
  5. Timeout ⇒ kill the tree WE spawned (launcher + validated descendants) ⇒ `on_error("Strata not ready after
     N s — see <log>")`.
  6. Abort flag: if `stop_endpoint` is called while starting, the start worker exits quietly and its callbacks are
     suppressed (the stop callback reports instead, with `cancelled_start=True`).
- **stop_endpoint(cb, force=False)**: returns False if already stopping, or if not starting and the port is not
  listening. Allowed while *starting* (= cancel: set abort flag, kill the spawned tree, report via cb). Worker:
  1. Locate the **server root**: listener PID via `psutil.net_connections(kind="tcp")` (status LISTEN, laddr.port
     == port) → walk validated ancestors-or-self → top-most whose cmdline is a Strata server process. If no
     listener PID (AccessDenied / not found / dead), fall back to our Popen PID if alive, else a scan for Strata
     server processes. Listener exists but is not Strata ⇒ kill nothing;
     `cb({"ok": False, ..., "error": "port <port> held by <name> (pid N), not Strata"})`.
  2. Unless cancelling a start: `POST /unload?wait=10` (timeout 15 s); with `force=True` send `/unload` without
     `wait` (timeout 5 s). 200 ⇒ "ok"; 409 ⇒ "busy"; else "unavailable". **busy and not force ⇒ kill nothing**;
     `cb({"ok": False, "unload": "busy", "error": "busy", ...})` — the UI asks "Model is mid-turn — force stop?"
     and retries with `force=True` (busy + force ⇒ proceed to kill). Cancelling a start ⇒ unload "skipped".
  3. Capture the tree (root + validated descendants as `psutil.Process` objects); `ram_freed_mb` = summed RSS.
     Launcher parent: if the root's parent is `cmd.exe` whose cmdline contains `/c` and a `run-*.bat` path that
     resolves under `strata_dir` (case-insensitive), terminate that cmd.exe too (only it, never its other children).
  4. Terminate root-first then the rest → `wait_procs(5)` → `kill()` survivors → `wait_procs(5)`. Kill only via the
     captured `psutil.Process` objects after `is_running()`. Never `taskkill`/`os.kill`.
  5. Verify port released: poll `is_running()` until False, ≤ `stop_timeout_s`.
  6. Toast ONLY if the port was released and `notify` is True: winotify `Notification(app_id="Agent Start",
     title="Strata stopped", msg=f"Strata stopped: {model_id or model_name} unloaded and memory
     freed.")`. Failure is non-fatal (logged).
  7. `cb(result)`, `result = {"ok": bool, "port_released": bool, "killed_pids": list[int], "ram_freed_mb": float,
     "unload": "ok"|"busy"|"unavailable"|"skipped", "error": str|None, "cancelled_start": bool}`.
- **is_running()**: `socket.connect_ex(("127.0.0.1", port))`, 0.2 s timeout, == 0. No HTTP. Thread-safe.
- **status()** → `{"state": "idle"|"starting"|"active"|"stopping", "pid": int|None, "model_id": str|None,
  "adopted": bool, "log_path": str|None, "loaded": bool|None}`. When not starting/stopping, derived live: `active`
  iff the port is listening AND Strata identity confirmed within the last 10 s (identity probes cached, ≤ 1 per
  5 s, each ≤ 1 s timeout — so call status() from worker threads only). `loaded` from the last health probe (an
  external Strata still loading shows active + loaded False).
- **protected_pids()**: server root (located as in stop step 1, no killing, no HTTP) + validated descendants +
  launcher cmd.exe + our Popen PID and descendants. Never raises; empty set on error.
- HTTP: `urllib.request.build_opener(urllib.request.ProxyHandler({}))` (ignore system proxies); every call has a
  timeout (≤ 3 s; unload 15 s). `HTTPError` 409 ⇒ busy.
- Usable without the UI (import + call from a plain script).

## Contract 3 — UI (`main.py`, `whitelist_modal.py`, `sweep_dialog.py`)

- `main.py`: `class AgentStartApp(ctk.CTk)`, `def main() -> None`, `if __name__ == "__main__": main()`.
  Single instance BEFORE creating any Tk root (inside `main()`): `win32event.CreateMutex(None, False,
  "AgentStart.Singleton")`, then immediately `win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS` ⇒
  `ctypes.windll.user32.MessageBoxW(None, "Agent Start is already running.", "Agent Start", 0x40)`
  and exit. Keep the handle referenced at module level for process life.
  Logging: `RotatingFileHandler(LOG_DIR\controller.log, maxBytes=1 MB, backupCount=3)` on logger
  "agent_start", level INFO.
  `ctk.set_appearance_mode("dark")`, `geometry("360x260")`, `resizable(False, False)`, title "Agent Start".
  Constructor `AgentStartApp(detector=None, endpoint=None, config=None)` — None ⇒ real ones from one shared
  `load_config()` dict: `endpoint = EndpointManager(cfg)`, `detector = ProcessDetector(cfg,
  protected_pids_provider=endpoint.protected_pids)`. Tests inject stubs.
- Layout (fits 360x260 logical px; CTk applies DPI scaling itself):
  - **Top bar**: "Agent Start" label; status pill (CTkLabel, fg_color, corner_radius) — IDLE `#4B5563`,
    STARTING / STOPPING `#D97706`, ACTIVE `#10B981`, ERROR `#DC2626` (texts IDLE/STARTING/ACTIVE/STOPPING/ERROR;
    ACTIVE with `loaded` False shows "LOADING" in the amber colour); gear button "⚙" → WhitelistModal (disabled
    while STARTING/STOPPING).
  - **Center**: toggle `CTkButton(width=180, height=56, corner_radius=28)`. Text "ON" in IDLE/ERROR, "OFF" in
    ACTIVE, "Cancel" in STARTING (⇒ stop_endpoint = cancel), "…" disabled in STOPPING.
    Preview label below (wraplength ≈ 330, small font). IDLE/ERROR: refresh every 5 s from
    `detector.sweep_processes(dry_run=True)` on a worker (skip if a scan is still running or the window is
    iconified): line 1 `"N closable · M preselected · X.X GB RAM · VRAM ~Y.Y GB|n/a"` (RAM/VRAM of the preselected
    set; if none preselected, use the all-candidates totals and append "if all ticked"); line 2 up to 3 names
    "+N more". ACTIVE: `"Serving <model_id>"`. ERROR: the error text (until the next refresh after 15 s).
  - **Footer**: endpoint URL (left, small) and telemetry (right): `"Freed X.X GB · Free RAM Y.Y GB"` (session sum of
    sweep + stop `ram_freed_mb`; free = `psutil.virtual_memory().available`), updated every 5 s.
- **Thread rule**: NO Tk/CTk call off the main thread. Workers and manager callbacks call `self.post(fn, *args)` →
  `queue.SimpleQueue`; main thread drains every 50 ms in `_pump` (each item in try/except + log; reschedule in
  `finally`; cancel the after-id on destroy). A 5 s **monitor** worker calls `endpoint.status()` and posts it; the
  main thread reconciles ACTIVE↔IDLE from live state, never while STARTING/STOPPING or while a dialog is open.
  If `start_endpoint`/`stop_endpoint` returns False ⇒ reconcile from `status()` (fetched on a worker).
- **ON flow**: STARTING; worker `scan_killable_processes()`. Non-empty ⇒ `SweepDialog`; empty ⇒ start directly.
  `close_and_start` ⇒ worker `sweep_processes(dry_run=False, targets=…)` (add `ram_freed_mb` to telemetry) then
  `start_endpoint(on_ready, on_error)`; `start_only` ⇒ start; `cancel` ⇒ IDLE.
  on_ready ⇒ ACTIVE (preview "Serving …"); on_error(msg) ⇒ ERROR + msg.
- **OFF flow**: STOPPING; `stop_endpoint(on_stopped)`. on_stopped(result): `unload=="busy"` and not ok ⇒
  `messagebox.askyesno("Agent Start", "Model is mid-turn — force stop?")` on the main thread ⇒ yes:
  `stop_endpoint(on_stopped, force=True)`; no: back to ACTIVE. Else if `cancelled_start` ⇒ IDLE. Else
  `port_released` ⇒ IDLE and add `ram_freed_mb`; else ERROR + `error`.
- **SweepDialog** (`sweep_dialog.py`): `class SweepDialog(ctk.CTkToplevel)`,
  `__init__(self, master, candidates: list[dict], on_decision: Callable[[str, list[tuple[int, float]]], None])`;
  decision ∈ `"close_and_start" | "start_only" | "cancel"`; targets = `[(pid, create_time)]` of ticked rows
  (empty for start_only/cancel). `on_decision` is called EXACTLY once on the main thread (buttons; close-X ⇒
  cancel; guard flag). Scrollable list; `preselect` rows ticked; row text
  `"name — 1.2 GB · VRAM ~0.3 GB|n/a · reason"`. Warning line "Force-closes apps — unsaved work is lost."
  Buttons "Close selected & start", "Start without closing", "Cancel". ≈ 340x320, centred over master.
- **WhitelistModal** (`whitelist_modal.py`): `class WhitelistModal(ctk.CTkToplevel)`,
  `__init__(self, master, detector, on_saved: Callable[[], None] | None = None)`. Scrollable `CTkCheckBox` list from
  `detector.list_whitelist_candidates()` loaded on a worker (placeholder "Loading…", results via `master.post`);
  checked = whitelisted; suffix " (not running)" when not running. Entry + "Add" (must end with `.exe`; adds a
  checked row; ignores casefold duplicates). "Save" ⇒ `detector.update_whitelist(checked_names)` on a worker, then
  close + `on_saved()`; "Cancel" closes. ≈ 320x320.
- **Modal recipe (both dialogs)**: `transient(master)`; then `after(200, …)` runs `lift()`, `focus_force()`,
  `grab_set()` guarded by `winfo_exists()`; no `wait_window`; release grab on close.
- **WM_DELETE_WINDOW**: STOPPING ⇒ ignore (note in preview). STARTING ⇒ `askyesno("Cancel start and exit?")` (yes ⇒
  stop_endpoint cancel, exit when its callback fires). ACTIVE ⇒ `askyesnocancel("Stop Strata before exit?")`:
  yes ⇒ OFF teardown, destroy when on_stopped fires (busy ⇒ the force prompt applies; if the user declines force,
  stay open); no ⇒ exit leaving Strata running; cancel ⇒ stay. IDLE/ERROR ⇒ exit. Always `detector.close()`
  before destroy.

## Phase 3 — QA rules (integrator + subagents)
- NEVER close/kill any process the test did not spawn itself. NEVER call `sweep_processes(dry_run=False)` unless
  `_scan_apps()` is overridden by a test subclass that returns only test-spawned processes.
- NEVER start, stop, unload, or send any request to the real Strata / port 8080. Server-manager tests use a fake
  Strata: a stdlib `http.server` script saved as `<tempdir>\serve\server.py` (so the cmdline identity matches),
  run on a free high port via a temp config dict (`launch_cmd`, `strata_dir`=tempdir, `endpoint_url`, `port`).
- Import check, `py_compile` all, grep for TODO/FIXME/placeholder.
- Detector: `scan_killable_processes()` + `sweep_processes(dry_run=True)` on the real system; assert no HARD /
  TREE-protected / whitelisted / self / ancestor PID appears; print the candidate table.
- UI smoke: stub detector/endpoint, drive every state, screenshot (PIL `ImageGrab` of the window bbox) to
  `tests\ui_<state>.png`, confirm `winfo_width/height` ≈ 360x260 × CTk scaling, then destroy.
- Real Strata ON/OFF: integrator only, after the user explicitly approves at that moment.

## Integrator amendments (post-implementation, before advisor checkpoint 2)
- Detector: `game` requires tree RAM ≥ `min_game_ram_mb` (50); names in `game_exclude` (wallpaper32/64) are never
  `game`. Family roots: a child whose exe is in the SAME game install folder as its parent
  (`<game_dir>\<install>`, Steam: `steamapps\common\<game>`) folds into the parent; a non-game child whose exe is
  under the parent's exe folder (≥ 3 path levels, parent not under %SystemRoot%) folds into the parent
  (steam.exe ⇐ steamwebhelper). Reasons are short labels ("open window", "GPU memory", "background / tray",
  "game dir: X") because RAM/VRAM are shown separately.
- Sweep dict gains `preselected_names`; the IDLE preview names the preselected apps (≤ 22 chars each).
- SweepDialog: 372 px wide, two-line rows (bold name, muted "RAM · VRAM · reason").
- Server manager deviations accepted as reported by its builder: no `/load` to adopted servers; `ok == port_released`;
  fallback Strata scan matches `--port`; is_running disables Windows SYN retries (closed port < 1 ms).

## Advisor checkpoint 2 (Fable 5.2 code review → FIX; all 16 findings applied)
- BLOCKER fixed: the stop/kill path identifies Strata STRICTLY — process name python* AND an argv[1:] token equal to
  or ending with `\serve\server.py` on a path-segment boundary (`server_manager._is_strata_cmd`); the fallback scan
  also needs an explicit `--port` match (or a script under strata_dir for the default port). The detector keeps the
  loose substring match because there it only protects. Regression: `tests/test_identity.py`.
- Server manager: whole identity probes serialized (`_probe_run`); probe cache primed on ready (incl. root pid);
  spawned tree killed if the start worker raises; `protected_pids()` no fallback scan; toast falls back to the probed
  model id; fallback scan uses direct `p.cmdline()` (no shared `.info`).
- Detector: real sweep with `targets=None` is refused (no mass kill); PDH adapter/process counters opened
  independently, failures logged once then DEBUG, queries never reopen after `close()`; any `python*` name is
  TREE_PROTECTED; `collect_processes` keys on `p.pid`.
- UI: ACTIVE→IDLE needs two consecutive idle readings; ERROR is held for its full 15 s against the monitor;
  close-request re-dispatches if the state changed under a modal box; sweep failures shown before starting;
  `on_stopped` wrapped (no stuck STOPPING) + `report_callback_exception` → log; `_pump` stops draining after
  destroy; a failed cancel shows ERROR instead of IDLE.
- Verified: a Strata spawned by the controller survives the controller exiting under the venv `pythonw.exe`
  redirector (no job-object kill) — so "No" to "Stop Strata before exit?" leaves it running.

## v1.1.0 amendments (configurable launcher, stats section, central version)

### Files added
| File | Purpose |
|---|---|
| `version.py` | `__version__` — the single source. `tools/sync_version.py` generates `installer/version_info.txt` + `installer/version.iss` (included by the .iss); `build.bat` runs it first and names the installer from it |
| `launcher_bat.py` | Pure parser: run-*.bat → `LauncherInfo(bat_path, argv, cwd, strata_dir, port, config_path, model_name)`; `list_launchers`, `configured_bat`, `apply_to_config`, `same_launch`, `missing_files`; raises `LauncherError` (message shown inline) |
| `settings_dialog.py` | `SettingsDialog(master, cfg, detector, on_save, on_closed, current_bat, running, on_whitelist_saved)` — 340x252, same modal recipe |
| `metrics_store.py` | `MetricsStore` (accumulate /metrics requests by id, keep 30; live values), `decode_rate`, `prompt_rate`, `fetch_metrics(root_url)` |
| `vram_sampler.py` | `VramSampler` — daemon thread, NVML device memory every 2 s, 5-minute deque, `snapshot()`, `push_external()` fallback |
| `graphs.py` | `LineGraph`, `BarGraph` (tk.Canvas), `StatRow`; `nice_ceil`, `fmt_scale`, `fmt_rate` |
| `tests/test_launcher_bat.py`, `tests/test_stats.py`, `tests/test_e2e_fake.py` | see README |

### Launcher (.bat)
- The .bat is READ, never executed. Parsing: `\r\r\n`/`\r\n` line ends; `^` continuations; `@`, `rem`, `::`, labels
  skipped; `set VAR=v` / `set "VAR=v"`; `%VAR%` (bat-local, then environment, unknown → empty), `%~dp0`-style
  modifiers, `%%`; `cd /d` / `chdir` / `pushd` set the working directory (initially the .bat's folder); lines are
  split at unquoted `&` `|`, redirections dropped; tokens follow the MSVCRT rules.
- The launch line is the first command containing a token that ends with `serve\server.py` (path-segment boundary,
  same rule as `server_manager._is_strata_cmd`) preceded within 6 tokens by a `python*`/`py`/`pyw` interpreter
  (so `call`, `start "" /b`, `if … (` prefixes and `-u`-style options work). Otherwise → `LauncherError`
  "… does not start Strata".
- Result: interpreter, script and `--config` value made absolute against the working directory; `--open` removed;
  `--port N` / `--port=N` extracted (default 8080; invalid → error). `strata_dir` = parent of `serve\`.
  `model_name` = config json `model`, else `model_name`, else the config file name without `strata-`, else the .bat
  name without `run-`. `endpoint_url` = `http://127.0.0.1:<port>/v1`.
- config.json keys written together on Save (`launcher_bat.LAUNCHER_KEYS`): `launcher_bat` (FULL path),
  `launch_cmd`, `launch_cwd` (new; `Popen` cwd, falls back to `strata_dir`), `strata_dir`, `port`, `endpoint_url`,
  `model_name`. Save re-reads config.json from disk and replaces only these keys (same pattern as the whitelist).
- Backward compatibility: a v1.0 config (relative `launcher_bat`, hand-written `launch_cmd`, no `launch_cwd`) starts
  unchanged; `configured_bat()` resolves the relative name against `strata_dir` to preselect it in the dropdown.
- **Applying**: `EndpointManager` reads `port` / `endpoint_url` / `strata_dir` live from the shared dict, so the
  dict is only switched while the UI is IDLE/ERROR **and** `endpoint.is_running()` is False. Otherwise the choice is
  saved to disk, kept as `_pending_launcher`, shown as "`<bat>` applies next start" in the ACTIVE detail line, and
  applied on the next transition to IDLE/ERROR or at the next ON. Never restarts Strata.
- `_launcher_cmd` (stop path) also recognises `cmd.exe /c <configured launcher_bat>` when that .bat is outside
  `strata_dir`.

### Settings dialog
- Gear → `SettingsDialog` (disabled while STARTING/STOPPING, as before). Dropdown = `list_launchers(strata_dir)`
  (only `run-*.bat`) + the current/pending launcher if it lives elsewhere; "Browse…" (`filedialog`, any .bat);
  read-only line `model · config file · port` or the rejection reason in `T.RED_TEXT` (Save disabled);
  "Protected apps…" opens `WhitelistModal` on top (Settings releases its grab, re-grabs when the modal closes,
  closes it if Settings closes); Cancel / Save; version label.
- Shared dialog helpers in `sweep_dialog.py`: `style_dialog` (dark caption + app icon — CTkToplevel otherwise swaps
  in CustomTkinter's icon after 200 ms), `later` (tracked `after`), `release_modal` (cancels them).

### Stats section
- Chevron (`T.CHEVRON_DOWN`/`UP`, 20x16) in the memory-card header; collapsed geometry is exactly 360x268.
  Expanded: memory card bottom pad 16 → 8, stats card (`STATS_H` = 3 x 82 + 2) gridded at row 5,
  geometry 360x`HEIGHT_EXPANDED` (524); the window is slid up if it would leave the monitor's work area.
  `stats_expanded` persisted in state.json (written on a worker).
- Rows (`StatRow`, 82 px: 16 header + 40 canvas): title · muted tag (`live`/`last`) · mono value with unit;
  graph with a 34 px left gutter holding the light max-scale label, dashed line at the maximum, solid baseline.
  tk.Canvas is not scaled by CTk: all sizes/fonts are multiplied by the widget scaling at draw time
  (fonts in negative = pixel sizes).
  - **VRAM · 5 min** — `VramSampler` (own thread, always running; one NVML call per 2 s). Scale = card total.
    Line grey (`T.TEXT_3`), and green (`T.ACCENT`, tinted area) inside the spans where Strata was alive
    (server launch → stopped, tracked in `_track_span`). NVML missing/failing → "VRAM unavailable"; if Strata's
    `/metrics` `hardware.gpu_mem_used/total` (bytes) is available it is fed in via `push_external` instead.
  - **Decode · per request** — bars of `requests[].decode_tok_s`, last 30; live bar = `live.tok_s`
    (`tok_s_mean` while 0) when `live.state == "generating"`.
  - **Prompt reading · per request** — `prompt_read / (prompt_ms / 1000)`; without `prompt_read`:
    `prompt_total − reused`. Skipped when `prompt_ms` is missing or < 50 ms, or fewer than 64 tokens were read.
    Live = `live.prefill_tok_s_mean`, else Δ`prompt_read`/Δt between two polls, when `live.state == "reading"`.
- `/metrics`: `GET <server root>/metrics` with `Accept: application/json` (Strata answers Prometheus text only for
  `text/plain`/openmetrics/`format=prometheus`), no proxy, 1.5 s timeout, on the `hc-metrics` worker every 2 s,
  **only** while state is ACTIVE/STOPPING and the section is expanded and the window is not iconified. Results go
  through `self.post`; an epoch counter drops answers of a previous server. Strata returns its newest 12 requests
  → accumulated by `id` (fallback key: time) to 30. History is cleared at each server start, kept after OFF.
- Empty states: "Strata is off" (not serving, no data), "No requests yet" / "No prompt reads yet", "Metrics
  unavailable" (3 failed polls and no data), "VRAM unavailable", "Collecting…".
- Test seams: `AgentStartApp(..., metrics_fetcher=None, vram_sampler=None)`.

### Code review (v1.1) — fixed
- A monitor reading probed before a state change is dropped (`_state_seq`): a stale "active" delivered after OFF no
  longer flips the idle window back to Serving.
- Result notes ("Stopped · freed …", "Start cancelled", …) are held 4 s (`_set_note`); before, the sweep preview
  requested right after replaced them within a fraction of a second.
- Second instance: the running window is found by class `TkTopLevel` + title (an Explorer window of the folder
  "Agent Start" has the same title).
- Dialog icon (see `style_dialog`), dialog `after` scripts cancelled on close, dialogs centred within the virtual
  desktop (multi-monitor) instead of being pulled to the primary screen.
- `config_store.save_state` read-modify-write under the lock; `os.replace` retried on `PermissionError`;
  `_check_config` also handles `OSError` and a reset that fails.
- Stop path: `/unload` is no longer POSTed to a port whose listener could not be identified as Strata.
