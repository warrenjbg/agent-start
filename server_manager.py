"""Start / stop / monitor the local inference server (Contract 2 of SPEC.md).

`EndpointManager` launches the engine, waits until the model is loaded, and tears it down again (unload, kill
the process tree, verify the port is released, toast). Everything engine-specific - what the server process
looks like, how its HTTP answers are read, how it is launched and unloaded - comes from an `engines.Engine`
(Strata by default); this module is the engine-neutral supervisor and the only place that kills.
All slow work runs on daemon worker threads; results are
delivered through callbacks invoked on those worker threads.
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import json
import logging
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import psutil

import config_store
from config_store import LOG_DIR, load_config, server_root
from engines import get_engine
from engines.base import (
    CAP_LOAD_NUDGE,
    CAP_UNLOAD,
    NO_ANSWER,
    UNLOAD_BUSY,
    UNLOAD_UNAVAILABLE,
    UNLOAD_UNSUPPORTED,
    Engine,
    HttpReply,
    LaunchError,
    ServiceState,
)
from engines.base import dir_prefix as _dir_prefix  # noqa: F401  (kept importable under the old names)
from engines.base import explicit_port as _explicit_port  # noqa: F401
from engines.base import norm_name as _norm_name
from engines.base import path_under as _path_under  # noqa: F401
from engines.strata import is_strata_cmd as _is_strata_cmd  # noqa: F401  (tests pin the strict Strata rule)

log = logging.getLogger("agent_start")

_LOG_KEEP = 5
_HEALTH_TIMEOUT_S = 2.0
_PROBE_TIMEOUT_S = 1.0
_PROBE_MIN_INTERVAL_S = 5.0
_PROBE_VALID_S = 10.0
_SIO_TCP_INITIAL_RTO = 0x98000011
_RTO_PARAMS = struct.pack("<HBx", 0xFFFF, 0xFE)   # TCP_INITIAL_RTO_UNSPECIFIED_RTT, ..._NO_SYN_RETRANSMISSIONS

try:
    _ws2 = ctypes.WinDLL("ws2_32")
    _ws2.WSAIoctl.argtypes = [ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
    _ws2.WSAIoctl.restype = ctypes.c_int
except (AttributeError, OSError):
    _ws2 = None


def _no_syn_retries(sock: socket.socket) -> None:
    """Best effort: make a refused connect fail immediately instead of after Windows' SYN retries."""
    if _ws2 is None:
        return
    try:
        buf = ctypes.create_string_buffer(_RTO_PARAMS, len(_RTO_PARAMS))
        ret = wintypes.DWORD()
        _ws2.WSAIoctl(sock.fileno(), _SIO_TCP_INITIAL_RTO, buf, len(_RTO_PARAMS), None, 0,
                      ctypes.byref(ret), None, None)
    except (OSError, ValueError, ctypes.ArgumentError):
        pass


def _is_listen_entry(c, port: int) -> bool:
    """TCP-table row for a listening socket on `port`. VERIFIED on this host: listening sockets can show up with
    status NONE (and are missing from `netstat -ano`), so an unconnected row bound to the port also counts."""
    if not c.laddr or c.laddr.port != port:
        return False
    return c.status == psutil.CONN_LISTEN or (c.status == psutil.CONN_NONE and not c.raddr)


def _avail_mb() -> float | None:
    try:
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None


def _proc_label(proc: psutil.Process) -> str:
    try:
        return proc.name()
    except (psutil.Error, OSError):
        return "unknown process"


