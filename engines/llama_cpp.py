"""llama.cpp's `llama-server` (single-model mode) as an engine.

Facts used here were read from llama.cpp master at commit 1d81992 (build b12314); see docs/llama_server_design.md.

Identity (stop path, STRICT): a process whose image name is exactly llama-server.exe AND whose command line names a
model (-m / -hf / -dr). Without a model llama-server runs as a router that spawns one child server per model on other
ports; such a process is never treated as this engine's server (a stop then refuses instead of guessing). Verified
from process facts when its exe is the configured program and its own --port is the configured port, or by the
port's reply carrying the header `Server: llama.cpp`.

Launch: the user types program + arguments in Settings (a CommandLaunch). plan_launch insists on an existing
llama-server.exe, a model and one explicit --port: the default port changed from 8080 to 9931 in build b11521, so it
is never left to the build.

Stop: no unload call exists in single-model mode (the process is ended). `is_busy` reads GET /slots so a normal Turn
off answers "busy" instead of cutting a reply. Tool-call endings are not visible (finish_reason is only in the
client's response), so `idle_grace_s` is long.

Metrics: per-request speeds come from the run's log (`prompt eval time` / `eval time` lines). Live state and the
model id need HTTP to the identified port; the poller only uses an `http` callable handed to it (seam `http=`), it
never opens a connection itself.

GPU fit (fit_check / parse_fit): llama-fit-params.exe from the configured llama-server's own folder answers 'how many
layers will this launch put on the GPU' in under 2 s without loading the weights. fit_check only builds the command
(the app's fit_check.run executes it); parse_fit judges its one-line answer, reading the model's layer count from the
GGUF header (a read-only file read, no process). See the section 'GPU fit pre-check' for every rule and its source.

Standard library + engines.base only: no subprocess, no sockets, no psutil calls that act on a process.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
import re
import struct
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Iterable, Mapping, cast

from .base import (
    CAP_FIND_INSTALL,
    CAP_LIVE_ACTIVITY,
    CAP_LOAD_PROGRESS,
    CAP_REQUEST_HISTORY,
    NO_ANSWER,
    CommandLaunch,
    Engine,
    FitCheck,
    FitReport,
    Http,
    HttpReply,
    LaunchError,
    LaunchPlan,
    LiveSample,
    MetricsPoller,
    MetricsSample,
    RequestSample,
    ServiceState,
    dir_prefix,
    path_under,
)

if TYPE_CHECKING:
    import psutil

SERVER_EXE = "llama-server.exe"
SERVER_HEADER = "llama.cpp"            # tools/server/server-http.cpp: set_default_headers({{"Server", "llama.cpp"}})
DEFAULT_PORT = 9931                    # common/common.h: int32_t port = 9931 (8080 before build b11521)
# Helpers a single-model server may start from its own folder: --connect runs llama-connect (README "--connect").
_HELPER_EXES = frozenset({"llama-connect.exe"})
# common/arg.cpp: the options that name the model. Without one of them (and without LLAMA_ARG_MODEL & co. in the
# environment) tools/server/server.cpp server_role_detect() starts a ROUTER.
_MODEL_FLAGS = frozenset({"-m", "--model", "-hf", "-hfr", "--hf-repo", "-dr", "--docker-repo"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0"})
_PORT_RE = re.compile(r"\d{1,5}")

# Cleaning rules for the speed graphs: the same values as metrics_store (MIN_DECODE_TOKENS, MIN_PROMPT_TOKENS,
# MIN_PROMPT_MS). Repeated here because an engine module may only import engines.base.
MIN_DECODE_TOKENS = 16
MIN_PROMPT_TOKENS = 256
MIN_PROMPT_MS = 50.0

_MAX_KEPT_REQUESTS = 500               # finished requests remembered for a catch-up poll
_RECENT_REQUESTS = 30                  # returned on a normal poll (the store keeps 30)
_MAX_READ_BYTES = 4 * 1024 * 1024      # log bytes read per poll (a long backlog is read over several polls)
_MAX_PENDING = 256                     # prompt lines waiting for their eval line
_HTTP_TIMEOUT_S = 1.5


# ------------------------------------------------------------------------------------------------ argument helpers
def _flag(tok: Any) -> str:
    """An argv token as llama.cpp's parser sees it: in a `--` option, '_' means '-' (common/arg.cpp)."""
    t = str(tok)
    return t.replace("_", "-") if t.startswith("--") else t


def _values(args: Iterable[Any], names: frozenset[str] | set[str]) -> list[str | None]:
    """The value after every occurrence of one of `names` (None: the option is the last token)."""
    toks = [str(a) for a in args]
    out: list[str | None] = []
    for i, tok in enumerate(toks):
        if _flag(tok) in names:
            out.append(toks[i + 1] if i + 1 < len(toks) else None)
    return out


def _port_value(text: str | None) -> int | None:
    """A port number written plainly (1..65535), else None. Stricter than the server's std::stoi on purpose."""
    if text is None or not _PORT_RE.fullmatch(text.strip()):
        return None
    n = int(text.strip())
    return n if 1 <= n <= 65535 else None


def effective_port(args: Iterable[Any]) -> int | None:
    """The port the server binds, read from argv: the LAST `--port N` wins (common/arg.cpp warns that only the last
    value is used). `--port=N` is not an option llama-server accepts, so it is not a port. None = no usable port."""
    vals = _values(args, {"--port"})
    return _port_value(vals[-1]) if vals else None


def has_model(args: Iterable[Any]) -> bool:
    """argv names a model (-m / -hf / -dr with a value): single-model mode, not a router."""
    return any(v is not None and v.strip() != "" for v in _values(args, _MODEL_FLAGS))


def _base(path: Any) -> str:
    return os.path.basename(str(path or "").strip().strip('"')).casefold()


def _same_file(a: str | None, b: str | None) -> bool:
    """Two paths name the same file (case, separators, `..` and links ignored). No disk access beyond realpath."""
    if not a or not b:
        return False
    try:
        ca = {os.path.normcase(os.path.abspath(a.strip('"'))), os.path.normcase(os.path.realpath(a.strip('"')))}
        cb = {os.path.normcase(os.path.abspath(b.strip('"'))), os.path.normcase(os.path.realpath(b.strip('"')))}
    except (OSError, ValueError):
        return False
    return bool(ca & cb)


def configured_program(cfg: Mapping[str, Any]) -> str | None:
    """launch_cmd[0] when it is an absolute path to a file named llama-server.exe, else None (e.g. a Strata config,
    whose launch_cmd starts with python.exe)."""
    cmd = cfg.get("launch_cmd") or []
    if not isinstance(cmd, (list, tuple)) or not cmd:
        return None
    prog = str(cmd[0]).strip().strip('"')
    if _base(prog) != SERVER_EXE or not os.path.isabs(prog):
        return None
    return prog


def _cfg_port(cfg: Mapping[str, Any]) -> int | None:
    try:
        raw: Any = cfg.get("port")
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 65535 else None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _float(text: str | None) -> float | None:
    """printf output to float; nan / inf / MSVC's '-nan(ind)' -> None."""
    try:
        f = float(str(text))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ------------------------------------------------------------------------------------------------ log parsing
