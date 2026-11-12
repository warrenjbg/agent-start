# llama.cpp `llama-server` as an engine: design note

Status: **not implemented.** This note checks the engine interface (`docs/engines.md`) against how `llama-server`
really behaves, so that adding it later is one new module, `engines/llama_cpp.py`.

Source: the llama.cpp repository at commit `1d81992` (master, 2026-11-11): `tools/server/README.md` and the server
sources where the README is silent or out of date. Items marked *unverified* were not confirmed from a primary
source and must be checked against a real install before the module is written.

## What the check changed in the interface

Four things did not fit and were fixed in this pass:

1. **API key.** With `--api-key`, everything except `/health` answers 401. The supervisor's HTTP helper could
   not send headers. Added `Engine.request_headers(cfg)`; the supervisor attaches them to every call made after
   the port was identified and never to the identity probe, so a foreign program on the port is not sent the key.
2. **Per-request speeds are only in the log.** `llama-server` prints `prompt eval time` / `eval time` lines per
   request; HTTP only has running totals. The metrics poller had no way to find the log. `metrics_poller(cfg)`
   now receives the config, and `MetricsPoller.reset(log_path=...)` receives each run's log file.
3. **Tool-call endings are invisible.** `finish_reason` exists only in the client's response. The safe turn-off
   relies on knowing them, so engines can now set `Engine.idle_grace_s` to wait longer when they cannot tell.

4. **No "busy" refusal.** Strata's unload call refuses while a reply runs; `llama-server` has no unload call,
   so a normal stop would have cut a reply off. Added `Engine.is_busy(http)`, which `GET /slots`
   (`is_processing`) can answer.

Already covered by the design: identity from a response header (`HttpReply.headers`), no unload call
(`CAP_UNLOAD` absent), no launcher file (`CAP_LAUNCHER_FILES` absent), no RAM modes (`CAP_RAM_MODES` absent),
child server processes in router mode (`is_server_process` is asked about every descendant).

## Part by part

