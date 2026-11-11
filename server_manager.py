"""Start / stop / monitor the local Strata inference server (Contract 2 of SPEC.md).

`EndpointManager` launches Strata, waits until the model is loaded, and tears it down again (unload, kill the
process tree, verify the port is released, toast). All slow work runs on daemon worker threads; results are
delivered through callbacks invoked on those worker threads.
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import json
import logging
import os
import re
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

log = logging.getLogger("hermes_controller")

_STRATA_MARK = os.path.normcase("serve\\server.py")
_LOG_KEEP = 5
_DEFAULT_PORT = 8080
_HEALTH_TIMEOUT_S = 2.0
_PROBE_TIMEOUT_S = 1.0
_PROBE_MIN_INTERVAL_S = 5.0
_PROBE_VALID_S = 10.0
_RUN_BAT_ABS_RE = re.compile(r'"([A-Za-z]:[\\/][^"]*?run-[^"\\/]*\.bat)"|([A-Za-z]:[\\/][^\s"]*?run-[^\s"\\/]*\.bat)',
                             re.IGNORECASE)
_RUN_BAT_REL_RE = re.compile(r"(?:^|[\s\"])(run-[^\s\"\\/<>|*?]*\.bat)", re.IGNORECASE)
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


_PY_NAMES = frozenset({"python", "pythonw", "py", "pyw"})


def _is_strata_cmd(name: str | None, cmd: list[str] | None) -> bool:
    """STRICT identity for the kill path: a python interpreter whose argv (after argv[0]) has a token that ENDS
    with serve\\server.py. A substring match on the joined cmdline would also match e.g.
    `hermes chat -q "... serve\\server.py ..."`, `bash -c "python serve/server.py"`, or an editor with the file
    open - and those (plus their whole trees) would be killed on OFF. The detector keeps the loose match because
    there it only PROTECTS."""
    n = (name or "").casefold()
    if n.endswith(".exe"):
        n = n[:-4]
    if n not in _PY_NAMES and not n.startswith("python"):
        return False
    def _hit(tok: str) -> bool:
        t = os.path.normcase(str(tok).strip('"'))
        return t == _STRATA_MARK or t.endswith(os.sep + _STRATA_MARK)   # whole path segments only
    return any(_hit(t) for t in (cmd or [])[1:])


def _is_strata_proc(proc: psutil.Process) -> bool:
    try:
        return _is_strata_cmd(proc.name(), proc.cmdline())
    except (psutil.Error, OSError):
        return False


def _avail_mb() -> float | None:
    try:
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None


def _explicit_port(cmd: list[str]) -> int | None:
    """`--port N` / `--port=N` if present, else None (no default - used where a guess could kill)."""
    for i, tok in enumerate(cmd):
        if tok == "--port" and i + 1 < len(cmd):
            try:
                return int(cmd[i + 1])
            except ValueError:
                return None
        if tok.startswith("--port="):
            try:
                return int(tok.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _proc_label(proc: psutil.Process) -> str:
    try:
        return proc.name()
    except (psutil.Error, OSError):
        return "unknown process"


class EndpointManager:
    """Owns the lifecycle of one Strata server on `config["port"]`.

    Extra keyword-only knobs (defaults match SPEC.md; tests shorten them):
      log_dir          directory for strata-*.log files (default: config_store.LOG_DIR, read at spawn time)
      poll_interval_s  readiness poll period while starting (default 1.0)
      load_nudge_s     seconds of `loaded: false` before the single POST /load (default 15.0)
    """

    def __init__(self, config: dict | None = None, notify: bool = True, *,
                 log_dir: str | os.PathLike | None = None,
                 poll_interval_s: float = 1.0,
                 load_nudge_s: float = 15.0) -> None:
        self._cfg = config if config is not None else load_config()
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

    # ------------------------------------------------------------------ config helpers
    @property
    def _port(self) -> int:
        try:
            return int(self._cfg.get("port", _DEFAULT_PORT))
        except (TypeError, ValueError):
            return _DEFAULT_PORT

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
                                 name="strata-start", daemon=True)
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
                                 name="strata-stop", daemon=True)
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

    def strata_memory_mb(self) -> float | None:
        """RAM charged to the running Strata tree: sum over processes of max(working set, private commit).

        None when no Strata server is found. Cheap (no HTTP); call from a worker thread.
        """
        try:
            root, _foreign = self._locate_root(fallback=False)
            if root is None:
                return None
            total = 0
            for p in self._tree(root):
                try:
                    mi = p.memory_info()
                    total += max(mi.rss, getattr(mi, "private", 0) or 0)
                except (psutil.Error, OSError):
                    continue
            return round(total / (1024 * 1024), 1)
        except Exception:  # noqa: BLE001
            log.debug("strata_memory_mb failed", exc_info=True)
            return None

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, timeout: float) -> tuple[int | None, object]:
        url = self._root_url + path
        data = None
        headers = {}
        if method == "POST":
            data = b"{}"
            headers = {"Content-Type": "application/json", "Content-Length": str(len(data))}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001
                raw = b""
            code = e.code
        except Exception as e:  # noqa: BLE001 - URLError, timeouts, resets
            log.debug("%s %s failed: %s", method, url, e)
            return None, None
        try:
            body = json.loads(raw.decode("utf-8", "replace")) if raw else None
        except ValueError:
            body = None
        return code, body

    def _health(self, timeout: float = _HEALTH_TIMEOUT_S) -> dict | None:
        code, body = self._request("GET", "/health", timeout)
        if code == 200 and isinstance(body, dict):
            return body
        return None

    def _fetch_model_id(self, health: dict | None) -> str | None:
        code, body = self._request("GET", "/v1/models", _HEALTH_TIMEOUT_S)
        if code == 200 and isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
                return str(data[0]["id"])
        if health and health.get("model"):
            return str(health["model"])
        return None

    def _unload(self, force: bool) -> str:
        if force:
            code, _ = self._request("POST", "/unload", 5.0)
        else:
            code, _ = self._request("POST", "/unload?wait=10", 15.0)
        if code == 200:
            return "ok"
        if code == 409:
            return "busy"
        return "unavailable"

    # ------------------------------------------------------------------ process helpers
    def _listener_pids(self) -> list[int] | None:
        try:
            conns = psutil.net_connections(kind="tcp")
        except Exception:  # noqa: BLE001
            return None
        port = self._port
        return sorted({c.pid for c in conns if c.pid and _is_listen_entry(c, port)})

    @staticmethod
    def _topmost_strata(proc: psutil.Process) -> psutil.Process:
        cur = proc
        for _ in range(64):
            try:
                par = cur.parent()
                if par is None or par.create_time() > cur.create_time() or not _is_strata_proc(par):
                    break
            except (psutil.Error, OSError):
                break
            cur = par
        return cur

    def _locate_root(self, fallback: bool = True) -> tuple[psutil.Process | None, str | None]:
        """Return (top-most Strata server process for our port, None) or (None, foreign-listener message)."""
        port = self._port
        pids = self._listener_pids()
        if pids:
            first_foreign = None
            for pid in pids:
                try:
                    p = psutil.Process(pid)
                except psutil.Error:
                    continue
                if _is_strata_proc(p):
                    return self._topmost_strata(p), None
                if first_foreign is None:
                    first_foreign = p
            if first_foreign is not None:
                return None, (f"port {port} held by {_proc_label(first_foreign)} "
                              f"(pid {first_foreign.pid}), not Strata")
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
        sdir = str(self._cfg.get("strata_dir") or "")
        sdir_n = os.path.normcase(os.path.abspath(sdir)).rstrip(os.sep) + os.sep if sdir else ""
        # NB: plain process_iter() + direct calls (not attrs/p.info) so a concurrent detector scan that shares
        # psutil's cached Process objects cannot clobber .info underneath either caller.
        for p in psutil.process_iter():
            try:
                cmd = p.cmdline() or []
                if not _is_strata_cmd(p.name(), cmd):
                    continue
                port_ok = _explicit_port(cmd) == port
                in_dir = bool(sdir_n) and any(os.path.normcase(os.path.abspath(str(t).strip('"'))).startswith(sdir_n)
                                              for t in cmd[1:] if str(t).casefold().endswith(".py"))
                if port_ok or (in_dir and _explicit_port(cmd) in (None, port) and port == _DEFAULT_PORT):
                    found.append(p)
            except (psutil.Error, OSError, ValueError):
                continue
        if found:
            roots = {}
            for p in found:
                r = self._topmost_strata(p)
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

    def _launcher_cmd(self, root: psutil.Process) -> psutil.Process | None:
        """cmd.exe /c "<strata_dir>\\run-*.bat" that launched `root`, else None."""
        sdir = str(self._cfg.get("strata_dir") or "").strip()
        if not sdir:
            return None
        prefix = os.path.normcase(os.path.abspath(sdir)) + os.sep
        try:
            par = root.parent()
            if par is None or par.create_time() > root.create_time():
                return None
            from_name = par.name().casefold()
            if from_name.endswith(".exe"):
                from_name = from_name[:-4]
            if from_name != "cmd":
                return None
            joined = " ".join(par.cmdline() or [])
        except (psutil.Error, OSError):
            return None
        if not re.search(r"(^|\s)/c(\s|$)", joined, re.IGNORECASE):
            return None
        cands = [m.group(1) or m.group(2) for m in _RUN_BAT_ABS_RE.finditer(joined)]
        if not cands:
            try:
                cwd = par.cwd()
            except (psutil.Error, OSError):
                cwd = ""
            if cwd:
                cands = [os.path.join(cwd, m.group(1)) for m in _RUN_BAT_REL_RE.finditer(joined)]
        for cand in cands:
            try:
                full = os.path.normcase(os.path.abspath(cand.strip()))
            except (OSError, ValueError):
                continue
            if full.startswith(prefix):
                return par
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
        path = d / f"strata-{stamp}.log"
        n = 1
        while path.exists():
            path = d / f"strata-{stamp}-{n}.log"
            n += 1
        return path

    @staticmethod
    def _prune_logs(d: Path, keep_path: Path) -> None:
        try:
            files = []
            for f in d.glob("strata-*.log"):
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
                    killed, errs = self._kill(self._tree(self._spawn_proc))
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
            log.info("Strata ready: model=%s adopted=%s", self._model_id, self._adopted)
            self._fire(on_ready)
        else:
            log.warning("Strata start failed: %s", msg)
            self._fire(on_error, msg)

    def _do_start(self, abort: threading.Event) -> tuple[str, str] | None:
        if self.is_running():
            return self._adopt(abort)
        cmd = [str(x) for x in (self._cfg.get("launch_cmd") or [])]
        if not cmd:
            return "error", "launch_cmd is empty in config.json"
        if not os.path.isfile(cmd[0]):
            return "error", f"launcher not found: {cmd[0]}"
        sdir = str(self._cfg.get("strata_dir") or "")
        if not sdir or not os.path.isdir(sdir):
            return "error", f"strata_dir not found: {sdir or '(empty)'}"
        log_path = self._new_log_path()
        env = dict(os.environ)
        env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        with self._lock:
            if abort.is_set():
                return None
            try:
                fh = open(log_path, "ab")
            except OSError as e:
                return "error", f"cannot create log {log_path}: {e}"
            try:
                popen = subprocess.Popen(cmd, cwd=sdir, stdin=subprocess.DEVNULL, stdout=fh,
                                         stderr=subprocess.STDOUT, creationflags=flags, env=env)
            except OSError as e:
                return "error", f"failed to launch Strata: {e}"
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
        log.info("launched Strata pid=%s log=%s", popen.pid, log_path)

        timeout = self._timeout_cfg("ready_timeout_s", 180)
        deadline = time.monotonic() + timeout
        unloaded_since: float | None = None
        nudged = False
        while True:
            if abort.is_set():
                return None
            rc = popen.poll()
            if rc is not None:
                return "error", f"Strata exited (code {rc}) — see {log_path}"
            h = self._health()
            if h is not None and h.get("service") == "strata":
                loaded = h.get("loaded") is True
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
                elif not nudged and now - unloaded_since > self._nudge_s:
                    nudged = True
                    code, _ = self._request("POST", "/load", 3.0)
                    log.info("sent POST /load (status %s)", code)
            if time.monotonic() >= deadline:
                if abort.is_set():
                    return None
                if self._spawn_proc is not None:
                    killed, errs = self._kill(self._tree(self._spawn_proc))
                    log.warning("ready timeout: killed %s %s", killed, errs)
                return "error", f"Strata not ready after {timeout:g} s — see {log_path}"
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
                return "error", f"port {port} stopped listening while waiting for Strata"
            h = self._health()
            health_strata = h is not None and h.get("service") == "strata"
            root, foreign = self._locate_root(fallback=False)
            if foreign and not health_strata:
                return "error", foreign
            if root is not None:
                with self._lock:
                    self._last_pid = root.pid
            if health_strata:
                loaded = h.get("loaded") is True
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
                return "error", (f"Strata already running on port {port} but not ready after {timeout:g} s "
                                 f"(left running)")
            abort.wait(self._poll_s)

    # ------------------------------------------------------------------ stop worker
    def _new_result(self, cancelling: bool) -> dict:
        return {"ok": False, "port_released": False, "killed_pids": [], "ram_freed_mb": 0.0,
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
            procs = self._tree(sp)
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
        # measure BEFORE /unload: the unload itself releases the model, so a post-unload RSS reads ~0.2 GB
        pre_unload_mb = self._rss_mb(self._tree(root)) if root is not None else 0.0
        avail_before = _avail_mb()
        result["unload"] = self._unload(force)
        if result["unload"] == "busy" and not force:
            result["error"] = "busy"
            return
        stop_timeout = self._timeout_cfg("stop_timeout_s", 20)
        errors: list[str] = []
        if root is not None:
            procs = self._tree(root)
            cmd = self._launcher_cmd(root)
            result["ram_freed_mb"] = max(pre_unload_mb, self._rss_mb(procs))
            if cmd is not None:
                procs = procs + [cmd]
            killed, errors = self._kill(procs)
            result["killed_pids"] = killed
        else:
            errors.append(f"could not locate the Strata process for port {self._port}")
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
            if self._notify:
                self._toast(model)
        else:
            result["ok"] = False
            errors.append(f"port {self._port} still in use after {stop_timeout:g} s")
            result["error"] = "; ".join(errors)

    def _toast(self, model: str) -> None:
        msg = f"Hermes Endpoint Closed: {model} unloaded and memory freed."
        try:
            from winotify import Notification
            icon = config_store.APP_DIR / "assets" / "app.png"
            # Installed build: the Start-menu shortcut carries this AUMID, so the toast shows the app's
            # name + icon. From source there is no shortcut, so use a plain display name.
            app_id = "warrenjbg.HermesController" if config_store.FROZEN else "Hermes Controller"
            Notification(app_id=app_id, title="Hermes Endpoint Closed", msg=msg,
                         icon=str(icon) if icon.exists() else "").show()
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
        h = self._health(_PROBE_TIMEOUT_S)
        root, _foreign = self._locate_root(fallback=False)
        strata = (h is not None and h.get("service") == "strata") or root is not None
        with self._probe_lock:
            if strata:
                self._confirmed_at = time.monotonic()
                self._probe_pid = root.pid if root is not None else None
                self._probe_model = str(h.get("model")) if h and h.get("model") else None
                self._probe_loaded = (h.get("loaded") is True) if h is not None else None
            else:
                self._confirmed_at = 0.0
