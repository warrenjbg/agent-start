"""The engine interface: everything that differs between inference engines lives behind an `Engine`.

Agent Start supervises ONE local model server. The supervisor itself (start/stop state machine, port checks,
the kill sequence, the UI) is engine-neutral; an engine module answers the questions that are not:

    identity      which processes ARE this engine's server, and whether an HTTP reply comes from it
    launch        how the user's settings become a command line
    readiness     is the service up, is a model loaded, which one
    release       unloading the model / what a stop may take down with the server
    extras        finding an install, launcher files, load progress, activity + speed, memory needs

Engine features are optional. An engine lists what it supports in `capabilities`; callers check
`engine.can(CAP_...)` and hide or skip the feature when it is absent. The base class implements every optional
method as "not available", so a new engine only writes what it has.

SAFETY CONTRACT (the stop path): the supervisor only ever kills (a) a root process the engine positively
identified AND verified, (b) descendants of that root the engine claims with `owns_child` or
`is_server_process`, and (c) the root's direct parent if `launcher_process` returns it. The supervisor enforces
the tree/parent bounds itself, so an engine cannot widen a kill beyond them - but every identity method below
must still return True ONLY on positive proof. When unsure, return False: a process that survives a stop is an
annoyance, a wrong kill is data loss.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

if TYPE_CHECKING:  # psutil objects are only passed through; engines must not import the supervisor
    import psutil

# ------------------------------------------------------------------------------------------------ capabilities
CAP_FIND_INSTALL = "find_install"        # can search this PC for an install (first-start offer)
CAP_LAUNCHER_FILES = "launcher_files"    # the user picks launcher files the engine parses (also: model switch)
CAP_UNLOAD = "unload"                    # can release the model over HTTP (and say "busy")
CAP_LOAD_NUDGE = "load_nudge"            # can be asked to load when it sits unloaded after start
CAP_LOAD_PROGRESS = "load_progress"      # can turn its log into a progress label / fraction
CAP_LIVE_ACTIVITY = "live_activity"      # can say what the model is doing right now
CAP_REQUEST_HISTORY = "request_history"  # can list finished requests with speeds (graphs)
CAP_REQUEST_END = "request_end"          # can say how the LAST request ended (answer vs tool call)
CAP_RAM_MODES = "ram_modes"              # knows its memory requirement and a full-RAM / low-RAM verdict

ALL_CAPABILITIES: frozenset[str] = frozenset({
    CAP_FIND_INSTALL, CAP_LAUNCHER_FILES, CAP_UNLOAD, CAP_LOAD_NUDGE, CAP_LOAD_PROGRESS, CAP_LIVE_ACTIVITY,
    CAP_REQUEST_HISTORY, CAP_REQUEST_END, CAP_RAM_MODES,
})

# unload() results
UNLOAD_OK = "ok"                    # the model is released
UNLOAD_BUSY = "busy"                # a request is running; the engine refused (only without force)
UNLOAD_UNAVAILABLE = "unavailable"  # no answer / an error
UNLOAD_UNSUPPORTED = "unsupported"  # the engine has no unload: releasing the model means ending the process


# ------------------------------------------------------------------------------------------------ plain data
@dataclass(frozen=True)
class HttpReply:
    """One HTTP answer from the engine's port. code None = no answer at all."""
    code: int | None = None
    json: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)   # header names lower-cased

    def header(self, name: str) -> str:
        return str(self.headers.get(name.lower(), "") or "")


# http(method, path, timeout_s) -> HttpReply. Provided by the supervisor (loopback only, no proxy). Calls made
# after the port was identified carry engine.request_headers(cfg); the identity probe never does.
Http = Callable[[str, str, float], HttpReply]


@dataclass(frozen=True)
class ServiceState:
    """What one probe of the port says.

    answered   the port gave a reply this engine can judge (False: nothing there yet / unreadable)
    is_engine  the reply positively identifies THIS engine (kill/unload-grade proof)
    loaded     a model is loaded and can serve; None = unknown
    model      model id if the probe carries one
    """
    answered: bool = False
    is_engine: bool = False
    loaded: bool | None = None
    model: str | None = None


NO_ANSWER = ServiceState()


@dataclass
class LaunchPlan:
    """A validated command line. cmd[0] is an existing .exe; never a script (CreateProcess would run it via cmd)."""
    cmd: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)   # added to the app's environment