class EndpointManager:
    """Owns the lifecycle of one engine server on `config["port"]`.

    Extra keyword-only knobs (defaults match SPEC.md; tests shorten them):
      log_dir          directory for strata-*.log files (default: config_store.LOG_DIR, read at spawn time)
      poll_interval_s  readiness poll period while starting (default 1.0)
      load_nudge_s     seconds of `loaded: false` before the single POST /load (default 15.0)
    """

    def __init__(self, config: dict | None = None, notify: bool = True, *,
                 log_dir: str | os.PathLike | None = None,
                 poll_interval_s: float = 1.0,
                 load_nudge_s: float = 15.0,
                 engine: Engine | None = None) -> None:
        self._cfg = config if config is not None else load_config()
        self._engine: Engine = engine if engine is not None else get_engine(self._cfg)
        self._notify = notify
        self._log_dir = Path(log_dir) if log_dir is not None else None
        self._poll_s = max(0.05, float(poll_interval_s))
        self._nudge_s = max(0.0, float(load_nudge_s))
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        self._lock = threading.Lock()
        self._state = "idle"                       # idle | starting | stopping (active is derived)
        self._abort = threading.Event()
        self._start_thread: threading.Thread | None = None
        self._popen: subprocess.Popen | None = None
        self._spawn_proc: psutil.Process | None = None
        self._log_path: str | None = None
        self._model_id: str | None = None
        self._adopted = False
        self._loaded: bool | None = None
        self._last_pid: int | None = None
        # identity probe cache for status()
        self._probe_lock = threading.Lock()
        self._probe_run = threading.Lock()      # serializes whole identity probes
        self._probe_at = 0.0
        self._confirmed_at = 0.0
        self._probe_pid: int | None = None
        self._probe_model: str | None = None
        self._probe_loaded: bool | None = None
        self.last_toast: str | None = None         # message of the last toast shown (diagnostics/tests)
        # One-shot wording for the next stop's toast, set by the UI just before a stop it wants explained
        # (e.g. a safe Turn off that waited for the reply): (title, message); "{freed}" becomes e.g. "50.3 GB".
        self.next_toast: tuple[str, str] | None = None

    # ------------------------------------------------------------------ config helpers
    @property
    def _port(self) -> int:
        try:
            return int(self._cfg.get("port", self._engine.default_port))
        except (TypeError, ValueError):
            return self._engine.default_port

    @property
    def _root_url(self) -> str:
        return server_root(str(self._cfg.get("endpoint_url") or f"http://127.0.0.1:{self._port}/v1"))

    def _timeout_cfg(self, key: str, default: float) -> float:
        try:
            return float(self._cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------ public API
    def is_running(self) -> bool:
        """True iff 127.0.0.1:<port> accepts a TCP connection (connect_ex, 0.2 s timeout). No HTTP.

        Windows retries a refused loopback SYN (~2 s), so the socket first gets SIO_TCP_INITIAL_RTO with
        "no SYN retransmissions": a closed port then answers in < 1 ms instead of hitting the 0.2 s timeout.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                _no_syn_retries(s)
                s.settimeout(0.2)
                return s.connect_ex(("127.0.0.1", self._port)) == 0
        except OSError:
            return False

    def start_endpoint(self, on_ready_callback: Callable[[], None],
                       on_error_callback: Callable[[str], None]) -> bool:
        with self._lock:
            if self._state in ("starting", "stopping"):
                return False
            self._state = "starting"
            self._abort = threading.Event()
            self._model_id = None
            self._adopted = False
            self._loaded = None
            t = threading.Thread(target=self._start_worker, args=(on_ready_callback, on_error_callback),
                                 name=f"{self._engine.id}-start", daemon=True)
            self._start_thread = t
            t.start()
        return True

    def stop_endpoint(self, on_stopped_callback: Callable[[dict], None], force: bool = False) -> bool:
        with self._lock:
            if self._state == "stopping":
                return False
            cancelling = self._state == "starting"
            if not cancelling and not self.is_running():
                return False
            if cancelling:
                self._abort.set()
            self._state = "stopping"
            t = threading.Thread(target=self._stop_worker,
                                 args=(on_stopped_callback, bool(force), cancelling, self._start_thread),
                                 name=f"{self._engine.id}-stop", daemon=True)
            t.start()
        return True

    def status(self) -> dict:
        with self._lock:
            state = self._state
            snap = {"pid": self._spawn_proc.pid if self._spawn_proc else self._last_pid,
                    "model_id": self._model_id, "adopted": self._adopted,
                    "log_path": self._log_path, "loaded": self._loaded}
        if state in ("starting", "stopping"):
            return {"state": state, **snap}
        out = {"state": "idle", "pid": None, "model_id": None, "adopted": False,
               "log_path": snap["log_path"], "loaded": None}
        if not self.is_running():
            return out
        self._identity_probe()
        with self._probe_lock:
            confirmed = self._confirmed_at and (time.monotonic() - self._confirmed_at) <= _PROBE_VALID_S
            pid, model, loaded = self._probe_pid, self._probe_model, self._probe_loaded
        if not confirmed:
            return out
        ours = self._our_spawn_alive()
        out.update(state="active", pid=pid,
                   model_id=(self._model_id if ours and self._model_id else None) or model or self._model_id,
                   adopted=(self._adopted if ours else True), loaded=loaded)
        return out

    def protected_pids(self) -> set[int]:
        try:
            pids: set[int] = set()
            root, _foreign = self._locate_root(fallback=False)   # detector already protects by cmdline/name
            if root is not None:
                pids.update(p.pid for p in self._tree(root))
                cmd = self._launcher_cmd(root)
                if cmd is not None:
                    pids.add(cmd.pid)
            sp = self._spawn_proc
            if sp is not None:
                try:
                    if sp.is_running():
                        pids.update(p.pid for p in self._tree(sp))
                except psutil.Error:
                    pass
            return pids
        except Exception:  # noqa: BLE001 - contract: never raises
            log.exception("protected_pids failed")
            return set()

    @property
    def engine(self) -> Engine:
        return self._engine

    def engine_memory_mb(self) -> float | None:
        """Physical RAM held by the running engine tree: the sum of working sets.

        Not private commit: in full-RAM mode strata.exe commits far more than it has resident (measured: working set
        48.7 GB, private commit 63.3 GB on a 64 GB PC), so max(rss, private) booked the whole machine to Strata.
        None when no engine server is found. Cheap (no HTTP); call from a worker thread.
        """
        try:
            root, _foreign = self._locate_root(fallback=False)
            if root is None:
                return None
            total = 0
            for p in self._tree(root):
                try:
                    mi = p.memory_info()
                    total += mi.rss                        # working set = resident physical RAM
                except (psutil.Error, OSError):
                    continue
            return round(total / (1024 * 1024), 1)
        except Exception:  # noqa: BLE001
            log.debug("engine_memory_mb failed", exc_info=True)
            return None

    strata_memory_mb = engine_memory_mb          # the name used before engines existed

    # ------------------------------------------------------------------ HTTP
    def _http(self, method: str, path: str, timeout: float, extra_headers: dict | None = None) -> HttpReply:
        """One request to the engine's port (loopback, no proxy). Never raises; code None = no answer."""
        url = self._root_url + path
        data = None
        headers = {}
        if method == "POST":
            data = b"{}"
            headers = {"Content-Type": "application/json", "Content-Length": str(len(data))}
        headers.update(extra_headers or {})
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                code = resp.status
                hdrs = resp.headers
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001
                raw = b""
            code = e.code
            hdrs = e.headers
        except Exception as e:  # noqa: BLE001 - URLError, timeouts, resets
            log.debug("%s %s failed: %s", method, url, e)
            return HttpReply()
        try:
            body = json.loads(raw.decode("utf-8", "replace")) if raw else None
        except ValueError:
            body = None
        try:
            hmap = {str(k).lower(): str(v) for k, v in (hdrs.items() if hdrs is not None else ())}
        except Exception:  # noqa: BLE001
            hmap = {}
        return HttpReply(code=code, json=body, headers=hmap)

    def _http_id(self, method: str, path: str, timeout: float) -> HttpReply:
        """A request to a port ALREADY identified as the engine: adds the engine's own headers (an API key).
        The identity probe uses plain _http, so a foreign program on the port is never sent them."""
        try:
            extra = {str(k): str(v) for k, v in (self._engine.request_headers(self._cfg) or {}).items()}
        except Exception:  # noqa: BLE001
            extra = {}
        if extra and not self._probe(_PROBE_TIMEOUT_S).is_engine:
            # A process can be verified from process facts alone (listener PID hidden) while something else
            # answers on the port: the key only goes out when the port's own answer says it is the engine.
            log.warning("not sending the %s request headers: the port did not identify itself", self._engine.name)
            extra = {}
        return self._http(method, path, timeout, extra)

    def _probe(self, timeout: float = _HEALTH_TIMEOUT_S) -> ServiceState:
        """What the port says it is (engine.probe). Never raises: a failing engine probe counts as no answer."""
        try:
            st = self._engine.probe(self._http, timeout)
        except Exception:  # noqa: BLE001
            log.debug("engine probe failed", exc_info=True)
            return NO_ANSWER
        return st if isinstance(st, ServiceState) else NO_ANSWER

    def _fetch_model_id(self, state: ServiceState) -> str | None:
        try:
            return self._engine.fetch_model_id(self._http_id, state) or None
        except Exception:  # noqa: BLE001
            log.debug("model id lookup failed", exc_info=True)
            return state.model

    def _unload(self, force: bool) -> str:
        """Ask the engine to release the model. Engines without an unload call answer 'unsupported': for them
        the model is released by ending the process."""
        if not self._engine.can(CAP_UNLOAD):
            return UNLOAD_UNSUPPORTED
        try:
            return str(self._engine.unload(self._http_id, force))
        except Exception:  # noqa: BLE001
            log.debug("unload failed", exc_info=True)
            return UNLOAD_UNAVAILABLE

    def _engine_busy(self) -> bool:
        """engine.is_busy for engines without an unload call; unknown or an error = not busy (the stop goes on)."""
        try:
            return self._engine.is_busy(self._http_id) is True
        except Exception:  # noqa: BLE001
            log.debug("busy check failed", exc_info=True)
            return False

    def _is_engine_proc(self, proc: psutil.Process) -> bool:
        """STRICT process identity (engine.is_server_process); unknown = no."""
        try:
            return bool(self._engine.is_server_process(proc.name(), proc.cmdline()))
        except (psutil.Error, OSError):
            return False
        except Exception:  # noqa: BLE001 - an engine bug must never widen a kill
            log.exception("engine identity check failed")
            return False

    # ------------------------------------------------------------------ process helpers
    def _listener_pids(self) -> list[int] | None:
        try:
            conns = psutil.net_connections(kind="tcp")
        except Exception:  # noqa: BLE001
            return None
        port = self._port
        return sorted({c.pid for c in conns if c.pid and _is_listen_entry(c, port)})

    def _topmost_engine(self, proc: psutil.Process) -> psutil.Process:
        cur = proc
        for _ in range(64):
            try:
                par = cur.parent()
                if par is None or par.create_time() > cur.create_time() or not self._is_engine_proc(par):
                    break
            except (psutil.Error, OSError):
                break
            cur = par
        return cur

    def _locate_root(self, fallback: bool = True) -> tuple[psutil.Process | None, str | None]:
        """Return (top-most engine server process for our port, None) or (None, foreign-listener message)."""
        port = self._port
        pids = self._listener_pids()
        if pids:
            first_foreign = None
            for pid in pids:
                try:
                    p = psutil.Process(pid)
                except psutil.Error:
                    continue
                if self._is_engine_proc(p):
                    return self._topmost_engine(p), None
                if first_foreign is None:
                    first_foreign = p
            if first_foreign is not None:
                return None, self._not_engine_msg(port, first_foreign)
        if not fallback:
            return None, None
        sp = self._spawn_proc
        if sp is not None:
            try:
                if sp.is_running():
                    return sp, None
            except psutil.Error:
                pass
        found: list[psutil.Process] = []
        # NB: plain process_iter() + direct calls (not attrs/p.info) so a concurrent detector scan that shares
        # psutil's cached Process objects cannot clobber .info underneath either caller.
        for p in psutil.process_iter():
            try:
                cmd = p.cmdline() or []
                if not self._is_engine_proc(p):
                    continue
                try:
                    claims = bool(self._engine.claims_port(cmd, port, self._cfg))
                except Exception:  # noqa: BLE001 - an engine bug: unknown = not ours
                    log.exception("engine port check failed")
                    claims = False
                if claims:
                    found.append(p)
            except (psutil.Error, OSError, ValueError):
                continue
        if found:
            roots = {}
            for p in found:
                r = self._topmost_engine(p)
                roots[r.pid] = r
            def _ctime(r: psutil.Process) -> float:
                try:
                    return r.create_time()
                except psutil.Error:
                    return float("inf")
            return min(roots.values(), key=_ctime), None
        return None, None

    @staticmethod
    def _tree(root: psutil.Process) -> list[psutil.Process]:
        procs = [root]
        try:
            procs.extend(root.children(recursive=True))  # psutil validates create_time ordering
        except (psutil.Error, OSError):
            pass
        return procs

    def _whitelist(self) -> set[str]:
        return {_norm_name(n) for n in (self._cfg.get("user_whitelist") or []) if isinstance(n, str) and n.strip()}

    def _kill_targets(self, root: psutil.Process, log_skips: bool = True) -> list[psutil.Process]:
        """The part of root's tree that OFF may kill. `root` (already identified as the engine) is always kept; a
        descendant is kept only if it is the engine's: a server process (engine.is_server_process), a helper the
        engine claims (engine.owns_child - for Strata an exe under strata_dir: engine\\strata.exe,
        strata-vision.exe, the venv python), or a conhost.exe whose parent is kept.
        Everything else - e.g. the browser that `--open` / webbrowser.open() starts as a child of the server -
        is left running, and so is any process whose name is in user_whitelist (unless its cmdline identifies it
        as the engine server itself). The unfiltered _tree() stays in use where processes are only PROTECTED.
        The engine only answers yes/no about members of root's tree: it cannot add a process from outside it."""
        tree = self._tree(root)
        wl = self._whitelist()
        kept: set[int] = {root.pid}
        pending: list[tuple[psutil.Process, str]] = []
        skipped: list[tuple[str, int, str]] = []
        for p in tree[1:]:
            try:
                name = p.name()
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError):
                name = ""
            if self._is_engine_proc(p):
                kept.add(p.pid)
                continue
            if _norm_name(name) in wl:
                skipped.append((name, p.pid, "user_whitelist"))
                continue
            try:
                owned = bool(self._engine.owns_child(p, self._cfg))
            except Exception:  # noqa: BLE001 - unknown = not ours
                owned = False
            if owned:
                kept.add(p.pid)
                continue
            pending.append((p, name))
        changed = True
        while changed and pending:                      # conhost.exe of a kept process (any depth of kept chain)
            changed = False
            for item in list(pending):
                p, name = item
                if _norm_name(name) != "conhost":
                    continue
                try:
                    ppid = p.ppid()
                except (psutil.Error, OSError):
                    continue
                if ppid in kept:
                    kept.add(p.pid)
                    pending.remove(item)
                    changed = True
        skipped.extend((name or "unknown process", p.pid, f"not {self._engine.name}") for p, name in pending)
        if log_skips:
            for name, pid, why in skipped:
                log.info("stop: leaving %s (pid %s) running - %s", name, pid, why)
        return [p for p in tree if p.pid in kept]

    def _is_own_spawn(self, proc: psutil.Process | None) -> bool:
        sp = self._spawn_proc
        try:
            return proc is not None and sp is not None and proc == sp   # psutil eq = pid + create time
        except psutil.Error:
            return False

    def _verified_engine(self, root: psutil.Process, state: ServiceState | None = None,
                         probe: bool = True) -> bool:
        """Kill/unload-grade identity for a process _locate_root() matched by cmdline: the server we spawned,
        OR the engine proves it from process facts (Strata: its serve\\server.py lives under strata_dir), OR
        the port's own answer identifies the engine (Strata: GET /health says service == 'strata').
        A different project's look-alike on the port passes none of these."""
        if self._is_own_spawn(root):
            return True
        try:
            if self._engine.verified_by_process(root, self._cfg):
                return True
        except Exception:  # noqa: BLE001 - unknown = not verified
            log.exception("engine verification failed")
        if state is None and probe:
            state = self._probe()
        return state is not None and state.is_engine

    def _not_engine_msg(self, port: int, proc: psutil.Process) -> str:
        return f"port {port} held by {_proc_label(proc)} (pid {proc.pid}), not {self._engine.name}"

    def _launcher_cmd(self, root: psutil.Process) -> psutil.Process | None:
        """The wrapper process that launched `root` and ends with it (Strata: cmd.exe /c "...\\run-*.bat"), as
        named by the engine - accepted only if it really is root's direct, older parent."""
        try:
            cand = self._engine.launcher_process(root, self._cfg)
            if cand is None:
                return None
            par = root.parent()
            if par is None or cand != par or cand.pid != par.pid or par.create_time() > root.create_time():
                log.warning("stop: ignoring launcher %r - not the direct parent of pid %s", cand, root.pid)
                return None
            return par
        except (psutil.Error, OSError):
            return None
        except Exception:  # noqa: BLE001 - an engine bug must never widen a kill
            log.exception("engine launcher lookup failed")
            return None

    @staticmethod
    def _kill(procs: list[psutil.Process]) -> tuple[list[int], list[str]]:
        """Terminate in order (root first), wait, kill survivors. Returns (gone pids, error strings)."""
        targets: list[psutil.Process] = []
        errors: list[str] = []
        for p in procs:
            try:
                if p.is_running():
                    p.terminate()
                    targets.append(p)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied as e:
                errors.append(f"pid {p.pid}: access denied ({e})")
        _gone, alive = psutil.wait_procs(targets, timeout=5)
        for p in alive:
            try:
                if p.is_running():
                    p.kill()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied as e:
                errors.append(f"pid {p.pid}: access denied ({e})")
        _gone2, still = psutil.wait_procs(alive, timeout=5)
        still_pids = {p.pid for p in still}
        for p in still:
            errors.append(f"pid {p.pid} survived kill")
        killed = [p.pid for p in targets if p.pid not in still_pids]
        return killed, errors

    def _our_spawn_alive(self) -> bool:
        sp = self._spawn_proc
        try:
            return sp is not None and sp.is_running()
        except psutil.Error:
            return False

    def _wait_port_released(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if not self.is_running():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    # ------------------------------------------------------------------ logs
    def _new_log_path(self) -> Path:
        d = self._log_dir if self._log_dir is not None else Path(config_store.LOG_DIR or LOG_DIR)
        d.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = d / f"{self._engine.log_prefix}-{stamp}.log"
        n = 1
        while path.exists():
            path = d / f"{self._engine.log_prefix}-{stamp}-{n}.log"
            n += 1
        return path

    def _prune_logs(self, d: Path, keep_path: Path) -> None:
        try:
            files = []
            for f in d.glob(f"{self._engine.log_prefix}-*.log"):
                try:
                    files.append((f.stat().st_mtime, f.name, f))
                except OSError:
                    continue
            files.sort(reverse=True)
            survivors = [keep_path] + [f for _m, _n, f in files if f != keep_path]
            for f in survivors[_LOG_KEEP:]:
                try:
                    f.unlink()
                except OSError as e:
                    log.info("could not prune %s: %s", f, e)
        except Exception:  # noqa: BLE001 - pruning is best-effort
            log.exception("log pruning failed")

    # ------------------------------------------------------------------ callbacks
    @staticmethod
    def _fire(cb: Callable, *args) -> None:
        try:
            cb(*args)
        except Exception:  # noqa: BLE001 - contract: callback exceptions are caught + logged
            log.exception("callback %r raised", cb)

    # ------------------------------------------------------------------ start worker
    def _start_worker(self, on_ready: Callable[[], None], on_error: Callable[[str], None]) -> None:
        abort = self._abort
        outcome: tuple[str, str] | None
        try:
            outcome = self._do_start(abort)
        except Exception as e:  # noqa: BLE001
            log.exception("start worker failed")
            outcome = ("error", f"start failed: {e}")
            try:
                if not abort.is_set() and self._our_spawn_alive() and self._spawn_proc is not None:
                    killed, errs = self._kill(self._kill_targets(self._spawn_proc))
                    log.warning("start failure: killed spawned tree %s %s", killed, errs)
            except Exception:  # noqa: BLE001
                log.exception("cleanup after start failure failed")
        with self._lock:
            suppressed = abort.is_set() or outcome is None
            if not suppressed:
                self._state = "idle"
        if suppressed:
            log.info("start worker finished after cancel; callbacks suppressed")
            return
        kind, msg = outcome
        if kind == "ready":
            log.info("%s ready: model=%s adopted=%s", self._engine.name, self._model_id, self._adopted)
            self._fire(on_ready)
        else:
            log.warning("%s start failed: %s", self._engine.name, msg)
            self._fire(on_error, msg)

    def _do_start(self, abort: threading.Event) -> tuple[str, str] | None:
        if self.is_running():
            return self._adopt(abort)
        name = self._engine.name
        try:
            plan = self._engine.plan_launch(self._cfg)
        except LaunchError as e:
            return "error", str(e)
        cmd, cwd = [str(x) for x in plan.cmd], plan.cwd
        if not cmd or not cmd[0].strip().casefold().endswith(".exe") or not os.path.isfile(cmd[0]):
            # whatever the engine planned: never hand a .bat/.cmd/script to CreateProcess (it runs via cmd.exe)
            return "error", f"launch command is not an existing .exe: {cmd[0] if cmd else '(empty)'}"
        log_path = self._new_log_path()
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (plan.env or {}).items()})
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        with self._lock:
            if abort.is_set():
                return None
            try:
                fh = open(log_path, "ab")
            except OSError as e:
                return "error", f"cannot create log {log_path}: {e}"
            try:
                popen = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fh,
                                         stderr=subprocess.STDOUT, creationflags=flags, env=env)
            except OSError as e:
                return "error", f"failed to launch {name}: {e}"
            finally:
                fh.close()
            self._popen = popen
            try:
                self._spawn_proc = psutil.Process(popen.pid)
            except psutil.Error:
                self._spawn_proc = None
            self._log_path = str(log_path)
            self._last_pid = popen.pid
        self._prune_logs(log_path.parent, log_path)
        log.info("launched %s pid=%s log=%s", name, popen.pid, log_path)

        timeout = self._timeout_cfg("ready_timeout_s", 180)
        deadline = time.monotonic() + timeout
        unloaded_since: float | None = None
        nudged = False
        while True:
            if abort.is_set():
                return None
            rc = popen.poll()
            if rc is not None:
                return "error", f"{name} exited (code {rc}) — see {log_path}"
            h = self._probe()
            if h.is_engine:
                loaded = h.loaded is True
                with self._lock:
                    self._loaded = loaded
                if loaded:
                    model = self._fetch_model_id(h)
                    with self._lock:
                        self._model_id = model or str(self._cfg.get("model_name") or "") or None
                        self._adopted = False
                    self._prime_probe(self._model_id)
                    return "ready", ""
                now = time.monotonic()
                if unloaded_since is None:
                    unloaded_since = now
                elif not nudged and now - unloaded_since > self._nudge_s and self._engine.can(CAP_LOAD_NUDGE):
                    nudged = True
                    try:
                        code = self._engine.nudge_load(self._http_id)
                    except Exception:  # noqa: BLE001
                        code = None
                    log.info("sent POST /load (status %s)", code)
            if time.monotonic() >= deadline:
                if abort.is_set():
                    return None
                if self._spawn_proc is not None:
                    killed, errs = self._kill(self._kill_targets(self._spawn_proc))
                    log.warning("ready timeout: killed %s %s", killed, errs)
                return "error", f"{name} not ready after {timeout:g} s — see {log_path}"
            abort.wait(self._poll_s)

    def _adopt(self, abort: threading.Event) -> tuple[str, str] | None:
        port = self._port
        timeout = self._timeout_cfg("ready_timeout_s", 180)
        deadline = time.monotonic() + timeout
        with self._lock:
            self._spawn_proc = None
            self._popen = None
        while True:
            if abort.is_set():
                return None
            if not self.is_running():
                return "error", f"port {port} stopped listening while waiting for {self._engine.name}"
            h = self._probe()
            health_strata = h.is_engine
            root, foreign = self._locate_root(fallback=False)
            if foreign and not health_strata:
                return "error", foreign
            if root is not None and h.answered and not health_strata \
                    and not self._verified_engine(root, h, probe=False):
                return "error", self._not_engine_msg(port, root)   # another project's look-alike server
            if root is not None:
                with self._lock:
                    self._last_pid = root.pid
            if health_strata:
                loaded = h.loaded is True
                with self._lock:
                    self._loaded = loaded
                if loaded:
                    model = self._fetch_model_id(h)
                    with self._lock:
                        self._model_id = model
                        self._adopted = True
                    self._prime_probe(model)
                    return "ready", ""
            if time.monotonic() >= deadline:
                return "error", (f"{self._engine.name} already running on port {port} but not ready after "
                                 f"{timeout:g} s (left running)")
            abort.wait(self._poll_s)

    # ------------------------------------------------------------------ stop worker
    def _new_result(self, cancelling: bool) -> dict:
        # "released": the engine no longer holds the model - the engine-neutral fact the UI acts on. For an
        # engine that is a process we end (Strata) it equals "port_released", which stays for diagnostics.
        return {"ok": False, "released": False, "port_released": False, "killed_pids": [], "ram_freed_mb": 0.0,
                "unload": "skipped", "error": None, "cancelled_start": cancelling}

    def _stop_worker(self, cb: Callable[[dict], None], force: bool, cancelling: bool,
                     start_thread: threading.Thread | None) -> None:
        result = self._new_result(cancelling)
        try:
            if cancelling:
                self._do_cancel(result, start_thread)
            else:
                self._do_stop(result, force)
        except Exception as e:  # noqa: BLE001
            log.exception("stop worker failed")
            result["ok"] = False
            result["error"] = f"stop failed: {e}"
        result["released"] = bool(result.get("port_released"))
        with self._lock:
            self._state = "idle"
        log.info("stop result: %s", result)
        self._fire(cb, result)

    @staticmethod
    def _rss_mb(procs: list[psutil.Process]) -> float:
        total = 0
        for p in procs:
            try:
                total += p.memory_info().rss
            except (psutil.Error, OSError):
                continue
        return round(total / (1024 * 1024), 1)

    def _do_cancel(self, result: dict, start_thread: threading.Thread | None) -> None:
        if start_thread is not None and start_thread is not threading.current_thread():
            start_thread.join(timeout=20)
        sp = self._spawn_proc
        errors: list[str] = []
        if sp is not None and self._our_spawn_alive():
            procs = self._kill_targets(sp)
            result["ram_freed_mb"] = self._rss_mb(procs)
            killed, errors = self._kill(procs)
            result["killed_pids"] = killed
            result["port_released"] = self._wait_port_released(self._timeout_cfg("stop_timeout_s", 20))
        else:
            # nothing spawned (still validating, or adopting an external server we never kill)
            result["port_released"] = not self.is_running()
        with self._lock:
            self._spawn_proc = None
            self._popen = None
            self._model_id = None
            self._adopted = False
            self._loaded = None
        result["ok"] = not errors
        result["error"] = "; ".join(errors) or None

    def _do_stop(self, result: dict, force: bool) -> None:
        root, foreign = self._locate_root(fallback=True)
        if foreign:
            result["error"] = foreign
            return
        if root is not None and not self._verified_engine(root):
            # a look-alike (Strata: a python running SOME serve\\server.py that is not under strata_dir and whose
            # /health is not Strata): never unload or kill it
            result["error"] = self._not_engine_msg(self._port, root)
            result["port_released"] = not self.is_running()
            log.warning("stop refused: %s", result["error"])
            return
        # measure BEFORE /unload: the unload itself releases the model, so a post-unload RSS reads ~0.2 GB
        pre_unload_mb = self._rss_mb(self._kill_targets(root, log_skips=False)) if root is not None else 0.0
        avail_before = _avail_mb()
        if root is None:
            # No engine process found for the port (listener PID hidden / not ours). Only talk to the port if
            # its own answer says it IS the engine - never send an unload to an unidentified service.
            if not self._probe().is_engine:
                result["unload"] = "unavailable"
                result["port_released"] = not self.is_running()
                result["ok"] = result["port_released"]
                if not result["ok"]:
                    result["error"] = f"could not identify a {self._engine.name} server on port {self._port}"
                return
        result["unload"] = self._unload(force)
        if result["unload"] == UNLOAD_UNSUPPORTED and not force and self._engine_busy():
            result["unload"] = UNLOAD_BUSY          # no unload call to refuse for it: the engine says a reply runs
        if result["unload"] == UNLOAD_BUSY and not force:
            result["error"] = "busy"
            return
        stop_timeout = self._timeout_cfg("stop_timeout_s", 20)
        errors: list[str] = []
        if root is not None:
            procs = self._kill_targets(root)
            cmd = self._launcher_cmd(root)
            result["ram_freed_mb"] = max(pre_unload_mb, self._rss_mb(procs))
            if cmd is not None:
                procs = procs + [cmd]
            killed, errors = self._kill(procs)
            result["killed_pids"] = killed
        else:
            errors.append(f"could not locate the {self._engine.name} process for port {self._port}")
        released = self._wait_port_released(stop_timeout)
        result["port_released"] = released
        if released and avail_before is not None:
            # Strata's expert pages are largely not charged to its own RSS (verified: RSS read ~0.2 GB while
            # system free RAM rose ~13.5 GB on OFF), so report the measured system-wide change as well.
            time.sleep(1.0)
            avail_after = _avail_mb()
            if avail_after is not None:
                result["ram_freed_mb"] = round(max(result["ram_freed_mb"], avail_after - avail_before), 1)
        model = self._model_id or self._probe_model or str(self._cfg.get("model_name") or "") or "model"
        with self._lock:
            self._spawn_proc = None
            self._popen = None
            self._model_id = None
            self._adopted = False
            self._loaded = None
        with self._probe_lock:
            self._confirmed_at = 0.0
            self._probe_at = 0.0
        if released:
            result["ok"] = True
            result["error"] = None
            if errors:
                log.warning("stop: port released but: %s", "; ".join(errors))
            note, self.next_toast = self.next_toast, None
            if self._notify:
                self._toast(model, note, result.get("ram_freed_mb"))
        else:
            result["ok"] = False
            errors.append(f"port {self._port} still in use after {stop_timeout:g} s")
            result["error"] = "; ".join(errors)

    def _toast(self, model: str, note: tuple[str, str] | None = None, freed_mb: float | None = None) -> None:
        name = self._engine.name
        title, msg = f"{name} stopped", f"{name} stopped: {model} unloaded and memory freed."
        if note:
            freed = f"{freed_mb / 1024:.1f} GB" if isinstance(freed_mb, (int, float)) and freed_mb > 0 else "its memory"
            try:
                title, msg = note[0], note[1].replace("{freed}", freed).replace("{model}", model)
            except Exception:  # noqa: BLE001
                pass
        try:
            import toasts                         # data-only toasts (winotify pasted text into PowerShell)
            icon = config_store.APP_DIR / "assets" / "app.png"
            # Installed build: the Start-menu shortcut carries this AUMID, so the toast shows the app's
            # name + icon. From source there is no shortcut, so use a plain display name.
            app_id = "warrenjbg.AgentStart" if config_store.FROZEN else "Agent Start"
            if toasts.show(title, msg, app_id, icon=str(icon) if icon.exists() else ""):
                self.last_toast = msg
        except Exception:  # noqa: BLE001 - toast failure is non-fatal
            log.exception("toast failed")

    # ------------------------------------------------------------------ status probe
    def _prime_probe(self, model: str | None) -> None:
        """Mark identity as just confirmed (called on ready) so the first status() after a start cannot
        report idle while another thread's probe is still in flight."""
        try:
            root, _foreign = self._locate_root(fallback=False)
        except Exception:  # noqa: BLE001
            root = None
        now = time.monotonic()
        with self._probe_lock:
            self._confirmed_at = now
            self._probe_at = now
            self._probe_loaded = True
            self._probe_pid = root.pid if root is not None else self._probe_pid
            if model:
                self._probe_model = model

    def _identity_probe(self) -> None:
        # One probe at a time: a second caller waits for the in-flight probe and then reads its result,
        # instead of returning early with a stale/zero _confirmed_at (which flipped the UI to IDLE).
        with self._probe_run:
            now = time.monotonic()
            with self._probe_lock:
                if self._probe_at and now - self._probe_at < _PROBE_MIN_INTERVAL_S:
                    return
                self._probe_at = now
            self._identity_probe_body()

    def _identity_probe_body(self) -> None:
        h = self._probe(_PROBE_TIMEOUT_S)
        root, _foreign = self._locate_root(fallback=False)
        health_strata = h.is_engine
        strata = health_strata or (root is not None and self._verified_engine(root, h, probe=False))
        with self._probe_lock:
            if strata:
                self._confirmed_at = time.monotonic()
                self._probe_pid = root.pid if root is not None else None
                self._probe_model = h.model or None
                self._probe_loaded = (h.loaded is True) if h.answered else None
            else:
                self._confirmed_at = 0.0