# The server's per-request lines. Formats from tools/server/server-context.cpp (server_slot::print_timings, ~L753):
#   SLT_INF(*this, "prompt eval time = %10.2f ms / %5d tokens (%8.2f ms per token, %8.2f tokens per second)\n", ...)
#   SLT_INF(*this, "       eval time = %10.2f ms / %5d tokens (%8.2f ms per token, %8.2f tokens per second)\n", ...)
# with the prefix from tools/server/server-common.h (SLT_INF):  "slot %12.*s: id %2d | task %d | "
# and common/log.cpp optionally adding a timestamp, an "I " level tag and colour codes in front. Matching is done
# with `search`, not anchored, and ignores the 12-character function name (it is inferred, not captured output).
_SLOT = r"slot\s+\S+:\s+id\s+(?P<slot>\d+)\s+\|\s+task\s+(?P<task>-?\d+)\s+\|\s+"
_TIMING = (r"time\s*=\s*(?P<ms>\S+)\s+ms\s*/\s*(?P<n>\d+)\s+tokens\s*"
           r"\(\s*(?P<per>\S+)\s+ms per token,\s*(?P<tps>\S+)\s+tokens per second\)")
PROMPT_RE = re.compile(_SLOT + r"prompt eval " + _TIMING)
EVAL_RE = re.compile(_SLOT + r"eval " + _TIMING)          # "| prompt eval" cannot match: "prompt " sits in between
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Load phases, from format strings in the sources (each one INFO level, so at the default verbosity):
#   tools/server/server-context.cpp L1242   SRV_INF("loading model '%s'\n", ...)
#   src/llama-model.cpp L1716               "%s: loading model tensors, this can take a while... (load_mode = %s)\n"
#   src/llama-kv-cache.cpp L280             "%s: %10s KV buffer size = %8.2f MiB\n"
#   tools/server/server.cpp L696            SRV_INF("%s", "model loaded\n")
_PHASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"srv\s+\S+:\s+model loaded\s*$", re.MULTILINE), "Model loaded"),
    (re.compile(r"KV buffer size\s*="), "Setting up the context"),
    (re.compile(r"loading model tensors, this can take a while"), "Loading model weights"),
    (re.compile(r"srv\s+\S+:\s+loading model '"), "Loading model"),
)


def _log_message(line: str) -> str:
    """The text of one log line: colour codes removed; a --log-jsonl record gives its "msg"."""
    s = _ANSI_RE.sub("", line)
    st = s.lstrip()
    if st.startswith("{"):
        try:
            obj = json.loads(st)
        except ValueError:
            return s
        if isinstance(obj, dict) and isinstance(obj.get("msg"), str):
            return obj["msg"]
    return s


def _clean_decode(tokens: int | None, tps: float | None) -> float | None:
    if tps is None or tps <= 0 or tokens is None or tokens < MIN_DECODE_TOKENS:
        return None
    return tps


def _clean_prompt(tokens: int | None, ms: float | None) -> float | None:
    if tokens is None or ms is None or ms < MIN_PROMPT_MS or tokens < MIN_PROMPT_TOKENS:
        return None
    return tokens / (ms / 1000.0)