@dataclass(frozen=True)
class LiveSample:
    """What the model is doing at the moment of one poll. Every figure is optional (None = the engine does
    not report it); the UI shows what is there.

    state            'idle' | 'reading' (prompt processing) | 'generating'
    queued           requests waiting behind the running one
    phase            short display text for the generating phase ("thinking", "answering", ...), or None
    in_tool_call     the model is writing a tool call right now (the request will END in a tool call)
    tool             that tool's name, if known
    prompt_read / prompt_total   prompt tokens processed so far / to process (progress bar)
    prompt_tokens    size of the prompt
    generated / max_tokens       tokens generated so far / the request's limit
    decode_tps       generation speed right now (tok/s); decode_tps_mean = the request's mean so far
    prompt_tps_mean  prompt-processing speed of this request so far (tok/s)
    """
    state: str | None = None
    queued: int = 0
    phase: str | None = None
    in_tool_call: bool = False
    tool: str | None = None
    prompt_read: float | None = None
    prompt_total: float | None = None
    prompt_tokens: float | None = None
    generated: float | None = None
    max_tokens: float | None = None
    decode_tps: float | None = None
    decode_tps_mean: float | None = None
    prompt_tps_mean: float | None = None


@dataclass(frozen=True)
class RequestSample:
    """One FINISHED request. key identifies it across polls (the store keeps each key once); time orders it.
    decode_tps / prompt_tps are the speeds to plot, already cleaned by the engine (None = do not plot: too
    short an answer, a cache hit, no timing)."""
    key: str
    time: float | None = None
    decode_tps: float | None = None
    prompt_tps: float | None = None


@dataclass(frozen=True)
class RequestEnd:
    """How the most recent request ended. tool_call=True means the model asked for a tool: an agent will send
    another request shortly, so a 'turn off when it's done' must keep waiting."""
    tool_call: bool = False
    tool: str | None = None


@dataclass(frozen=True)
class MetricsSample:
    """One poll of the engine, in engine-neutral form: the ONLY thing the metrics store, the status line and the
    graphs ever see. An engine converts whatever it has (a JSON document, Prometheus counters, log lines).

    model     the served model id: a change resets the history (another server / a model switch)
    requests  finished requests, any order, repeats across polls allowed
    live      what the model is doing now; None = unknown (then nothing is shown as running)
    clock     the engine's own timestamp for this poll (seconds), if it has one
    vram_mb   (used MB, total MB) as the engine sees the GPU, if it reports it
    last_end  set on the poll where a request was seen to have just finished AND the engine could tell how
              (needs CAP_REQUEST_END); otherwise None
    """
    model: str | None = None
    requests: tuple[RequestSample, ...] = ()
    live: LiveSample | None = None
    clock: float | None = None
    vram_mb: tuple[float, float] | None = None
    last_end: RequestEnd | None = None


@dataclass(frozen=True)
class LoadReport:
    """How the engine loaded the model, for engines with more than one memory mode (CAP_RAM_MODES).

    mode  'low_ram'          running, but slower: not enough free RAM for its fast mode
          'waiting_for_ram'  still starting: waiting for RAM to become free
          'unknown'          nothing to say yet
          anything else      loaded normally (the engine's own name for it, e.g. 'full_ram'; only logged)
    need_gib / available_gib / short_gib   what the fast mode needs, what was available, how much was missing
    detail  extra text for the log only
    """
    mode: str = "unknown"
    need_gib: float | None = None
    available_gib: float | None = None
    short_gib: float | None = None
    detail: str = ""


class MetricsPoller:
    """Stateful poller for one serving session (it may remember things between polls, e.g. a request counter).
    The app calls reset() when a new server run starts and poll() about once a second from a worker thread.
    poll() returns None when the engine gave no usable answer; it must not raise."""

    def reset(self, log_path: str | None = None) -> None:
        """A new server run. log_path: the file this run's stdout/stderr go to when the app started the server
        (None for a server it adopted) - for engines whose per-request figures are only in their log."""
        return None

    def poll(self, root_url: str, catch_up: bool = False) -> MetricsSample | None:
        """catch_up=True: the app missed polls (was collapsed / just started) - return as much finished-request
        history as the engine keeps, not only the newest few."""
        return None