| Interface part | `llama-server` | Fits? |
|---|---|---|
| `id` / `name` / `default_port` / `log_prefix` | `"llama-cpp"` / `"llama.cpp"` / 9931 / `"llama"`. The default port moved from 8080 to 9931 in build b11521 (2026-10-09); Docker images still use 8080. | Yes. `plan_launch` should always pass `--port` so the default never matters. |
| `is_server_process` | Process name `llama-server.exe`. The newer `llama.exe serve` launcher runs the server in-process: name `llama.exe` with `serve` as the first argument. | Yes. Match the exe name exactly; for `llama.exe` require the `serve` token. |
| `verified_by_process` | The exe lives under the configured install folder (a new config key, e.g. `llama_dir`, or the folder of `launch_cmd[0]`). | Yes. |
| `claims_port` | `--port N` on the command line, or the `LLAMA_ARG_PORT` environment variable (not visible in argv). | Partly. Argv only; a server configured purely by environment is found through its listening socket, which is the normal path anyway. |
| `probe` | `GET /health`: 503 `{"error":{"message":"Loading model",...}}` while loading, 200 `{"status":"ok"}` when ready, both without a key. Every response carries `Server: llama.cpp`. | Yes: `is_engine` from the header, `loaded` from the status code. A failed load exits the process with code 1, which the supervisor already reports. |
| `fetch_model_id` | `GET /v1/models` (`owned_by: "llamacpp"`), needs the key if one is set. | Yes, with `request_headers`. |
| `plan_launch` | The user supplies the exe and arguments (`-m` or `-hf`, `--port`, `-c`, `-ngl`, ...). No launcher-file convention. | Yes: validate `launch_cmd[0]` is `llama-server.exe` / `llama.exe` and exists; add `--port` from the config if missing. |
| `protects_process` | Exe name, or exe under the install folder. | Yes. |
| `owns_child` | Router mode (started without a model) spawns one child per model, the same exe on free ports. They are server processes, so they are ended with the root without `owns_child`. | Yes. Default "no" is right. |
| `launcher_process` | None. | Yes (absent). |
| `unload` | Single-model mode: none. Router mode: `POST /models/unload {"model": id}`. | Yes: no `CAP_UNLOAD` at first. The stop result then says `unload: "unsupported"`. Router mode could add the capability later, but `unload(http, force)` sends no body today: the supervisor's HTTP helper posts `{}`. Sending a JSON body is a small extension to make then. |
| Stopping | No HTTP shutdown. Only CTRL_C is handled on Windows, and only after the model loaded; a tray app cannot send it to one process group, so the stop is `TerminateProcess` on the tree. In-flight requests are not drained (read from the source flow, not stated in the docs). | Yes: this is what the supervisor's kill sequence does. The safe turn-off (wait for idle first) matters more here because there is no "busy" refusal. |
| `nudge_load` | Not needed: a single-model server loads at start. | Yes (absent). |
| `load_progress` | Called with the log text only (no HTTP), so router mode's `/models/sse` progress figure cannot be used without extending it. No percentage in single-model mode. The log (stderr, which the supervisor captures with stdout) has phases: `loading model`, buffer size lines, `model loaded`, `listening on`. | Yes: `CAP_LOAD_PROGRESS` with a phase label and a coarse bar, or leave it out and get the time-based bar. |
| `metrics_poller` → `live` | `GET /slots` (on by default; needs the key): `is_processing`, `n_decoded`, prompt progress. `GET /metrics` (only with `--metrics`): `llamacpp:requests_processing`, `llamacpp:requests_deferred` (queue length). | Yes. Note that each `/metrics` scrape resets the averaging window of the throughput gauges, so the poller should use the counters, not the gauges. |
| `metrics_poller` → `requests` | Per-request `prompt eval time` / `eval time` lines in the log. | Yes, for servers this app started (`reset(log_path=...)`). An adopted server has no log: empty graphs. |
| `metrics_poller` → `last_end` | Not available. | Yes (no `CAP_REQUEST_END`); set `idle_grace_s` (suggested: 60 s) so a "turn off when it's done" does not fall into the gap between an agent's tool calls. This is weaker than Strata's behaviour and should be said in the UI text for that engine. |
| `load_report` / `memory_need_gib` | No full-RAM versus low-RAM switch. `--load-mode {auto,none,mmap,mlock,mmap+mlock,dio}` replaced `--mlock` / `--no-mmap`; `--fit on` (default) sizes `-ngl` and context to the device. Sizes appear in load-time log lines; nothing in `/props`. | Yes (no `CAP_RAM_MODES`). The sweep keeps the detector's own pre-ticks. |
| `find_installs` | Release zips are flat (`llama-server.exe`, `llama-server-impl.dll`, `llama.exe`, `ggml*.dll`). winget id `ggml.llamacpp`. Model cache: `LLAMA_CACHE`, else `%LOCALAPPDATA%\llama.cpp`. | Yes: search for `llama-server.exe`. *Unverified:* the winget install subfolder and its PATH links. |
| Launcher files / model switch | None. | Absent: the model-switch button hides, and Settings shows a program box and an arguments box (a `CommandLaunch`), which `plan_launch` checks before Save is offered. |

## Open points before writing the module

1. **Port and endpoint in Settings.** The program + arguments boxes save the command only. `port` and
   `endpoint_url` stay as they are in `config.json`; the module's `apply_launcher` should read `--port` from the
   arguments and set both, as Strata's does from its launcher file.
2. **Choosing the engine.** `backend` is read from `config.json`. Settings shows an Engine row once a second
   engine module is installed; choosing one saves `backend` and takes effect after Agent Start restarts.
3. **Idle wait without tool-call knowledge.** Pick `idle_grace_s` from real agent runs.
4. **Unverified facts** to confirm on a real install: the winget install path; whether CTRL_BREAK kills without
   cleanup; what happens to mmapped weights under memory pressure and the `mlock` limits on Windows (relevant to
   the memory-pressure trigger's thresholds); the field list of router-mode `/props`; whether `llama serve`
   reuses a server already on 9931.
5. **Memory-pressure trigger.** Its floor corrects for the engine's own growth using the working set of the
   engine's processes. With mmap, model pages are file-backed and counted differently from Strata's arena, so the
   defaults need their own measurement.
