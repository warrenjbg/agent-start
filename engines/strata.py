"""Strata as an engine: the only place that knows what a Strata server looks like.

Identity: a python interpreter whose argv has a token ending in serve\\server.py, verified by that script living
under the configured `strata_dir` or by GET /health answering `service == "strata"`.
The UI features delegate to the existing Strata modules (launcher_bat, strata_finder, engine_log,
load_progress, ram_plan, metrics_store), which stay where they are.
"""
from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .base import (
    CAP_FIND_INSTALL,
    CAP_LAUNCHER_FILES,
    CAP_LIVE_ACTIVITY,
    CAP_LOAD_NUDGE,
    CAP_LOAD_PROGRESS,
    CAP_RAM_MODES,
    CAP_REQUEST_END,
    CAP_REQUEST_HISTORY,
    CAP_UNLOAD,
    NO_ANSWER,
    UNLOAD_BUSY,
    UNLOAD_OK,
    UNLOAD_UNAVAILABLE,
    Engine,
    Http,
    LaunchError,
    LaunchPlan,
    LoadReport,
    MetricsPoller,
    ServiceState,
    dir_prefix,
    explicit_port,
    path_under,
)

if TYPE_CHECKING:
    import psutil

SERVER_MARK = os.path.normcase("serve\\server.py")
_PY_NAMES = frozenset({"python", "pythonw", "py", "pyw"})
_RUN_BAT_ABS_RE = re.compile(r'"([A-Za-z]:[\\/][^"]*?run-[^"\\/]*\.bat)"|([A-Za-z]:[\\/][^\s"]*?run-[^\s"\\/]*\.bat)',
                             re.IGNORECASE)
_RUN_BAT_REL_RE = re.compile(r"(?:^|[\s\"])(run-[^\s\"\\/<>|*?]*\.bat)", re.IGNORECASE)


def _is_python_exe(path: str) -> bool:
    """The file name of a python interpreter (python.exe, pythonw.exe, python3.14.exe, py.exe, pyw.exe)."""
    n = os.path.basename(str(path or "").strip()).casefold()
    if n.endswith(".exe"):
        n = n[:-4]
    return n in _PY_NAMES or n.startswith("python")


def is_strata_cmd(name: str | None, cmd: list[str] | None) -> bool:
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
        return t == SERVER_MARK or t.endswith(os.sep + SERVER_MARK)   # whole path segments only
    return any(_hit(t) for t in (cmd or [])[1:])


def strata_scripts(proc: "psutil.Process") -> list[str]:
    """Absolute paths of the serve\\server.py tokens in proc's argv (relative ones resolved against its cwd)."""
    try:
        cmd = proc.cmdline() or []
    except Exception:  # noqa: BLE001 - psutil.Error / OSError: unknown = no proof
        return []
    out = []
    cwd = None
    for tok in cmd[1:]:
        t = str(tok).strip('"')
        nt = os.path.normcase(t)
        if not (nt == SERVER_MARK or nt.endswith(os.sep + SERVER_MARK)):
            continue
        if not os.path.isabs(t):
            if cwd is None:
                try:
                    cwd = proc.cwd() or ""
                except Exception:  # noqa: BLE001
                    cwd = ""
            if not cwd:
                continue
            t = os.path.join(cwd, t)
        out.append(t)
    return out


