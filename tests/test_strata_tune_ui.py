"""Settings, Strata: the Context length / Vision rows under the launcher. Window test (not CI). Fakes only: a made-up
Strata folder; nothing is started or contacted; config.json writes are captured."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_ui_smoke as U  # noqa: E402  (first: it redirects the data dir)

import config_store  # noqa: E402
import strata_tuning as st  # noqa: E402
import ui_helpers as H  # noqa: E402

PASSES = 0
FAILS: list[str] = []
ARGS = ["--pack", "C:\\packs\\m", "--max-context", "262144", "--vision"]


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


def test_rows() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="agentstart-tune-ui-"))
    pa, pb = U._free_port(), U._free_port()
    sdir = U.make_strata(tmp, "Strata-T", {"a": pa, "b": pb})
    (sdir / "strata-a.json").write_text(json.dumps({"model_name": "model-a", "args": ARGS, "vision": {"exe": "v"}}),
                                        encoding="utf-8")                     # b keeps a config with nothing to tune
    cfg = dict(U.CFG, strata_dir=str(sdir), launcher_bat=str(sdir / "run-a.bat"), port=pa,
               endpoint_url=f"http://127.0.0.1:{pa}/v1", model_name="model-a", launch_cwd=str(sdir),
               launch_cmd=[str(sdir / ".venv" / "Scripts" / "python.exe"), str(sdir / "serve" / "server.py"),
                           "--engine", "strata", "--config", str(sdir / "strata-a.json"), "--port", str(pa)])
    saved: list[dict] = []
    real_save, real_load = config_store.save_config, config_store.load_config
    config_store.save_config = lambda c, *a, **k: saved.append(dict(c))
    config_store.load_config = lambda *a, **k: dict(cfg)
    app, det, ep = U.new_app(cfg)
    key = st._key(str(sdir / "strata-a.json"))
    try:
        d = H.open_settings(app)
        U.pump(app, 0.4)
        check(d._tune_ctx.get() == "256K tokens" and d._tune_vis.get() == "On"
              and str(d._tune_ctx_menu.cget("state")) == "normal" and list(d._tune_vis_menu.cget("values")) == ["On", "Off"],
              f"the rows show what the launcher is set to ({d._tune_ctx.get()!r}, {d._tune_vis.get()!r})")
        check(list(d._tune_ctx_menu.cget("values")) == ["32K tokens", "64K tokens", "128K tokens", "256K tokens"],
              "context choices")
        check(all(h <= d.logical_height for h in d.tab_heights.values()), f"every tab still fits ({d.tab_heights})")
        U.shot(d, "settings_strata_tuning")
        d._tune_ctx.set("128K tokens")
        d._tune_changed("128K tokens")
        d._tune_vis.set("Off")
        d._tune_changed("Off")
        U.pump(app, 0.2)
        check(str(d._save_btn.cget("state")) == "normal", "a new choice can be saved")
        before = (sdir / "strata-a.json").read_bytes()
        d.save()
        U.pump_until(app, lambda: bool(saved), 4.0)
        U.pump(app, 0.5)
        check(bool(saved) and saved[-1].get(st.TUNING_KEY) == {key: {"context": 131072, "vision": False}}
              and saved[-1]["launch_cmd"] == cfg["launch_cmd"],
              f"saved into Agent Start's config only; the launch command is unchanged ({saved[-1:] and saved[-1].get(st.TUNING_KEY)})")
        check((sdir / "strata-a.json").read_bytes() == before and not ep.start_calls,
              "Strata's own file is untouched and nothing was started")
        check(app.cfg.get(st.TUNING_KEY) == {key: {"context": 131072, "vision": False}}, "applied to the running app")
        if not H.is_closed(app) and app.dialog is not None:
            H.close_settings(app.dialog)
            U.pump(app, 0.4)
        # reopen: the saved choice is shown; the other launcher has nothing to tune
        cfg.update(app.cfg)
        d = H.open_settings(app)
        U.pump(app, 0.4)
        check(d._tune_ctx.get() == "128K tokens" and d._tune_vis.get() == "Off", "reopened: the saved choice is shown")
        d.choose(str(sdir / "run-b.bat"))
        U.pump(app, 0.2)
        check(str(d._tune_ctx_menu.cget("state")) == "disabled" and d._tune_ctx.get() == "As in the launcher"
              and str(d._tune_vis_menu.cget("state")) == "disabled", "a launcher with nothing to tune: rows greyed out")
        d.choose(str(sdir / "run-a.bat"))
        U.pump(app, 0.2)
        d._tune_ctx.set("256K tokens")
        d._tune_changed("256K tokens")
        d._tune_vis.set("On")
        d._tune_changed("On")
        saved.clear()
        d.save()
        U.pump_until(app, lambda: bool(saved), 4.0)
        check(bool(saved) and st.TUNING_KEY not in saved[-1], "back to the launcher's own values: the choice is removed")
        U.pump(app, 0.4)
        if app.dialog is not None:
            H.close_settings(app.dialog)
            U.pump(app, 0.3)
    finally:
        config_store.save_config, config_store.load_config = real_save, real_load
        app.destroy()


def run() -> int:
    for t in (test_rows,):
        print(f"-- {t.__name__}")
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