@dataclass(frozen=True)
class CommandLaunch:
    """A launch the user typed in Settings as program + arguments, for engines without launcher files. It offers
    what every launcher-info object offers (.path, .file_name, .model_name, .summary())."""
    program: str
    args: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.program

    @property
    def file_name(self) -> str:
        return os.path.basename(self.program)

    @property
    def model_name(self) -> str:
        return os.path.splitext(self.file_name)[0]

    @property
    def argv(self) -> list[str]:
        return [self.program, *self.args]

    def summary(self) -> str:
        n = len(self.args)
        return f"{self.file_name} · {n} argument{'' if n == 1 else 's'}"


def split_args(text: str) -> list[str]:
    """Split an arguments box the way Windows users write it: spaces separate, "double quotes" group."""
    out: list[str] = []
    cur: list[str] = []
    quoted = had = False
    for ch in str(text or ""):
        if ch == '"':
            quoted, had = not quoted, True
        elif ch.isspace() and not quoted:
            if cur or had:
                out.append("".join(cur))
            cur, had = [], False
        else:
            cur.append(ch)
    if cur or had:
        out.append("".join(cur))
    return out


def join_args(args: Iterable[str]) -> str:
    """The inverse of split_args for display: arguments with spaces (or empty) get double quotes."""
    return " ".join(f'"{a}"' if (not a or any(c.isspace() for c in a)) else a for a in (str(x) for x in args))


class LaunchError(Exception):
    """The settings cannot be turned into a launch; str(exc) is shown to the user."""


# ------------------------------------------------------------------------------------------------ path helpers
def norm_name(name: str | None) -> str:
    """casefold + strip ONE trailing '.exe' (same normalisation as detector.norm / user_whitelist entries)."""
    n = (name or "").casefold()
    return n[:-4] if n.endswith(".exe") else n


def dir_prefix(d: str | None) -> str:
    """normcase(abspath(d)) + sep, or '' when d is empty/unusable - for whole-segment `startswith` checks."""
    d = str(d or "").strip()
    if not d:
        return ""
    try:
        return os.path.normcase(os.path.abspath(d)).rstrip(os.sep) + os.sep
    except (OSError, ValueError):
        return ""


def path_under(path: str | None, prefix: str) -> bool:
    """True iff `path` (also after resolving links) lies under `prefix` (a dir_prefix() result)."""
    if not path or not prefix:
        return False
    try:
        full = os.path.normcase(os.path.realpath(str(path).strip('"')))
        plain = os.path.normcase(os.path.abspath(str(path).strip('"')))
    except (OSError, ValueError):
        return False
    real_prefix = dir_prefix(os.path.realpath(prefix.rstrip(os.sep)))
    return any(x.startswith(pre) for x in (full, plain) for pre in (prefix, real_prefix) if pre)


def explicit_port(cmd: list[str], flag: str = "--port") -> int | None:
    """`--port N` / `--port=N` if present, else None (no default - used where a guess could kill)."""
    for i, tok in enumerate(cmd):
        if tok == flag and i + 1 < len(cmd):
            try:
                return int(cmd[i + 1])
            except ValueError:
                return None
        if tok.startswith(flag + "="):
            try:
                return int(tok.split("=", 1)[1])
            except ValueError:
                return None
    return None