class StrataEngine(Engine):
    id = "strata"
    name = "Strata"
    default_port = 8080
    log_prefix = "strata"
    capabilities = frozenset({
        CAP_FIND_INSTALL, CAP_LAUNCHER_FILES, CAP_UNLOAD, CAP_LOAD_NUDGE, CAP_LOAD_PROGRESS, CAP_LIVE_ACTIVITY,
        CAP_REQUEST_HISTORY, CAP_REQUEST_END, CAP_RAM_MODES,
    })

    # ------------------------------------------------------------------ identity (stop path)
    def is_server_process(self, name: str | None, cmdline: list[str] | None) -> bool:
        return is_strata_cmd(name, cmdline)

    def verified_by_process(self, proc: "psutil.Process", cfg: Mapping[str, Any]) -> bool:
        prefix = dir_prefix(cfg.get("strata_dir"))
        return bool(prefix) and any(path_under(s, prefix) for s in strata_scripts(proc))

    def claims_port(self, cmdline: list[str], port: int, cfg: Mapping[str, Any]) -> bool:
        sdir = str(cfg.get("strata_dir") or "")
        sdir_n = os.path.normcase(os.path.abspath(sdir)).rstrip(os.sep) + os.sep if sdir else ""
        port_ok = explicit_port(cmdline) == port
        in_dir = bool(sdir_n) and any(os.path.normcase(os.path.abspath(str(t).strip('"'))).startswith(sdir_n)
                                      for t in cmdline[1:] if str(t).casefold().endswith(".py"))
        return port_ok or (in_dir and explicit_port(cmdline) in (None, port) and port == self.default_port)

    def probe(self, http: Http, timeout: float) -> ServiceState:
        r = http("GET", "/health", timeout)
        if r.code != 200 or not isinstance(r.json, dict):
            return NO_ANSWER
        body = r.json
        return ServiceState(answered=True, is_engine=body.get("service") == "strata",
                            loaded=body.get("loaded") is True,
                            model=str(body["model"]) if body.get("model") else None)

    def protects_process(self, cmdline: Iterable[str] | None, exe: str | None, cfg: Mapping[str, Any]) -> bool:
        """Loose on purpose (sweep protection only): the joined cmdline mentions serve\\server.py, or the exe
        lives under strata_dir."""
        if cmdline:
            try:
                if SERVER_MARK in os.path.normcase(" ".join(str(c) for c in cmdline)):
                    return True
            except TypeError:
                pass
        prefix = dir_prefix(cfg.get("strata_dir"))
        if not exe or not prefix:
            return False
        try:
            return os.path.normcase(os.path.abspath(exe)).startswith(prefix)
        except (OSError, ValueError):
            return False

    def owns_child(self, proc: "psutil.Process", cfg: Mapping[str, Any]) -> bool:
        """engine\\strata.exe, strata-vision.exe, the venv python: an exe under strata_dir."""
        prefix = dir_prefix(cfg.get("strata_dir"))
        if not prefix:
            return False
        try:
            exe = proc.exe() or ""
        except Exception:  # noqa: BLE001 - psutil.Error / OSError: unknown exe = not ours
            exe = ""
        return bool(exe) and path_under(exe, prefix)

    def launcher_process(self, root: "psutil.Process", cfg: Mapping[str, Any]) -> "psutil.Process | None":
        """cmd.exe /c "<strata_dir>\\run-*.bat" (or the configured launcher .bat) that launched `root`, else None."""
        sdir = str(cfg.get("strata_dir") or "").strip()
        bat = str(cfg.get("launcher_bat") or "").strip()
        own_bat = os.path.normcase(os.path.abspath(bat)) if bat and os.path.isabs(bat) else None
        if not sdir and own_bat is None:
            return None
        prefix = os.path.normcase(os.path.abspath(sdir)) + os.sep if sdir else None
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
        except Exception:  # noqa: BLE001 - psutil.Error / OSError
            return None
        if not re.search(r"(^|\s)/c(\s|$)", joined, re.IGNORECASE):
            return None
        cands = [m.group(1) or m.group(2) for m in _RUN_BAT_ABS_RE.finditer(joined)]
        if not cands:
            try:
                cwd = par.cwd()
            except Exception:  # noqa: BLE001
                cwd = ""
            if cwd:
                cands = [os.path.join(cwd, m.group(1)) for m in _RUN_BAT_REL_RE.finditer(joined)]
        for cand in cands:
            try:
                full = os.path.normcase(os.path.abspath(cand.strip()))
            except (OSError, ValueError):
                continue
            if (prefix is not None and full.startswith(prefix)) or full == own_bat:
                return par
        return None

    # ------------------------------------------------------------------ launch
    def is_configured(self, cfg: Mapping[str, Any]) -> bool:
        """A python launch command is set (another engine's command left in config.json does not count)."""
        cmd = [str(x) for x in (cfg.get("launch_cmd") or [])]
        return bool(cmd) and _is_python_exe(cmd[0])

    def plan_launch(self, cfg: Mapping[str, Any]) -> LaunchPlan:
        cmd = [str(x) for x in (cfg.get("launch_cmd") or [])]
        if not cmd:
            raise LaunchError("No Strata launcher chosen yet · open Settings (gear) and pick your run-*.bat")
        if not cmd[0].strip().casefold().endswith(".exe"):
            # never hand a .bat/.cmd/script to CreateProcess: that runs it through cmd.exe (hand-edited config.json)
            raise LaunchError(f"Launch command must start with python.exe (the Strata venv interpreter), "
                              f"not {os.path.basename(cmd[0]) or cmd[0]!r} - re-select the run-*.bat in Settings")
        if not _is_python_exe(cmd[0]):
            # config.json's launch keys are shared between engines: after another engine was set up, its command
            # is still there. Strata is only ever started through a python interpreter.
            raise LaunchError(f"Launch command must start with python.exe (the Strata venv interpreter), "
                              f"not {os.path.basename(cmd[0])} - re-select the run-*.bat in Settings")
        if not os.path.isfile(cmd[0]):
            raise LaunchError(f"launcher not found: {cmd[0]}")
        sdir = str(cfg.get("strata_dir") or "")
        if not sdir or not os.path.isdir(sdir):
            raise LaunchError(f"strata_dir not found: {sdir or '(empty)'}")
        # the directory the chosen run-*.bat starts the server from (its `cd /d`); v1.0 configs have none
        cwd = str(cfg.get("launch_cwd") or "")
        if not cwd or not os.path.isdir(cwd):
            cwd = sdir
        return LaunchPlan(cmd=cmd, cwd=cwd, env={"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})

    # ------------------------------------------------------------------ readiness / release
    def nudge_load(self, http: Http) -> int | None:
        return http("POST", "/load", 3.0).code

    def unload(self, http: Http, force: bool) -> str:
        code = http("POST", "/unload", 5.0).code if force else http("POST", "/unload?wait=10", 15.0).code
        if code == 200:
            return UNLOAD_OK
        if code == 409:
            return UNLOAD_BUSY
        return UNLOAD_UNAVAILABLE

    # ------------------------------------------------------------------ UI features
    setup_hint = "Open Settings (gear) and pick your run-*.bat"
    launcher_kind = "run-*.bat"
    launcher_pick_text = "Pick the .bat that starts Strata."
    launcher_filetypes = (("Batch files", "*.bat *.cmd"), ("All files", "*.*"))
    has_dashboard = True                 # Strata serves its own web app (chat, Monitor, settings) on /
    low_ram_loading = "reading experts from SSD · slower"
    low_ram_serving = "Reading from SSD (slower)"

    def find_installs(self, stop_evt: Any = None) -> list[Any]:
        import strata_finder
        return strata_finder.find_strata(strata_finder.default_roots(), time_budget_s=8.0, stop_evt=stop_evt)

    def install_dir_of(self, found: Any) -> str:
        return str(getattr(found, "strata_dir", "") or "")

    def install_settings(self, found: Any) -> dict:
        return {"strata_dir": self.install_dir_of(found)}

    def preferred_launcher(self, found: Any) -> str | None:
        import strata_finder
        return strata_finder.best_launcher(found) or None

    def auto_launch(self, found: list[Any], cfg: Mapping[str, Any]) -> Any:
        """One Strata install on this PC with exactly one launcher (one model) that can be read: use it. Several
        launchers are several models: the user picks."""
        try:
            if len(found) != 1 or len(list(getattr(found[0], "launchers", None) or [])) != 1:
                return None
            path = self.preferred_launcher(found[0])
            return self.read_launcher(path) if path else None
        except Exception:  # noqa: BLE001 - LaunchError (a launcher that cannot be used) included: the user picks
            return None

    # launcher files: run-*.bat in the Strata folder (launcher_bat parses them; they are never executed)
    def list_launchers(self, cfg: Mapping[str, Any]) -> list[str]:
        import launcher_bat
        return launcher_bat.list_launchers(str(cfg.get("strata_dir") or ""))

    def current_launcher(self, cfg: Mapping[str, Any]) -> str | None:
        import launcher_bat
        return launcher_bat.configured_bat(dict(cfg))

    def read_launcher(self, path: str) -> Any:
        import launcher_bat
        try:
            info = launcher_bat.parse_bat(path)
        except launcher_bat.LauncherError as exc:
            raise LaunchError(str(exc)) from exc
        missing = launcher_bat.missing_files(info)
        if missing:
            raise LaunchError(missing)
        return info

    def same_launch(self, cfg: Mapping[str, Any], info: Any) -> bool:
        import launcher_bat
        return launcher_bat.same_launch(dict(cfg), info)

    def apply_launcher(self, cfg: dict, info: Any) -> None:
        import launcher_bat
        launcher_bat.apply_to_config(cfg, info)

    def load_progress(self, log_text: str, elapsed_s: float, expected_s: float | None) -> tuple[str, float, str | None]:
        import load_progress
        return load_progress.parse(log_text, elapsed_s, expected_s)

    @staticmethod
    def config_path(cfg: Mapping[str, Any]) -> str | None:
        """The Strata config JSON from launch_cmd's --config (its "log" field names the engine log)."""
        cmd = list(cfg.get("launch_cmd") or [])
        for i, a in enumerate(cmd[:-1]):
            if str(a) == "--config":
                return str(cmd[i + 1])
        return None

    def load_report(self, cfg: Mapping[str, Any], since: float | None = None) -> LoadReport | None:
        """The engine log's verdict for a run: full-RAM mode, or the slow Low-RAM (SSD) mode."""
        import engine_log
        cfg_path = self.config_path(cfg)
        if not cfg_path:
            return None
        ld = engine_log.current(cfg_path, since)
        return LoadReport(mode=ld.mode, need_gib=ld.arena_gib, available_gib=ld.available_gib,
                          short_gib=ld.short_gib,
                          detail=f"GPU slots {ld.gpu_slots}{', GPU cache shrunk' if ld.gpu_shrunk else ''}")

    def memory_need_gib(self, cfg: Mapping[str, Any]) -> float | None:
        """The expert arena the engine log recorded for this model: what full-RAM mode needs."""
        import engine_log
        cfg_path = self.config_path(cfg)
        log_path = engine_log.config_log_path(cfg_path) if cfg_path else None
        return engine_log.learned_arena_gib(log_path) if log_path else None

    def metrics_poller(self, cfg: Mapping[str, Any] | None = None, **seams: Any) -> MetricsPoller:
        """Strata's GET /metrics (+ GET /status when a request just ended), converted by metrics_store.
        seams: fetch_metrics(root_url, all_requests=False) -> document, fetch_status(root_url) -> document."""
        import metrics_store
        return metrics_store.StrataPoller(fetch_metrics=seams.get("fetch_metrics"),
                                          fetch_status=seams.get("fetch_status"))
