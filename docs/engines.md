# Engines

Agent Start supervises one local model server. The supervisor (start/stop state machine, port checks, the kill
sequence, the sweep, the UI) is engine-neutral. Everything that differs between inference engines is answered by
an `Engine` object from `engines/`, chosen by the `backend` key in `config.json` (missing or unknown: `strata`).

| File | What it is |
|---|---|
| `engines/base.py` | The interface: `Engine`, the capability names, and the plain data types that cross it |
| `engines/strata.py` | Strata, the only engine today |
| `engines/__init__.py` | The registry: `get_engine(cfg)`. It finds every module in the package by itself |

## Adding an engine

1. Write `engines/<name>.py` with a subclass of `Engine`. Set `id` (the `backend` value), `name` (shown to the
   user), `default_port`, `log_prefix` and `capabilities`.
2. Implement the six required methods (below). Implement an optional method only if the engine has the feature,
   and list the matching capability.
3. Add tests modelled on `tests/test_engines.py` (fake processes, fake HTTP; nothing real is started or killed).
4. Build, and check that `AgentStart.exe --selftest` exits 0.

That is all: the registry imports every module in `engines/` and registers each `Engine` subclass under its `id`,
`AgentStart.spec` collects the whole package, and `--selftest` loads it. Nothing in `server_manager.py`,
`detector.py`, `main.py` or the dialogs needs to change. The engine is the `backend` key in `config.json`.

Settings shows an **Engine** row once two or more engines are installed (with Strata alone it is hidden). Choosing
an engine there saves `backend`; the choice takes effect after Agent Start restarts.

Not yet proven on a frozen build: the package-wide collection in the spec and the registry's discovery inside the
packaged app. The first build after this change must run `--selftest`.

## Required methods

| Method | Question it answers | Rule |
|---|---|---|
| `is_server_process(name, cmdline)` | Is this process the engine's server? | Strict. Exact exe name or argv token, never a substring of the joined command line. It decides what a stop treats as the root and which descendants it ends. |
| `verified_by_process(proc, cfg)` | Is this matched process the user's install and not a look-alike? | Process facts only (for example: the binary lives in the configured install folder). No HTTP. |
| `claims_port(cmdline, port, cfg)` | When the listener's PID is hidden: does this server serve our port? | Read it from the command line. No guessing. |
| `probe(http, timeout)` | What does the port say? | Return a `ServiceState`: `answered`, `is_engine` (the reply positively identifies this engine), `loaded`, `model`. Must not raise. |
| `plan_launch(cfg)` | What command starts it? | Return a `LaunchPlan` whose `cmd[0]` is an existing `.exe`, or raise `LaunchError` with a message that tells the user what to fix. |
| `protects_process(cmdline, exe, cfg)` | Should the app sweep leave this process alone? | Loose. It only ever protects, so erring towards yes is safe. |

## The safety contract of the stop path

A stop ends only:

- a root process that `is_server_process` matched **and** that is verified: this app spawned it, or
  `verified_by_process` says yes, or `probe` returned `is_engine`;
- descendants of that root for which `is_server_process` or `owns_child` says yes, plus a `conhost.exe` whose
  parent is being ended;
- the root's direct, older parent, if `launcher_process` returns exactly that process.

The supervisor enforces the tree and parent bounds itself, never ends a descendant on the user's protected list
unless it is a server process, and treats an engine method that raises as "no". An engine cannot add a process
from outside the root's tree.

What the supervisor cannot do is judge `is_server_process` for the engine. The root is the top-most ancestor of
the listening process for which `is_server_process` is true (Strata's venv launcher starts the real interpreter
as a child, and both must end). A rule that is too loose - "any python.exe", say - therefore moves the root up to
an unrelated parent and takes that parent's tree with it. Every identity method must return true only on positive
proof: a process that survives a stop is an annoyance, a wrong kill is data loss.

Unloading is only sent to a port that was identified as the engine, and `request_headers` (an API key) only go
out when the port's own answer identified it. An engine without `CAP_UNLOAD` is stopped by ending its process;
the stop result then reports `unload: "unsupported"`. Such an engine can implement `is_busy(http)`: a normal
(non-forced) stop then answers "busy" while a reply runs, as Strata's unload call does, instead of cutting it off.