# ------------------------------------------------------------------------------------------------ the interface
class Engine:
    """Base class. Subclasses set `id`, `name`, `capabilities` and implement the REQUIRED section."""

    id: str = ""                       # the `backend` value in config.json
    name: str = ""                     # shown to the user: "<name> stopped", "port held by X, not <name>"
    default_port: int = 8080
    log_prefix: str = "engine"         # server log files are <log_prefix>-<timestamp>.log
    capabilities: frozenset[str] = frozenset()
    # Seconds the model must be idle before a "turn off when it's done" acts. None = the app's default, which
    # relies on the engine saying when an agent is between tool calls (CAP_REQUEST_END / LiveSample.in_tool_call).
    # An engine that cannot say so should set a longer wait here.
    idle_grace_s: float | None = None

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    # ===================================================================== REQUIRED: identity (stop path)
    def is_server_process(self, name: str | None, cmdline: list[str] | None) -> bool:
        """STRICT: this process is the engine's server. Decides what a stop treats as the root and which
        descendants it kills, so match exact argv tokens / exe names - never a substring of the joined cmdline."""
        raise NotImplementedError

    def verified_by_process(self, proc: "psutil.Process", cfg: Mapping[str, Any]) -> bool:
        """Kill-grade proof from process facts alone (no HTTP) that `proc` - already matched by
        is_server_process - is the user's engine and not a look-alike, e.g. its binary lives in the configured
        install folder. The process this app spawned itself is always trusted by the supervisor."""
        raise NotImplementedError

    def claims_port(self, cmdline: list[str], port: int, cfg: Mapping[str, Any]) -> bool:
        """Fallback when the listener's PID is hidden: does this server process (already matched by
        is_server_process) serve OUR port, judging by its command line? No guessing."""
        raise NotImplementedError

    def probe(self, http: Http, timeout: float) -> ServiceState:
        """Ask the port what it is and whether it is ready. Must not raise."""
        raise NotImplementedError

    def plan_launch(self, cfg: Mapping[str, Any]) -> LaunchPlan:
        """Validate the user's settings and return the command to run; raise LaunchError with a message that
        tells the user what to fix."""
        raise NotImplementedError

    def protects_process(self, cmdline: Iterable[str] | None, exe: str | None, cfg: Mapping[str, Any]) -> bool:
        """LOOSE: should the app sweep leave this process (and its tree) alone because it belongs to the engine?
        Only ever protects, so erring towards True is safe here."""
        raise NotImplementedError

    # ===================================================================== optional: stop path extras
    def owns_child(self, proc: "psutil.Process", cfg: Mapping[str, Any]) -> bool:
        """A descendant of the verified root that is not itself a server process: may a stop kill it? True only
        for the engine's own helpers (e.g. an exe inside the install folder). Default: no."""
        return False

    def launcher_process(self, root: "psutil.Process", cfg: Mapping[str, Any]) -> "psutil.Process | None":
        """The wrapper that started `root` and should end with it (a `cmd.exe /c launcher.bat`), else None. The
        supervisor only accepts root's direct parent."""
        return None

    # ===================================================================== optional: readiness / release
    def fetch_model_id(self, http: Http, state: ServiceState) -> str | None:
        """The served model's id. Default: OpenAI-style GET /v1/models, else what the probe said."""
        r = http("GET", "/v1/models", 2.0)
        if r.code == 200 and isinstance(r.json, dict):
            data = r.json.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
                return str(data[0]["id"])
        return state.model

    def nudge_load(self, http: Http) -> int | None:
        """CAP_LOAD_NUDGE: ask the service to load its model. Returns the HTTP status."""
        return None

    def is_busy(self, http: Http) -> bool | None:
        """Is a request running right now? Asked before a NON-forced stop of an engine without CAP_UNLOAD (an
        unload call says "busy" itself): True makes the stop answer "busy" instead of ending the process
        mid-reply. None = cannot tell (the stop goes ahead)."""
        return None

    def unload(self, http: Http, force: bool) -> str:
        """CAP_UNLOAD: release the model. force=False may answer UNLOAD_BUSY. Only called after the port was
        identified as this engine."""
        return UNLOAD_UNSUPPORTED

    def request_headers(self, cfg: Mapping[str, Any]) -> dict[str, str]:
        """Headers for requests to the engine's port AFTER it was identified (an API key the user configured).
        Never sent with the identity probe, so a foreign program on the port is not handed the key."""
        return {}

    # ===================================================================== optional: UI features
    # Each is used only when the matching capability is listed; the defaults mean "nothing to offer".
    def find_installs(self, stop_evt: Any = None) -> list[Any]:
        """CAP_FIND_INSTALL: bounded, read-only search for installs on this PC."""
        return []

    def install_dir_of(self, found: Any) -> str:
        """The folder of one find_installs() result (shown to the user, used as the install setting)."""
        return str(getattr(found, "install_dir", "") or "")

    def install_settings(self, found: Any) -> dict:
        """Config overrides that make Settings open on a found install (not saved until the user confirms)."""
        return {}

    def preferred_launcher(self, found: Any) -> str | None:
        """CAP_LAUNCHER_FILES: the launcher to preselect for a found install."""
        return None

    setup_hint: str = "Open Settings (gear) to set it up"     # shown when nothing is configured / found

    def is_configured(self, cfg: Mapping[str, Any]) -> bool:
        """Has the user set up a launch yet? (No: the first-start search / the setup hint are offered.)
        Default: the neutral `launch_cmd` key is filled in."""
        return bool(cfg.get("launch_cmd"))

    # ---- launcher files (CAP_LAUNCHER_FILES): the user picks one file per model; the engine reads it.
    # A launcher-info object must offer .path, .file_name and .model_name (text) and .summary() (one line).
    def list_launchers(self, cfg: Mapping[str, Any]) -> list[str]:
        """Launcher files that belong to the configured install (the model-switch menu lists them)."""
        return []

    def current_launcher(self, cfg: Mapping[str, Any]) -> str | None:
        """Absolute path of the launcher the config uses now, or None."""
        return None

    def read_launcher(self, path: str) -> Any:
        """Parse one launcher file into a launcher-info object; raise LaunchError (message for the user) if it is
        not a usable launcher (wrong format, files it needs are missing)."""
        raise LaunchError("this engine has no launcher files")

    def same_launch(self, cfg: Mapping[str, Any], info: Any) -> bool:
        """True if the config already launches exactly what `info` describes. Default: a CommandLaunch equals
        the configured launch_cmd."""
        if isinstance(info, CommandLaunch):
            return [str(x) for x in (cfg.get("launch_cmd") or [])] == info.argv
        return False

    def apply_launcher(self, cfg: dict, info: Any) -> None:
        """Write `info` into the config dict (launch command, port, endpoint, install folder...). Default: a
        CommandLaunch becomes launch_cmd, started in the program's folder."""
        if isinstance(info, CommandLaunch):
            cfg["launch_cmd"] = info.argv
            cfg["launch_cwd"] = os.path.dirname(info.program)

    # Settings texts. With CAP_LAUNCHER_FILES the user picks a launcher file; without it Settings shows a program
    # box and an arguments box (a CommandLaunch), checked with plan_launch before Save is offered.
    launcher_kind: str = "launcher file"                      # "No <kind> found", "Browse… to pick a <kind>."
    launcher_pick_text: str = "Pick the launcher file."
    launcher_filetypes: tuple[tuple[str, str], ...] = (("All files", "*.*"),)
    program_filetypes: tuple[tuple[str, str], ...] = (("Programs", "*.exe"), ("All files", "*.*"))
    has_dashboard: bool = False                               # GET / on the port is a web app worth a link
    # CAP_RAM_MODES wording for LoadReport.mode == "low_ram": the status summary, the progress detail while
    # loading, and the detail line while serving.
    low_ram_label: str = "Low-RAM mode"
    low_ram_loading: str = "slower"
    low_ram_serving: str = "Running slower"

    # ---- load progress (CAP_LOAD_PROGRESS)
    def load_progress(self, log_text: str, elapsed_s: float, expected_s: float | None) -> tuple[str, float, str | None]:
        """(label, fraction 0..1, detail or None) for the start-up progress bar, from the server log read so far.
        Default (no capability): a plain label and a time-based bar against the usual load time."""
        frac = min(0.95, elapsed_s / expected_s) if expected_s and expected_s > 0 else 0.0
        return "Loading model", max(0.0, frac), None

    # ---- memory (CAP_RAM_MODES)
    def load_report(self, cfg: Mapping[str, Any], since: float | None = None) -> LoadReport | None:
        """How the current run loaded (since = epoch seconds of our launch; None = the latest run), or None."""
        return None

    def memory_need_gib(self, cfg: Mapping[str, Any]) -> float | None:
        """RAM the configured model needs to run at full speed, if known (pre-ticks apps to close for it)."""
        return None

    def metrics_poller(self, cfg: Mapping[str, Any] | None = None, **seams: Any) -> MetricsPoller:
        """CAP_LIVE_ACTIVITY / CAP_REQUEST_HISTORY / CAP_REQUEST_END: a poller that turns the engine's own
        monitoring data into MetricsSample objects. cfg: the live config (API key, port). `seams` are
        engine-specific test hooks (fake fetchers); an engine ignores the ones it does not know. Default: a
        poller that reports nothing."""
        return MetricsPoller()

    def __repr__(self) -> str:
        return f"<Engine {self.id}>"