class LogRequests:
    """Incremental parser of one run's log: finished requests with stable keys.

    The log only grows, so a request's key is the log's file name plus the byte offset of its `eval time` line:
    the same request gets the same key on every read, two requests never share one, and keys of one run sort in
    the order the requests finished. A line without its final newline is left for the next read."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self._tag = os.path.basename(path) if path else ""
        self._pos = 0                                   # offset of the first byte not parsed yet
        self._pending: OrderedDict[tuple[int, int], tuple[float | None, int]] = OrderedDict()
        self.done: OrderedDict[str, RequestSample] = OrderedDict()

    def feed(self, data: bytes, start: int) -> list[RequestSample]:
        """Parse `data`, which begins at file offset `start` (must equal the parse position). Returns the requests
        that finished in it. Unterminated trailing text is not consumed."""
        out: list[RequestSample] = []
        end = data.rfind(b"\n")
        if end < 0:
            return out
        off = start
        for raw in data[:end + 1].split(b"\n")[:-1]:
            line_off = off
            off += len(raw) + 1
            text = _log_message(raw.rstrip(b"\r").decode("utf-8", "replace"))
            for m in PROMPT_RE.finditer(text):
                slot_task = (int(m["slot"]), int(m["task"]))
                self._pending[slot_task] = (_float(m["ms"]), int(m["n"]))
                self._pending.move_to_end(slot_task)
                while len(self._pending) > _MAX_PENDING:
                    self._pending.popitem(last=False)
            for m in EVAL_RE.finditer(text):
                slot, task = int(m["slot"]), int(m["task"])
                p_ms, p_n = self._pending.pop((slot, task), (None, None))  # no prompt line: no prompt figures
                gen = int(m["n"])
                key = f"{self._tag}#{line_off:012d}/s{slot}t{task}"
                req = RequestSample(key=key, time=None,
                                    decode_tps=_clean_decode(gen, _float(m["tps"])),
                                    prompt_tps=_clean_prompt(p_n, p_ms),
                                    prompt_tokens=p_n, generated_tokens=gen)
                if key not in self.done:
                    self.done[key] = req
                    out.append(req)
        while len(self.done) > _MAX_KEPT_REQUESTS:
            self.done.popitem(last=False)
        self._pos = start + end + 1
        return out

    def read(self, max_bytes: int = _MAX_READ_BYTES) -> list[RequestSample]:
        """Read what the file gained since the last call. Never raises (an unreadable file = nothing new)."""
        if not self.path:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size < self._pos:                         # the file was replaced: start over
                    self._pos = 0
                    self._pending.clear()
                if size == self._pos:
                    return []
                fh.seek(self._pos)
                data = fh.read(max_bytes)
        except OSError:
            return []
        out = self.feed(data, self._pos)
        if not out and len(data) == max_bytes and data.rfind(b"\n") < 0:
            self._pos += len(data)                           # one line longer than a whole read: skip it
        return out


# ------------------------------------------------------------------------------------------------ metrics poller
def live_from_slots(slots: Any) -> LiveSample | None:
    """GET /slots (tools/server/server-context.cpp server_slot::to_json) -> what the model does now.

    A slot is busy when `is_processing` is true. `next_token[0].n_decoded` (stats.n_gen) is 0 from the start of a
    task until its first generated token (reset when the slot is released and when the prompt is done), so a busy
    slot with n_decoded == 0 is reading the prompt and one with n_decoded > 0 is generating. None = not a slot list."""
    if not isinstance(slots, list) or not all(isinstance(s, dict) for s in slots):
        return None
    busy = [s for s in slots if s.get("is_processing") is True]
    if not busy:
        return LiveSample(state="idle")

    def decoded(s: dict) -> float | None:
        nt = s.get("next_token")
        if isinstance(nt, list) and nt and isinstance(nt[0], dict):
            return _num(nt[0].get("n_decoded"))
        return None

    gen = [d for d in (decoded(s) for s in busy) if d is not None and d > 0]
    if gen:
        return LiveSample(state="generating", generated=max(gen), queued=0)
    if all(decoded(s) == 0 for s in busy):
        read = [_num(s.get("n_prompt_tokens_processed")) for s in busy]
        known = [r for r in read if r is not None]
        return LiveSample(state="reading", prompt_read=max(known) if known else None, queued=0)
    # busy, but the phase is not readable: say generating (busy) without figures, never "idle"
    return LiveSample(state="generating", queued=0)


def _is_llama_reply(r: HttpReply) -> bool:
    return r.header("server").strip() == SERVER_HEADER


class LlamaCppPoller(MetricsPoller):
    """One serving session of llama-server.

    http: an `Http` bound to the identified port (the supervisor's call that adds the API key). Without it the
    poller reads only the run's log: finished requests, no live state, no model id. It never opens a connection
    itself."""

    def __init__(self, http: Http | None = None) -> None:
        self._http = http
        self._log = LogRequests(None)
        self._model: str | None = None
        self._model_tries = 0

    def reset(self, log_path: str | None = None) -> None:
        self._log = LogRequests(str(log_path) if log_path else None)
        self._model = None
        self._model_tries = 0

    def _get(self, path: str) -> HttpReply:
        if self._http is None:
            return HttpReply()
        try:
            r = self._http("GET", path, _HTTP_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a broken fetcher is "no answer"
            return HttpReply()
        return r if isinstance(r, HttpReply) else HttpReply()

    def _fetch_model(self) -> str | None:
        r = self._get("/v1/models")
        if r.code == 200 and _is_llama_reply(r) and isinstance(r.json, dict):
            data = r.json.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
                return model_label(str(data[0]["id"]))
        return None

    def poll(self, root_url: str, catch_up: bool = False) -> MetricsSample | None:
        try:
            return self._poll(catch_up)
        except Exception:  # noqa: BLE001 - contract: never raises
            return None

    def _poll(self, catch_up: bool) -> MetricsSample | None:
        self._log.read()
        done = list(self._log.done.values())
        reqs = tuple(done if catch_up else done[-_RECENT_REQUESTS:])
        live: LiveSample | None = None
        if self._http is not None:
            r = self._get("/slots")
            if r.code is None:
                return None                              # the server does not answer: a failed poll
            if r.code == 200 and _is_llama_reply(r):
                live = live_from_slots(r.json)
            if self._model is None and self._model_tries % 30 == 0:   # one model may not change; a 401 is
                self._model = self._fetch_model()                    # not asked again every second
            self._model_tries += 1
        return MetricsSample(model=self._model, requests=reqs, live=live)


# ------------------------------------------------------------------------------------------------ GPU fit pre-check
# llama-fit-params.exe ships next to llama-server.exe. It parses the SAME option table as the server, filtered to its
# own example (common/arg.cpp ~L1409 `add_opt`: an option registered only for LLAMA_EXAMPLE_SERVER is not in its
# table) and stops with "error: invalid argument" on any option it does not know (common/arg.cpp ~L1228). It then
# runs the server's own --fit pass (common/fit.cpp common_fit_params) and prints the fitted arguments as ONE stdout
# line, e.g. `-c 8192 -ngl -1` (measured, build b12314).
#
# Which arguments are passed on: an ALLOW-list of the options that change the fit's answer (model, context, batch,
# KV cache, flash attention, devices, the fit's own knobs), all of them common options the tool accepts. Everything
# else is dropped: the ~230 server-only options of b12314 (it would reject them: --port, --host, --api-key, -kvu,
# --mmproj, ...), and common options with side effects in a check: --log-file writes a file (arg.cpp ~L3916),
# --list-devices / --version / --help / --completion-bash / --cache-list print to stdout and exit. A deny-list would
# have to track every new server option of every build; an allow-list cannot pass an API key on.
FIT_EXE = "llama-fit-params.exe"
FIT_TIMEOUT_S = 20.0                   # measured < 2 s for a 27B model; the runner caps any value at 60 s
# options passed on with their value (common/arg.cpp, all registered for every example):
_FIT_PASS_VALUE = frozenset({
    "-c", "--ctx-size",                     # ~L1609 context (0 = the model's own, then --fit may shrink it)
    "-b", "--batch-size", "-ub", "--ubatch-size",    # ~L1639/L1646 compute buffer sizes
    "-fa", "--flash-attn",                  # ~L1726
    "-ctk", "--cache-type-k", "-ctv", "--cache-type-v",   # ~L2408/L2422 KV cache size
    "-dev", "--device", "-mg", "--main-gpu",               # ~L2709/L2851 which GPU(s)
    "-sm", "--split-mode",                  # ~L2800 (row / tensor make the check meaningless, see below)
    "-fit", "--fit", "-fitt", "--fit-target", "-fitc", "--fit-ctx",   # ~L2860-L2920 the fit's own knobs
    "--override-kv",                        # ~L2927 can change the model's hyper-parameters
})
_FIT_PASS_FLAG = frozenset({            # options without a value
    "--swa-full",                           # ~L1661 full-size SWA cache
    "-kvo", "--kv-offload", "-nkvo", "--no-kv-offload",   # ~L2386 KV cache on the GPU or not
    "--op-offload", "--no-op-offload",      # ~L2939
})
_PARALLEL = frozenset({"-np", "--parallel"})        # ~L2515 (server) / ~L2526 (other tools): both take a value
_KV_UNIFIED_ON = frozenset({"-kvu", "--kv-unified"})
_KV_UNIFIED_OFF = frozenset({"-no-kvu", "--no-kv-unified"})
_KV_PER_SLOT = "--kv-unified-per-slot"              # ~L1620, server-only: translated into -c (see fit_check)
_MODEL_PATH_FLAGS = frozenset({"-m", "--model"})
_GPU_LAYER_FLAGS = frozenset({"-ngl", "--gpu-layers", "--n-gpu-layers"})          # ~L2782
# Options that make the check meaningless (fit_check returns None), with the reason:
_FIT_REMOTE = frozenset({               # the model (or a part) comes from the network: the tool would download it
    "-hf", "-hfr", "--hf-repo", "-hff", "--hf-file", "-mu", "--model-url", "-dr", "--docker-repo",   # ~L3046-L3079
    "--rpc",                                # ~L2651 remote devices: the tool would contact other machines
    # server presets that set -hf themselves (~L4629-L4792)
    "--embd-gemma-default", "--fim-qwen-1.5b-default", "--fim-qwen-3b-default", "--fim-qwen-7b-default",
    "--fim-qwen-7b-spec", "--fim-qwen-14b-spec", "--fim-qwen-30b-default", "--gpt-oss-20b-default",
    "--gpt-oss-120b-default", "--vision-gemma-4b-default", "--vision-gemma-12b-default", "--spec-default",
})
_FIT_PLACED_BY_USER = frozenset({        # the user placed weights by hand: common/fit.cpp aborts the fit
    "-ot", "--override-tensor", "-cmoe", "--cpu-moe", "-ncmoe", "--n-cpu-moe",   # ~L567 overrides already set
    "-ts", "--tensor-split",                # ~L556 tensor_split already set (with more than one GPU)
})
_FIT_DRAFT = frozenset({                 # a draft model is fitted TOGETHER with the main one (common/common.cpp
    "-md", "--model-draft", "--spec-draft-model",           # ~L1246-L1270), but these options are server-only for
    "-hfd", "-hfrd", "--hf-repo-draft", "--spec-draft-hf",  # the tool (arg.cpp ~L4302 / ~L4011): its answer would
})                                                          # be for a different setup
# values that are always consumed with their option, even when they look like an option (secrets first)
_ALWAYS_VALUE = frozenset({"--api-key", "--api-key-file", "-hft", "--hf-token", "--ssl-key-file", "--ssl-cert-file",
                           "--connect-code"})
_NUMBER_RE = re.compile(r"[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?")


def _looks_like_option(tok: str) -> bool:
    return tok.startswith("-") and len(tok) > 1 and not _NUMBER_RE.fullmatch(tok)


def _last_value(args: list[str], names: frozenset[str] | set[str]) -> str | None:
    vals = [v for v in _values(args, names) if v is not None]
    return vals[-1].strip() if vals else None


def _kv_unified(args: list[str]) -> bool | None:
    """The last -kvu / -no-kvu wins (llama.cpp warns about repeats and keeps the last value). None = not given."""
    state: bool | None = None
    for tok in args:
        f = _flag(tok)
        if f in _KV_UNIFIED_ON:
            state = True
        elif f in _KV_UNIFIED_OFF:
            state = False
    return state


def _launch_cwd(cfg: Mapping[str, Any], prog: str) -> str:
    """The folder plan_launch starts the server in (relative model paths are resolved against it)."""
    cwd = str(cfg.get("launch_cwd") or "")
    return cwd if cwd and os.path.isdir(cwd) else os.path.dirname(prog)


def fit_model_path(cfg: Mapping[str, Any]) -> str | None:
    """The model file the configured launch loads (the LAST -m, like the server), absolute; None if none is named."""
    prog = configured_program(cfg)
    raw = cfg.get("launch_cmd") or []
    if prog is None or not isinstance(raw, (list, tuple)):
        return None
    m = _last_value([str(x) for x in raw[1:]], _MODEL_PATH_FLAGS)
    if not m:
        return None
    path = m.strip('"')
    try:
        return os.path.abspath(path if os.path.isabs(path) else os.path.join(_launch_cwd(cfg, prog), path))
    except (OSError, ValueError):
        return None


def _api_keys(cfg: Mapping[str, Any], args: list[str]) -> list[str]:
    keys: list[str] = []
    k = cfg.get("api_key")
    if isinstance(k, str) and k.strip():
        keys.append(k.strip())
    for v in _values(args, {"--api-key"}):
        keys.extend(x.strip() for x in (v or "").split(",") if x.strip())
    return keys


def _contains_secret(args: Iterable[str], keys: list[str]) -> bool:
    for a in args:
        for k in keys:
            if a == k or (len(k) >= 4 and k in a):
                return True
    return False


def fit_args(cfg: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The llama-fit-params arguments for this config, or None when a check would be meaningless (see fit_check)."""
    prog = configured_program(cfg)
    raw = cfg.get("launch_cmd") or []
    if prog is None or not isinstance(raw, (list, tuple)):
        return None
    args = [str(x) for x in raw[1:]]
    flags = {_flag(a) for a in args}
    if flags & (_FIT_REMOTE | _FIT_PLACED_BY_USER | _FIT_DRAFT):
        return None
    spec = _last_value(args, {"--spec-type"})
    if spec and "mtp" in spec.casefold():
        return None                          # an MTP context is fitted together with the model, like a draft
    ngl = _values(args, _GPU_LAYER_FLAGS)
    if any((v or "").strip().casefold() not in ("auto", "-1") for v in ngl):
        return None                          # common/fit.cpp ~L550: "n_gpu_layers already set by user ... abort"
    fit = _last_value(args, {"-fit", "--fit"})
    if fit is not None and fit.casefold() in ("off", "0", "false", "disabled"):
        return None                          # common/common.cpp ~L1239: no fit at all; the user's layers stand
    dev = _last_value(args, {"-dev", "--device"})
    if dev is not None and dev.casefold() == "none":
        return None                          # arg.cpp ~L1133: no GPU by the user's choice
    sm = _last_value(args, {"-sm", "--split-mode"})
    if sm is not None and sm.casefold() in ("row", "tensor"):
        return None                          # common/fit.cpp: SPLIT_MODE_TENSOR aborts; ROW aborts on 2+ GPUs
    model = fit_model_path(cfg)
    if model is None or not os.path.isfile(model):
        return None
    unified = _kv_unified(args)
    out: list[str] = ["-m", model]
    per_slot: str | None = None
    np_val: str | None = None
    i = 0
    while i < len(args):
        tok = args[i]
        f = _flag(tok)
        nxt = args[i + 1] if i + 1 < len(args) else None
        if f in _MODEL_PATH_FLAGS:
            i += 2                               # the model is passed once, absolute, above
        elif f in _FIT_PASS_VALUE:
            if nxt is None:
                return None                      # the server would refuse this command line
            out += [f, nxt]
            i += 2
        elif f in _FIT_PASS_FLAG:
            out.append(f)
            i += 1
        elif f in _PARALLEL:
            np_val = nxt
            i += 2
        elif f == _KV_PER_SLOT:
            per_slot = nxt
            i += 2
        elif f in _GPU_LAYER_FLAGS:
            i += 2                               # 'auto' / -1 = the default: nothing to pass on
        elif f in _ALWAYS_VALUE:
            i += 2
        elif nxt is not None and _looks_like_option(f) and not _looks_like_option(nxt):
            i += 2                               # an option that is not passed on, with its value
        else:
            i += 1
    # Slots: tools/server/server.cpp ~L128 turns an unset (or -1) -np into 4 slots with ONE unified KV cache, which
    # the fit sizes like a single sequence (common/fit.cpp: n_streams = kv_unified ? 1 : n_seq_max) - the tool's own
    # default of 1 slot (common/common.h n_parallel = 1) gives the same answer. -np N without -kvu: N sequences, as
    # the tool computes them; with -kvu the cache is one stream again, so -np is not passed on (-kvu is server-only).
    n_par: int | None = None
    if np_val is not None:
        try:
            n_par = int(np_val.strip())
        except ValueError:
            return None
        if n_par == 0:
            return None                          # the server refuses -np 0 (arg.cpp ~L2519)
    auto_np = n_par is None or n_par < 0
    if not auto_np and unified is not True and n_par is not None:
        out += ["-np", str(n_par)]
    # --kv-unified-per-slot N without -c: the server sizes the context to slots * N (server.cpp ~L139-L145)
    if per_slot is not None and not _values(args, {"-c", "--ctx-size"}):
        try:
            n = int(per_slot.strip())
        except ValueError:
            return None
        if n > 0:
            out += ["-c", str((4 if auto_np else (n_par or 1)) * n)]
    if _contains_secret(out, _api_keys(cfg, args)):
        return None                              # never hand an API key to another program
    return tuple(out)


# ---- GGUF header: the model's layer count (to judge "-ngl N")
_GGUF_SCALAR = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}   # value type -> bytes
_GGUF_INT = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 10: "<Q", 11: "<q"}
_GGUF_STRING, _GGUF_ARRAY = 8, 9
_GGUF_MAX_KV = 100_000
_GGUF_MAX_ELEMS = 4_000_000        # string elements stepped over (a 260k-token vocabulary is the usual largest)
_GGUF_MAX_STR = 1 << 20