Known limit: the supervisor itself still equates "stopped" with "the process is gone". It always ends the
process after unloading, sets `released` from the port being free, and reports the engine as active while the
port answers as the engine. A long-running service that loads and unloads models inside one process (Ollama,
LM Studio) would need the supervisor to learn "unloaded but still running"; the UI would not.

The UI acts on the stop result's `released` field: the engine no longer holds the model. For an engine that is
a process the app ends, the supervisor sets it from the port being free. The UI does not depend on how the
release happened.

## Capabilities

An absent capability hides or simplifies the feature; it never breaks the app.

| Capability | Methods | Without it |
|---|---|---|
| `CAP_FIND_INSTALL` | `find_installs`, `install_dir_of`, `install_settings`, `preferred_launcher` | No first-start search; the user configures it in Settings |
| `CAP_LAUNCHER_FILES` | `list_launchers`, `current_launcher`, `read_launcher`, `same_launch`, `apply_launcher` | No model-switch button or tray submenu. Settings shows a program box and an arguments box instead of the launcher list; the result is a `CommandLaunch`, saved as `launch_cmd` / `launch_cwd` once `plan_launch` accepts it |
| `CAP_UNLOAD` | `unload` | A stop ends the process without asking the engine first |
| `CAP_LOAD_NUDGE` | `nudge_load` | The app only waits for the model to load |
| `CAP_LOAD_PROGRESS` | `load_progress` | A plain "Loading model" label and a time-based bar |
| `CAP_LIVE_ACTIVITY` | `metrics_poller` → `MetricsSample.live` | The status line shows the model name only; a safe turn-off cannot see a running reply and waits `idle_grace_s` instead. With none of the three metrics capabilities the app does not poll at all and treats the engine as "nothing reported", not as failing |
| `CAP_REQUEST_HISTORY` | `metrics_poller` → `MetricsSample.requests` | Empty speed graphs |
| `CAP_REQUEST_END` | `metrics_poller` → `MetricsSample.last_end` | Tool-call endings are only known if the live sample caught them (`LiveSample.in_tool_call`) |
| `CAP_RAM_MODES` | `load_report`, `memory_need_gib` | No "Low-RAM mode" note; the sweep keeps the detector's own pre-ticks |

## Metrics

`metrics_poller()` returns a `MetricsPoller`. The app calls `reset()` when a server run starts and
`poll(root_url, catch_up)` about once a second on a worker thread. Each poll returns one `MetricsSample`, the only
thing the metrics store, the status line and the graphs ever see:

- `live` (`LiveSample`): what the model is doing now (`state` idle / reading / generating, queue length, progress
  and speeds; every figure optional).
- `requests` (`RequestSample`): finished requests with a stable `key` and the speeds to plot, already cleaned by
  the engine.
- `last_end` (`RequestEnd`): how the most recent request ended, on the poll where it was seen to finish.
- `model`, `clock`, `vram_mb`.

The engine converts whatever it has (a JSON document, Prometheus counters, log lines). Strata's converter is
`metrics_store.strata_sample` and its poller is `metrics_store.StrataPoller`.

## Texts

The main window, the dialogs and the toasts name the engine with `Engine.name`. An engine can also set
`launcher_kind`, `launcher_pick_text`, `launcher_filetypes`, `program_filetypes`, `setup_hint`, `has_dashboard`
(whether `GET /` on the port is a web app worth a link) and the `low_ram_*` wording for `CAP_RAM_MODES`.
`is_configured(cfg)` says whether the user has set up a launch yet (default: `launch_cmd` is filled in).

## Config keys

Engine-specific settings live in `config.json` under the engine's own keys (Strata: `strata_dir`,
`launcher_bat`, `model_name`). The neutral keys every engine uses are `backend`, `launch_cmd`, `launch_cwd`,
`port`, `endpoint_url`, `ready_timeout_s` and `stop_timeout_s`. Existing keys must keep their meaning: users'
config files are never migrated by hand.
