# How the llama.cpp engine works

Status: **implemented, first version.** Module `engines/llama_cpp.py` (`backend`: `llama_cpp`), tests
`tests/test_llama_cpp.py`. This note maps `llama-server` onto the engine interface (`docs/engines.md`) and lists
the known limits and what is still unverified.

Sources: the llama.cpp repository at commit `1d81992` (build b12314), `tools/server/README.md` and the server
sources. Tested on one PC against build b12314: the winget build (Vulkan) and the CUDA build from llama.cpp's GitHub
releases.

## Scope

Single-model mode only: `llama-server.exe` started with a model (`-m`, `-hf` / `--hf-repo`, or `-dr`).

- **Router mode** (no model) is not supported. Such a process is never identified as the server, so a stop refuses
  instead of guessing, and `plan_launch` refuses a command without a model.
- **`llama.exe serve`** is not supported: `llama.exe` is also the command-line front end for other commands, so
  it is never matched.

## What maps to what

| Interface part | What the module does |
|---|---|
| `id` / `name` / `default_port` / `log_prefix` | `llama_cpp` / `llama.cpp` / 9931 / `llama`. 9931 is llama-server's own default since build b11521 (8080 before); it is never relied on |
| `capabilities` | `CAP_LIVE_ACTIVITY`, `CAP_REQUEST_HISTORY`, `CAP_LOAD_PROGRESS` |
| `plan_launch` | The user's program + arguments (a `CommandLaunch`), passed on unchanged. Refused: a program that is not an existing `llama-server.exe` given with its full path; no `--port`, `--port=N`, `--port` twice, a port outside 1-65535 or different from `port` in `config.json`; no model, or a `-m` file that does not exist (a relative path is checked against the working folder); a `--host` list without 127.0.0.1, localhost or 0.0.0.0; `--api-prefix` |
| `apply_launcher` | Saves `launch_cmd` and `launch_cwd` (the program's folder); `port` and `endpoint_url` (`http://127.0.0.1:<port>/v1`) from a single `--port`; `api_key` from the first `--api-key` |
| `is_configured` | `launch_cmd[0]` is an absolute path to a file named `llama-server.exe` (a Strata command left in `config.json` does not count) |
| `is_server_process` | Image name exactly `llama-server.exe` and a model on the command line |
| `verified_by_process` | Its exe is the configured `llama-server.exe` and its last `--port` is the configured port |
| `claims_port` | Only an explicit `--port` (the last one, as the server uses it); without one, no claim |
| `probe` | `GET /health` with the reply header `Server: llama.cpp`: 200 `{"status":"ok"}` = loaded, 503 *Loading model* = loading. Anything else that answers is not identified |
| `fetch_model_id` | The base class: `GET /v1/models` |
| `owns_child` | Only `llama-connect.exe` from the configured program's folder (started by `--connect`). Tool servers and other programs the server starts are left alone |
| `launcher_process` | None (a terminal that started the server is never ended) |
| `protects_process` | Any `llama-server.exe`, and anything under the configured program's folder (only the program itself when that folder is a drive root) |
| `is_busy` | `GET /slots`: a slot with `is_processing` true = busy. Unknown (the stop goes ahead) when `/slots` is off (`--no-slots`), refused (no key), or not answered by llama-server |
| `request_headers` | `Authorization: Bearer <api_key>` when `api_key` is set |
| `idle_grace_s` | 60 s |
| `load_progress` | A phase label from the log (*Loading model*, *Loading model weights*, *Setting up the context*, *Model loaded*); the bar stays time-based |
| `metrics_poller` | Live state from `GET /slots` through the app's `http` seam: idle, reading (busy, no token generated yet) or generating. Finished requests from the run's log (`prompt eval time` / `eval time` lines), with prompt and generated token counts. Model id from `GET /v1/models` |
| `has_dashboard` | True: llama-server serves its web page on `/` (unless started with `--no-ui`) |

Absent: `CAP_UNLOAD`, `CAP_LOAD_NUDGE`, `CAP_REQUEST_END`, `CAP_RAM_MODES`, `CAP_FIND_INSTALL`,
`CAP_LAUNCHER_FILES`.

## Stopping

Single-model mode has no unload call, so a stop ends the process (the stop result says `unload: "unsupported"`).
Before a normal (not forced) stop, `is_busy` asks `/slots`; a running reply makes the stop answer "busy", as
Strata's unload call does. A stop ends the verified `llama-server.exe`, its console host (`conhost.exe`) and an
owned `llama-connect.exe`; nothing else. Requests in flight are not drained.

## Known limits

- No unload without ending the process, and no load percentage (phase labels only).
- Tool-call endings are not visible (`finish_reason` is only in the client's response), so *Turn off when it's
  done* waits for 60 s of idle instead of following an agent between tool calls.
- A server Agent Start did not start has no log it reads: live state works, but no speed graphs and no usage
  history for it.
- A port set only through the environment (`LLAMA_ARG_PORT`) is not seen; `--port` is required on the command line.
- No full-RAM / low-RAM plan, no model switch, no first-start search for an install.

## Not verified yet

- Setups with `--api-key` (the key is saved and sent after the port identified itself; not tested against a real
  server).
- Builds other than b12314. The log lines the speeds come from were read from the b12314 sources; if a build
  changes them, the graphs and the usage history stay empty.
- The memory-pressure trigger's thresholds were measured with Strata. With llama.cpp's memory-mapped weights they
  have not been measured.