class _GgufError(Exception):
    pass


class _GgufReader:
    def __init__(self, fh: Any) -> None:
        self.fh = fh
        self.elems = 0

    def read(self, n: int) -> bytes:
        b = self.fh.read(n)
        if not isinstance(b, bytes) or len(b) != n:
            raise _GgufError("short read")
        return b

    def u32(self) -> int:
        return int(struct.unpack("<I", self.read(4))[0])

    def u64(self) -> int:
        return int(struct.unpack("<Q", self.read(8))[0])

    def string(self, keep: bool) -> bytes | None:
        n = self.u64()
        if n > _GGUF_MAX_STR:
            raise _GgufError("string too long")
        if keep:
            return self.read(n)
        self.fh.seek(n, os.SEEK_CUR)
        return None


def gguf_block_count(path: str | None) -> int | None:
    """`<general.architecture>.block_count` from a GGUF file's header (v2/v3), or None. Reads only the metadata,
    never the weights; bounded; never raises. llama.cpp loads it as the layer count (src/llama-model.cpp ~L1488) and
    counts block_count + 1 offloadable layers, the output layer being the extra one (~L1766, ~L2538)."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            r = _GgufReader(fh)
            if r.read(4) != b"GGUF" or r.u32() not in (2, 3):
                return None
            r.u64()                                          # tensor count
            n_kv = r.u64()
            if n_kv > _GGUF_MAX_KV:
                return None
            arch: str | None = None
            counts: dict[str, int] = {}
            for _ in range(n_kv):
                key = (r.string(True) or b"").decode("utf-8", "replace")
                vt = r.u32()
                if vt == _GGUF_STRING:
                    val = r.string(key == "general.architecture")
                    if val is not None:
                        arch = val.decode("utf-8", "replace")
                elif vt in _GGUF_INT and key.endswith(".block_count"):
                    counts[key[:-len(".block_count")]] = int(struct.unpack(_GGUF_INT[vt],
                                                                         r.read(_GGUF_SCALAR[vt]))[0])
                elif vt in _GGUF_SCALAR:
                    fh.seek(_GGUF_SCALAR[vt], os.SEEK_CUR)
                elif vt == _GGUF_ARRAY:
                    et, n = r.u32(), r.u64()
                    if et in _GGUF_SCALAR:
                        fh.seek(_GGUF_SCALAR[et] * n, os.SEEK_CUR)
                    elif et == _GGUF_STRING:
                        r.elems += n
                        if r.elems > _GGUF_MAX_ELEMS:
                            return None
                        for _ in range(n):
                            r.string(False)
                    else:
                        return None                          # nested arrays: not in llama.cpp's metadata
                else:
                    return None
                if arch is not None and arch in counts:
                    c = counts[arch]
                    return c if 0 < c < 100_000 else None
    except (OSError, ValueError, OverflowError, struct.error, _GgufError):
        return None
    return None


# ---- the tool's answer
_FIT_LINE_MAX = 1024
_FIT_FLAG_RE = re.compile(r"--?[A-Za-z][A-Za-z0-9-]*")


def parse_fit_line(stdout: Any) -> dict[str, str] | None:
    """The tool's one stdout line as {option: value} (long spellings folded to the short ones), or None for anything
    that is not exactly one line of `-option value` pairs with exactly one layer count."""
    if not isinstance(stdout, str):
        return None
    text = stdout.strip()
    if not text or len(text) > _FIT_LINE_MAX or "\n" in text or "\r" in text or "\x00" in text:
        return None
    toks = text.split()
    if len(toks) % 2:
        return None
    alias = {"--ctx-size": "-c", "--gpu-layers": "-ngl", "--n-gpu-layers": "-ngl", "--tensor-split": "-ts",
             "--override-tensor": "-ot"}
    out: dict[str, str] = {}
    n_layer_opts = 0
    for i in range(0, len(toks), 2):
        opt, val = _flag(toks[i]), toks[i + 1]
        if not _FIT_FLAG_RE.fullmatch(opt) or _looks_like_option(val):
            return None
        opt = alias.get(opt, opt)
        if opt == "-ngl":
            n_layer_opts += 1
        out[opt] = val
    return out if n_layer_opts == 1 else None


def judge_fit(stdout: Any, block_count: int | None) -> FitReport:
    """The verdict on the tool's answer. Pure.

    `-ngl -1` / `auto`: common/fit.cpp returns early ("no changes needed", ~L441) and leaves the layer count at its
    default, so everything fits -> fits=True. A number N means the fit had to place layers because the whole model
    did not fit with the --fit-target margin. llama.cpp offloads min(N, block_count + 1) layers
    (src/llama-model.cpp ~L1766), so with the layer count from the GGUF header:
      * N < block_count + 1: whole layers stay on the CPU -> fits=False (measured on a 27B model: -ngl 63 and
        -ngl 54 served at 35 and 16 tok/s with the server at 49% CPU, against 42-56 tok/s and 6% fully on the GPU);
      * N >= block_count + 1: every layer is on the GPU, only part of one is not (its FFN overflows to the CPU,
        common/fit.cpp ~L580-L700) -> fits=None: not judged (a -ngl 65 run was not noticeably slower; the live check
        still watches);
      * layer count unknown: fits=False - the tool only prints a number after deciding the model does not fit.
    """
    try:
        line = parse_fit_line(stdout)
        if line is None:
            n = len(stdout.splitlines()) if isinstance(stdout, str) else 0
            return FitReport(None, f"llama-fit-params: unreadable answer ({n} line(s))")
        ctx = line.get("-c")
        where = f" at -c {ctx}" if ctx and ctx.isdigit() else ""
        ngl = line["-ngl"].casefold()
        if ngl in ("-1", "auto"):
            return FitReport(True, f"llama-fit-params: fits on the GPU{where}")
        if not re.fullmatch(r"\d{1,6}", ngl):
            return FitReport(None, f"llama-fit-params: unexpected layer count {line['-ngl'][:20]!r}")
        n = int(ngl)
        if block_count is None:
            return FitReport(False, f"llama-fit-params: only {n} layers fit on the GPU{where} (model's layer count "
                                    f"unknown)")
        total = block_count + 1
        if n == total - 1:
            # measured (27B, 16 GB card): 65 of 66 layers on the GPU served at full speed (55 tok/s, server at 6% CPU),
            # 63 of 66 at 35 tok/s and 49% CPU. One layer short is not worth a warning; the live check still watches.
            return FitReport(None, f"llama-fit-params: {n} of {total} layers on the GPU{where}, 1 on the CPU; "
                                   f"not judged")
        if n < total:
            return FitReport(False, f"llama-fit-params: {n} of {total} layers on the GPU{where}, {total - n} on the "
                                    f"CPU")
        return FitReport(None, f"llama-fit-params: all {total} layers on the GPU{where} but not all of one layer; "
                               f"not judged")
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        return FitReport(None, f"llama-fit-params: could not judge the answer ({type(exc).__name__})")


@dataclass(frozen=True)
class LlamaLaunch(CommandLaunch):
    """A CommandLaunch that names its model (the app's notes say "Model set to <model>", not the program).
    `models`: per-model settings to save with this launch (llama_models.Snapshot; empty = none changed)."""
    models: tuple = ()

    @property
    def model_name(self) -> str:
        m = _last_value([str(a) for a in self.args], _MODEL_PATH_FLAGS)
        return model_label(m.strip('"')) if m else super().model_name


@dataclass(frozen=True)
class FoundLlama:
    """What llama_finder found: every build, every model file, and the suggested pair (choice.build is the best
    build for this PC; choice.model only when there is exactly one model)."""
    builds: tuple
    models: tuple
    choice: Any


def model_label(model_id: str) -> str:
    """llama-server names a model after its -m argument, often a full path: show the file name without .gguf.
    An alias (--alias) or a repo name (org/model) is kept as it is."""
    text = str(model_id or "").strip()
    if text.casefold().endswith(".gguf"):
        text = re.split(r"[\\/]", text)[-1][:-5]
    return text or str(model_id or "")


# ------------------------------------------------------------------------------------------------ the engine
class LlamaCppEngine(Engine):
    id = "llama_cpp"
    name = "llama.cpp"
    default_port = DEFAULT_PORT
    log_prefix = "llama"
    # CAP_LIVE_ACTIVITY needs the poller's `http` seam (see metrics_poller); CAP_REQUEST_HISTORY reads the log of
    # a run this app started; CAP_LOAD_PROGRESS gives phase labels (no percentage exists in single-model mode).
    capabilities = frozenset({CAP_LIVE_ACTIVITY, CAP_REQUEST_HISTORY, CAP_LOAD_PROGRESS, CAP_FIND_INSTALL})
    # No tool-call endings: finish_reason is only in the client's response and the stop causes are logged at debug
    # level only. An agent between two tool calls leaves the server idle while the tool runs, so a "turn off when
    # it's done" waits a minute of idleness (the design note's suggestion) instead of the app's few seconds.
    idle_grace_s = 60.0

    # ------------------------------------------------------------------ identity (stop path)
    def is_server_process(self, name: str | None, cmdline: list[str] | None) -> bool:
        """Image name exactly llama-server.exe and a model on the command line. `llama.exe serve` (the llama.app
        multiplexer) does not count: llama.exe is also the CLI front end for other commands, and a llama.exe of
        unknown origin can sit in WindowsApps. A router (no model) does not count either: its children serve
        other ports, and the root walk would otherwise climb from one of them to the router."""
        if (name or "").strip().casefold() != SERVER_EXE:
            return False
        return has_model((cmdline or [])[1:])

    def verified_by_process(self, proc: "psutil.Process", cfg: Mapping[str, Any]) -> bool:
        """Its exe IS the configured llama-server.exe and its own --port is the configured port."""
        prog = configured_program(cfg)
        port = _cfg_port(cfg)
        if prog is None or port is None:
            return False
        try:
            name, exe, cmd = proc.name(), proc.exe(), proc.cmdline() or []
        except Exception:  # noqa: BLE001 - psutil.Error / OSError: unknown = no proof
            return False
        return self.is_server_process(name, cmd) and _same_file(exe, prog) and effective_port(cmd[1:]) == port

    def claims_port(self, cmdline: list[str], port: int, cfg: Mapping[str, Any]) -> bool:
        """Only an explicit --port (the last one, as the server uses it). No --port means the build's default, which
        changed between versions: no claim."""
        return effective_port((cmdline or [])[1:]) == port

    def probe(self, http: Http, timeout: float) -> ServiceState:
        """GET /health. Identity = the `Server: llama.cpp` header plus one of the two documented bodies:
        503 {"error": {"message": "Loading model", ...}} while loading, 200 {"status": "ok"} when ready
        (tools/server/server-http.cpp middleware_server_state, server-context.cpp get_health)."""
        try:
            r = http("GET", "/health", timeout)
        except Exception:  # noqa: BLE001
            return NO_ANSWER
        if not isinstance(r, HttpReply) or r.code is None:
            return NO_ANSWER
        body = r.json
        if _is_llama_reply(r):
            if r.code == 200 and isinstance(body, dict) and body.get("status") == "ok":
                return ServiceState(answered=True, is_engine=True, loaded=True)
            err = body.get("error") if isinstance(body, dict) else None
            if r.code == 503 and isinstance(err, dict) and (
                    str(err.get("message") or "").casefold().startswith("loading model")
                    or err.get("type") == "unavailable_error"):
                return ServiceState(answered=True, is_engine=True, loaded=False)
        # something answered, but not as llama-server answers /health: not identified
        return ServiceState(answered=True, is_engine=False, loaded=None)

    # ---- finding llama.cpp and models on this PC (llama_finder: bounded, read-only)
    def find_installs(self, stop_evt: Any = None) -> list[Any]:
        """One FoundLlama (the builds, the model files and the suggested pair), or [] when no build was found."""
        import llama_finder
        builds, models = llama_finder.find_all(stop_evt=stop_evt)
        if not builds:
            return []
        choice = llama_finder.choose(builds, models, llama_finder.has_nvidia())
        return [FoundLlama(tuple(builds), tuple(models), choice)]

    def install_dir_of(self, found: Any) -> str:
        build = getattr(getattr(found, "choice", None), "build", None)
        return str(getattr(build, "folder", "") or "")

    def install_settings(self, found: Any) -> dict:
        """Settings opens with the best build in the program box (nothing is saved until the user saves)."""
        build = getattr(getattr(found, "choice", None), "build", None)
        return {"launch_cmd": [build.program]} if build is not None else {}

    def auto_launch(self, found: list[Any], cfg: Mapping[str, Any]) -> Any:
        """A build and exactly one model: `-m <model> --port 9931` plus the model's short name (--alias), nothing
        else. Several models: the user picks."""
        try:
            import llama_models
            choice = getattr(found[0], "choice", None) if len(found) == 1 else None
            if choice is None or not choice.clear or choice.build is None or choice.model is None:
                return None
            session = llama_models.Session(cfg, os.path.dirname(choice.build.program))
            args = session.first_setup(choice.model.path, DEFAULT_PORT)
            return LlamaLaunch(program=choice.build.program, args=args, models=session.touched())
        except Exception:  # noqa: BLE001
            return None

    setup_action = "Choose model"

    def found_summary(self, found: Any) -> tuple[str, str]:
        build = getattr(getattr(found, "choice", None), "build", None)
        n = len(getattr(found, "models", ()) or ())
        tag = {"cuda": "CUDA", "vulkan": "Vulkan", "cpu": "CPU"}.get(str(getattr(build, "backend", "")),
                                                                    str(getattr(build, "backend", "")).upper())
        line = (f"{n} model{'' if n == 1 else 's'} on this PC" if n
                else "No model files found · add a .gguf in Settings")
        return tag, line

    def switch_choices(self, found: Any, cfg: Mapping[str, Any]) -> list[tuple[str, Any, bool]]:
        """One entry per model file found. Configured: the same program and shared options (port, key) with
        that model's own remembered arguments (llama_models; a model never configured gets a copy of the current
        ones without context, vision and alias). Not configured yet: the best build with `-m <model> --port 9931`
        and the model's short name."""
        try:
            import llama_finder
            import llama_models
            models = list(getattr(found, "models", ()) or ())
            if not models:
                return []
            prog = configured_program(cfg)
            raw = [str(x) for x in (cfg.get("launch_cmd") or [])]
            cur = fit_model_path(cfg) if prog else None
            build = getattr(getattr(found, "choice", None), "build", None)
            if prog is None and build is None:
                return []
            out: list[tuple[str, Any, bool]] = []
            for label, m in zip(llama_finder.unique_labels(models, llama_finder.model_label), models):
                if prog is not None:
                    args, snap = llama_models.switch_args(cfg, raw[1:], m.path, os.path.dirname(prog))
                    info = LlamaLaunch(program=raw[0], args=args, models=snap)
                else:
                    assert build is not None
                    session = llama_models.Session(cfg, os.path.dirname(build.program))
                    info = LlamaLaunch(program=build.program, args=session.first_setup(m.path, DEFAULT_PORT),
                                       models=session.touched())
                same = bool(cur) and os.path.normcase(os.path.normpath(cur or "")) == \
                    os.path.normcase(os.path.normpath(m.path))
                out.append((label, info, same))
            return out
        except Exception:  # noqa: BLE001
            return []

    def launch_title(self, info: Any) -> str:
        m = _last_value([str(a) for a in getattr(info, "args", ())], _MODEL_PATH_FLAGS)
        return model_label(m.strip('"')) if m else super().launch_title(info)

    def launch_options(self, found: Any, cfg: Mapping[str, Any]) -> dict | None:
        try:
            import llama_finder
            import llama_models
            builds, models = list(getattr(found, "builds", ())), list(getattr(found, "models", ()))
            prog = configured_program(cfg)
            # one working copy of the per-model settings for this Settings window: picking a model swaps the
            # arguments that belong to a model and keeps the shared ones; what changed is saved with the launch
            session = llama_models.Session(cfg, os.path.dirname(prog) if prog else "")

            def model_of(args: Any) -> str | None:
                m = _last_value([str(a) for a in args], _MODEL_PATH_FLAGS)
                return m.strip('"') if m else None

            def launch_info(info: Any) -> Any:
                return LlamaLaunch(program=info.program, args=tuple(info.args), models=session.touched())

            return {
                "program_title": "Build", "model_title": "Model",
                "programs": list(zip(llama_finder.unique_labels(builds, llama_finder.build_label),
                                     [b.program for b in builds])),
                "models": list(zip(llama_finder.unique_labels(models, llama_finder.model_label),
                                   [m.path for m in models])),
                "with_model": session.switch,
                "default_args": lambda model: session.first_setup(model, DEFAULT_PORT),
                "model_of": model_of, "launch_info": launch_info, "per_model": True,
                "context_choices": list(llama_finder.CONTEXT_CHOICES),
                "context_of": llama_finder.context_of, "with_context": llama_finder.with_context,
                "vision_files": llama_finder.vision_files,
                "vision_of": llama_finder.vision_of, "with_vision": llama_finder.with_vision,
                "port_of": llama_finder.port_of, "with_port": llama_finder.with_port,
            }
        except Exception:  # noqa: BLE001
            return None

    def fetch_model_id(self, http: Http, state: ServiceState) -> str | None:
        """As the default, but a model named after its file path is shown by its file name."""
        found = super().fetch_model_id(http, state)
        return model_label(found) if found else found

    def owns_child(self, proc: "psutil.Process", cfg: Mapping[str, Any]) -> bool:
        """llama-connect.exe (--connect) from the configured program's own folder. Everything else a server starts
        (MCP tool servers, docker) belongs to the user."""
        prog = configured_program(cfg)
        if prog is None:
            return False
        try:
            exe = proc.exe() or ""
        except Exception:  # noqa: BLE001
            return False
        if _base(exe) not in _HELPER_EXES:
            return False
        return _same_file(os.path.dirname(exe), os.path.dirname(prog))

    def protects_process(self, cmdline: Iterable[str] | None, exe: str | None, cfg: Mapping[str, Any]) -> bool:
        """LOOSE (sweep only): any llama-server.exe, and anything under the configured program's folder unless that
        folder is a drive root."""
        if _base(exe) == SERVER_EXE:
            return True
        try:
            first = next(iter(cmdline or []), None)
        except TypeError:
            first = None
        if first is not None and _base(first) == SERVER_EXE:
            return True
        prog = configured_program(cfg)
        if prog is None or not exe:
            return False
        folder = os.path.dirname(prog)
        if os.path.splitdrive(folder)[1].strip("\\/") == "":
            return _same_file(exe, prog)                 # a drive root: only the program itself
        return path_under(exe, dir_prefix(folder))

    # ------------------------------------------------------------------ launch
    def plan_launch(self, cfg: Mapping[str, Any]) -> LaunchPlan:
        raw = cfg.get("launch_cmd") or []
        cmd = [str(x) for x in raw] if isinstance(raw, (list, tuple)) else []
        if not cmd or not cmd[0].strip():
            raise LaunchError("No llama-server chosen yet · open Settings (gear) and choose llama-server.exe")
        prog = cmd[0].strip().strip('"')
        if _base(prog) != SERVER_EXE:
            raise LaunchError(f"The program must be llama-server.exe, not {os.path.basename(prog) or prog}")
        if not os.path.isabs(prog):
            raise LaunchError("Choose llama-server.exe with its full path (Browse…)")
        if not os.path.isfile(prog):
            raise LaunchError(f"llama-server.exe not found: {prog}")
        args = cmd[1:]
        cwd = str(cfg.get("launch_cwd") or "")
        if not cwd or not os.path.isdir(cwd):
            cwd = os.path.dirname(prog)

        if any(_flag(a).startswith("--port=") for a in args):
            raise LaunchError("Write the port as --port 9931 · llama-server does not accept --port=9931")
        ports = _values(args, {"--port"})
        if not ports:
            raise LaunchError(f"Add --port {self.default_port} to the arguments · Agent Start needs a fixed port")
        if len(ports) > 1:
            raise LaunchError("--port is given more than once · keep one")
        port = _port_value(ports[0])
        if port is None:
            raise LaunchError("--port needs a number from 1 to 65535 after it")
        want = _cfg_port(cfg)
        if want is not None and want != port:
            raise LaunchError(f"--port {port} does not match the port in config.json ({want}) · save the command "
                              f"again in Settings")

        models = _values(args, _MODEL_FLAGS)
        if not has_model(args):
            if models:
                raise LaunchError("-m needs a model file after it")
            raise LaunchError("Add a model: -m <file.gguf> or -hf <user>/<model> · router mode (no model) is not "
                              "supported")
        for v in _values(args, {"-m", "--model"}):
            if v is None or not v.strip():
                raise LaunchError("-m needs a model file after it")
            path = v.strip().strip('"')
            full = path if os.path.isabs(path) else os.path.join(cwd, path)
            if not os.path.isfile(full):
                raise LaunchError(f"Model file not found: {path}")

        for v in _values(args, {"--host"}):
            hosts = [h.strip().casefold() for h in (v or "").split(",") if h.strip()]
            if not any(h in _LOOPBACK_HOSTS for h in hosts):
                raise LaunchError("Agent Start reaches the server on 127.0.0.1 · use --host 127.0.0.1 or leave "
                                  "--host out")
        if _values(args, {"--api-prefix"}):
            raise LaunchError("Remove --api-prefix · Agent Start checks the server at /health")
        return LaunchPlan(cmd=[prog, *args], cwd=cwd)

    def is_configured(self, cfg: Mapping[str, Any]) -> bool:
        """A llama-server.exe is set up (a Strata launch command left in config.json does not count)."""
        return configured_program(cfg) is not None

    def same_launch(self, cfg: Mapping[str, Any], info: Any) -> bool:
        if not super().same_launch(cfg, info):
            return False
        import llama_models
        if not llama_models.same(cfg, getattr(info, "models", ())):
            return False                                 # another model's remembered settings changed
        port = effective_port(info.args) if isinstance(info, CommandLaunch) else None
        if port is None:
            return True
        return _cfg_port(cfg) == port and str(cfg.get("endpoint_url") or "") == f"http://127.0.0.1:{port}/v1"

    def apply_launcher(self, cfg: dict, info: Any) -> None:
        """launch_cmd / launch_cwd (base), plus `port` and `endpoint_url` from --port, and `api_key` from --api-key
        (the first key of a comma list) so the app can read /slots and /v1/models. Nothing else is touched; an
        absent --port leaves port and endpoint_url as they are (plan_launch then asks for one). The per-model
        memory (llama_models) is brought up to date: the arguments the config had are remembered under their model,
        then the entries the launch carries, then the new arguments under theirs."""
        old_args = None
        if isinstance(info, CommandLaunch) and configured_program(cfg) is not None:
            old_args = [str(x) for x in (cfg.get("launch_cmd") or [])][1:]      # only a llama.cpp command
        super().apply_launcher(cfg, info)
        if not isinstance(info, CommandLaunch):
            return
        import llama_models
        llama_models.apply(cfg, old_args, info.args, getattr(info, "models", ()), os.path.dirname(info.program))
        if not any(_flag(a).startswith("--port=") for a in info.args) and len(_values(info.args, {"--port"})) == 1:
            port = effective_port(info.args)
            if port is not None:
                cfg["port"] = port
                cfg["endpoint_url"] = f"http://127.0.0.1:{port}/v1"
        keys = _values(info.args, {"--api-key"})
        if keys and keys[-1]:
            first = next((k.strip() for k in keys[-1].split(",") if k.strip()), "")
            if first:
                cfg["api_key"] = first

    # ------------------------------------------------------------------ readiness / release
    def is_busy(self, http: Http) -> bool | None:
        """GET /slots: a slot with is_processing = true is a reply in progress. None when /slots is not available
        (--no-slots: 501, no key: 401, no answer) or does not come from llama-server."""
        try:
            r = http("GET", "/slots", 2.0)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(r, HttpReply) or r.code != 200 or not _is_llama_reply(r) or not isinstance(r.json, list):
            return None
        slots = [s for s in r.json if isinstance(s, dict)]
        if not slots:
            return None
        return any(s.get("is_processing") is True for s in slots)

    def request_headers(self, cfg: Mapping[str, Any]) -> dict[str, str]:
        key = cfg.get("api_key")
        if not isinstance(key, str) or not key.strip():
            return {}
        key = key.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in key):
            return {}                                    # never build a header that splits into two
        return {"Authorization": f"Bearer {key}"}

    def fit_check(self, cfg: Mapping[str, Any]) -> FitCheck | None:
        """llama-fit-params.exe from the configured llama-server.exe's own folder, with the launch's arguments that
        change the fit (fit_args: server-only and side-effect options removed, the model path made absolute, never an
        API key). None when nothing is configured, the tool is not there, or the check would be meaningless: the user
        set the GPU layers, turned the fit off, chose no GPU, placed tensors by hand, uses a draft/MTP model, or a
        model/device from the network (see fit_args). Disk access: two isfile checks. Never raises."""
        try:
            prog = configured_program(cfg)
            if prog is None:
                return None
            tool = os.path.join(os.path.dirname(prog), FIT_EXE)
            if not os.path.isfile(tool):
                return None
            args = fit_args(cfg)
            if args is None:
                return None
            return FitCheck(program=tool, args=args, timeout_s=FIT_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a broken config means "no check"
            return None

    def parse_fit(self, stdout: str, cfg: Mapping[str, Any]) -> FitReport:
        """judge_fit on the tool's stdout; for a numeric layer count the model's layer count is read from the GGUF
        header of the configured model (metadata only, bounded). Never raises."""
        try:
            line = parse_fit_line(stdout)
            blocks = None
            if line is not None and line["-ngl"].isdigit():
                blocks = gguf_block_count(fit_model_path(cfg))
            return judge_fit(stdout, blocks)
        except Exception as exc:  # noqa: BLE001 - contract: never raises
            return FitReport(None, f"llama-fit-params: could not judge the answer ({type(exc).__name__})")

    # ------------------------------------------------------------------ UI features
    setup_hint = "Open Settings (gear) and choose llama-server.exe and a model"
    launcher_kind = "llama-server.exe"
    launcher_pick_text = "Choose llama-server.exe."
    program_filetypes = (("llama-server", SERVER_EXE), ("Programs", "*.exe"), ("All files", "*.*"))
    has_dashboard = True                 # llama-server serves its web UI on / (unless started with --no-ui)
    watch_gpu_fit = True                 # run the live 'partly on the CPU' check (fit_watch) while serving

    def load_progress(self, log_text: str, elapsed_s: float, expected_s: float | None) -> tuple[str, float, str | None]:
        """A phase label from the log; the bar stays time-based (single-model mode reports no percentage)."""
        _label, frac, _detail = super().load_progress(log_text, elapsed_s, expected_s)
        text = _ANSI_RE.sub("", str(log_text or ""))
        for pat, label in _PHASES:
            if pat.search(text):
                return label, frac, None
        return "Starting llama-server", frac, None

    def metrics_poller(self, cfg: Mapping[str, Any] | None = None, **seams: Any) -> MetricsPoller:
        """seams: http(method, path, timeout) -> HttpReply for the identified port (with the API key). The app does
        not pass one today, so only the log is read (see LlamaCppPoller). Other engines' seams are ignored."""
        seam = seams.get("http")
        return LlamaCppPoller(http=cast("Http", seam) if callable(seam) else None)
