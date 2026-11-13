"""After an engine switch: the engine finds its install; one clear choice is configured without asking, otherwise
Settings shows Build / Model dropdowns. Window test (not CI). Fakes only: nothing is searched, started or contacted.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_ui_smoke as U  # noqa: E402  (first: it redirects the data dir)

import config_store  # noqa: E402
import engines  # noqa: E402
import llama_finder as LF  # noqa: E402
import main  # noqa: E402
import ui_helpers as H  # noqa: E402
from engines.llama_cpp import FoundLlama  # noqa: E402
from settings_dialog import SettingsDialog  # noqa: E402

PASSES = 0
FAILS: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="agentstart-find-"))


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


def _build(name: str, backend: str, source: str = "folder") -> LF.Build:
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    exe = d / "llama-server.exe"
    exe.write_bytes(b"")
    return LF.Build(str(exe), str(d), backend, (backend,), "b12314" if source == "folder" else None, source)


def _model(name: str, gb: float) -> LF.Model:
    d = TMP / "models"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.gguf"
    p.write_bytes(b"GGUF")
    return LF.Model(str(p), name, int(gb * 1024 ** 3), str(d))


CUDA, VULKAN = _build("llama.cpp-cuda-b12314", "cuda"), _build("winget-pkg", "vulkan", "winget")
M1, M2 = _model("Qwen-27B-Q3_K_XL", 12.2), _model("Qwen-27B-IQ4_XS", 13.4)
# a model the app sets up gets a short name once (else llama-server reports the full path as the model id)
A1, A2 = ["--alias", "Qwen-27B-Q3_K_XL"], ["--alias", "Qwen-27B-IQ4_XS"]


def _found(models) -> FoundLlama:
    builds = LF.rank_builds([VULKAN, CUDA], True)
    return FoundLlama(tuple(builds), tuple(models), LF.choose(builds, models, True))


def _llama_app(found_list):
    cfg = dict(U.CFG, backend="llama_cpp", launch_cmd=[], model_name="")
    det, ep = U.StubDetector(), U.StubEndpoint()
    ep.engine = engines.get_engine(cfg)
    app = main.AgentStartApp(detector=det, endpoint=ep, config=cfg, metrics_fetcher=U.FakeMetrics(),
                             vram_sampler=U.FakeVram())
    app.attributes("-topmost", True)
    app.engine.find_installs = lambda stop_evt=None: list(found_list)      # type: ignore[method-assign]
    app._no_finder, app._finder_ran = False, False
    U.pump(app, 0.3)
    return app, det, ep


def test_engine_side() -> None:
    eng = engines.get_engine({"backend": "llama_cpp"})
    one, two = _found([M1]), _found([M1, M2])
    info = eng.auto_launch([one], {})
    check(info is not None and info.program == CUDA.program
          and tuple(info.args) == ("-m", M1.path, "--port", "9931", "--alias", M1.name),
          f"one model: the CUDA build with -m, --port 9931 and the model's short name, nothing else ({info})")
    check(eng.auto_launch([two], {}) is None and eng.auto_launch([_found([])], {}) is None
          and eng.auto_launch([], {}) is None and eng.auto_launch([one, one], {}) is None
          and eng.auto_launch([object()], {}) is None, "several / no models, nothing found, garbage: no auto set-up")
    check(eng.install_dir_of(two) == CUDA.folder and eng.install_settings(two) == {"launch_cmd": [CUDA.program]},
          "Settings opens on the best build")
    opts = eng.launch_options(two, {})
    check(opts is not None and [p for _l, p in opts["programs"]] == [CUDA.program, VULKAN.program]
          and opts["programs"][0][0].startswith("CUDA") and len(opts["models"]) == 2
          and opts["program_title"] == "Build", f"options: builds best first, both models ({opts and opts['programs']})")
    check(opts["model_of"](("-c", "8192", "-m", M2.path)) == M2.path and opts["model_of"](("--port", "1")) is None
          and tuple(opts["default_args"](M1.path)) == ("-m", M1.path, "--port", "9931", "--alias", M1.name),
          "options: helpers")
    none = eng.launch_options(object(), {})
    check(none is not None and none["programs"] == [] and none["models"] == []
          and all(callable(none[k]) for k in ("with_context", "with_vision", "with_port", "model_of")),
          "nothing found: no Build / Model lists, but the context / vision / port controls are offered")
    strata = engines.get_engine({"backend": "strata"})
    check(strata.auto_launch([], {}) is None and strata.auto_launch([object(), object()], {}) is None
          and strata.auto_launch([object()], {}) is None, "Strata: none / several installs / unreadable: no auto set-up")


def test_auto_config() -> None:
    saved: list[dict] = []
    real_save, real_load = config_store.save_config, config_store.load_config
    config_store.save_config = lambda cfg, *a, **k: saved.append(dict(cfg))
    config_store.load_config = lambda *a, **k: dict(U.CFG, backend="llama_cpp", launch_cmd=[], model_name="")
    app = None
    try:
        app, det, ep = _llama_app([_found([M1])])
        app._maybe_find_strata()
        U.pump_until(app, lambda: bool(saved), 4.0)
        U.pump(app, 0.4)
        check(len(saved) == 1 and saved[0]["launch_cmd"] == [CUDA.program, "-m", M1.path, "--port", "9931", *A1]
              and saved[0]["port"] == 9931 and saved[0]["endpoint_url"] == "http://127.0.0.1:9931/v1",
              f"one build + one model: saved without a click ({saved})")
        check(app.engine.is_configured(app.cfg) and app.cfg["port"] == 9931, "and applied to the running app")
        check("Found llama.cpp" in U.status(app) and "Qwen-27B-Q3_K_XL" in U.status(app),
              f"the window says what it set up ({U.status(app)!r})")
        check(not ep.start_calls if hasattr(ep, "start_calls") else True, "nothing was started")
        check(app.state_name == main.IDLE, "still idle: the user turns it on")
        app.destroy()
        app = None
        # two models: nothing is saved; Turn on opens Settings with the dropdowns
        saved.clear()
        app, det, ep = _llama_app([_found([M1, M2])])
        app._maybe_find_strata()
        U.pump(app, 1.0)
        check(not saved and not app.engine.is_configured(app.cfg), "two models: nothing saved by itself")
        d = H.open_settings(app)
        check(isinstance(d, SettingsDialog) and d.option_rows == 2, f"Settings shows two dropdown rows ({d and d.option_rows})")
        if d is not None:
            check(d._program.get() == CUDA.program and d._program_choice.get().startswith("CUDA"),
                  f"the CUDA build is preselected ({d._program_choice.get()!r})")
            check(d._model_choice.get() == "Choose a model…" and str(d._save_btn.cget("state")) == "disabled",
                  "no model yet: Save is off")
            labels = [la for la, _p in d._opt_models]
            d._model_picked(labels[0])
            U.pump(app, 0.2)
            first = d._opt_models[0][1]
            check(first in d._args.get() and d._args.get().endswith(f"--port 9931 --alias {Path(first).stem}")
                  and d._args.get().startswith("-m "),
                  f"picking a model writes the arguments, with its short name ({d._args.get()!r})")
            check(str(d._save_btn.cget("state")) == "normal" and d._model_choice.get() == labels[0],
                  "now it can be saved")
            d._args.set(d._args.get() + " -c 8192")
            d._model_picked(labels[1])
            U.pump(app, 0.2)
            second = d._opt_models[1][1]
            check(second in d._args.get() and first not in d._args.get() and "-c 8192" not in d._args.get()
                  and d._args.get().endswith(f"--port 9931 --alias {Path(second).stem}"),
                  f"another model: its own arguments - the first model's context does not follow ({d._args.get()!r})")
            d._model_picked(labels[0])
            U.pump(app, 0.2)
            check(first in d._args.get() and second not in d._args.get() and "-c 8192" in d._args.get()
                  and f"--alias {Path(first).stem}" in d._args.get(),
                  f"back to the first model: its context is recalled ({d._args.get()!r})")
            d._model_picked(labels[1])
            U.pump(app, 0.2)
            check(second in d._args.get() and "-c 8192" not in d._args.get(),
                  f"and the second model has its own again ({d._args.get()!r})")
            d._program_picked(d._opt_programs[1][0])
            U.pump(app, 0.2)
            check(d._program.get() == VULKAN.program and d._program_choice.get() == d._opt_programs[1][0],
                  "picking the other build fills the program box")
            d._program.set(str(TMP / "elsewhere" / "llama-server.exe"))
            U.pump(app, 0.2)
            check(d._program_choice.get() == "Another program (below)" and d.advanced_open,
                  "a typed program: the dropdown says so, and Advanced (the boxes below) is open")
            d._program_picked(d._opt_programs[0][0])
            U.pump(app, 0.2)
            need = H.settings_tab_req_heights(d)
            check(H.settings_option(d, "Build")["caption"] == "Build" and H.settings_option(d, "Model")["caption"]
                  == "Model" and need["Model"] <= d.logical_height + 1 and d.tab_heights["Model"] <= d.logical_height,
                  f"the two rows are on the Model tab, which fits the window ({need['Model']} of {d.logical_height})")
            U.shot(d, "settings_llama_found")
            H.close_settings(d)
            U.pump(app, 0.3)
    finally:
        config_store.save_config, config_store.load_config = real_save, real_load
        if app is not None:
            app.destroy()


def test_card_and_switch() -> None:
    """Two models: the set-up card, the main button lists the models, and once set up the switch button does."""
    saved: list[dict] = []
    real_save, real_load = config_store.save_config, config_store.load_config
    disk = dict(U.CFG, backend="llama_cpp", launch_cmd=[], model_name="")
    config_store.save_config = lambda cfg, *a, **k: (saved.append(dict(cfg)), disk.update(cfg))
    config_store.load_config = lambda *a, **k: dict(disk)
    app, det, ep = _llama_app([_found([M1, M2])])
    try:
        app._maybe_find_strata()
        U.pump_until(app, lambda: app._setup_shown, 4.0)
        U.pump(app, 0.3)
        check(app._setup_shown and app.setup_title.cget("text") == "llama.cpp found"
              and app.setup_tag.cget("text").strip() == "CUDA" and app.setup_line.cget("text") == "2 models on this PC",
              f"card: llama.cpp found, CUDA, 2 models ({app.setup_line.cget('text')!r})")
        check(app.toggle.cget("text") == "Choose model" and "Not set up yet" in U.status(app)
              and not H.switch_button_visible(app), f"the main button chooses ({app.toggle.cget('text')!r})")
        check(abs(U.expected_h(app) - H.window_height(app)) <= 2, "height fits the card")
        U.shot(app, "llama_setup_card")
        opened: list = []
        app._model_menu.open = lambda x, y: opened.append((x, y))        # a native popup: it would block the test
        n = len(ep.start_calls)
        app.on_toggle()
        U.pump(app, 0.3)
        labels = [la for la, _k in app._launchers]
        check(len(opened) == 1 and len(labels) == 2 and app.dialog is None and len(ep.start_calls) == n,
              f"the button opens the model list; nothing starts, no Settings ({labels})")
        pick = next(la for la in labels if "Q3_K_XL" in la)
        app._on_model_menu(pick)
        U.pump_until(app, lambda: bool(saved), 4.0)
        U.pump(app, 0.5)
        check(bool(saved) and saved[-1]["launch_cmd"] == [CUDA.program, "-m", M1.path, "--port", "9931", *A1],
              f"picking a model sets it up on the best build ({saved[-1:] and saved[-1]['launch_cmd']})")
        check(not app._setup_shown and app.toggle.cget("text") == "Turn on" and "Qwen-27B-Q3_K_XL" in U.status(app)
              and len(ep.start_calls) == n, f"set up: Turn on, says which model, nothing started ({U.status(app)!r})")
        check(H.switch_button_visible(app), "the switch button is back, now listing models")
        labels = [la for la, _k in app._launchers]
        check(sum(la.endswith("✓") for la in labels) == 1 and next(la for la in labels if la.endswith("✓")).startswith(
            "Qwen-27B-Q3_K_XL"), f"the configured model is ticked ({labels})")
        # idle switch keeps the user's other arguments
        app.cfg["launch_cmd"] = list(app.cfg["launch_cmd"]) + ["-c", "65536"]
        disk["launch_cmd"] = list(app.cfg["launch_cmd"])
        app._refresh_launchers()
        other = next(la for la, _k in app._launchers if "IQ4_XS" in la)
        app._on_model_menu(other)
        U.pump_until(app, lambda: saved[-1]["launch_cmd"][2] == M2.path, 4.0)
        check(saved[-1]["launch_cmd"] == [CUDA.program, "-m", M2.path, "--port", "9931", *A2],
              f"switching: the other model starts with its own arguments, not the first one's context "
              f"({saved[-1]['launch_cmd']})")
        U.pump(app, 0.4)
        # serving: a switch goes through the app's own stop first
        U.to_active(app, ep)
        U.pump(app, 0.3)
        stops = len(ep.stop_calls)
        back = next(la for la, _k in app._launchers if "Q3_K_XL" in la)
        count = len(saved)
        app._on_model_menu(back)
        U.pump(app, 0.5)
        U.pump_until(app, lambda: len(ep.stop_calls) == stops + 1, 4.0)
        check(len(ep.stop_calls) == stops + 1 and ep.stop_calls[-1][1] is False and len(saved) == count + 1
              and saved[-1]["launch_cmd"] == [CUDA.program, "-m", M1.path, "--port", "9931", *A1, "-c", "65536"],
              f"idle model: the switch goes ahead - saved with that model's own context recalled, then the normal "
              f"(not forced) stop ({len(ep.stop_calls) - stops}, {saved[-1]['launch_cmd']})")
    finally:
        config_store.save_config, config_store.load_config = real_save, real_load
        app.destroy()


def test_strata_unchanged() -> None:
    """A configured Strata never searches; Settings has no dropdown rows."""
    app, det, ep = U.new_app()
    try:
        ran: list = []
        app.engine.find_installs = lambda stop_evt=None: (ran.append(1), [])[1]     # type: ignore[method-assign]
        app._no_finder, app._finder_ran = False, False
        app._maybe_find_strata()
        U.pump(app, 0.5)
        check(not ran and app._finder_ran is False, "configured Strata: no search")
        d = H.open_settings(app)
        check(d is not None and d.option_rows == 0 and d._program_menu is None, "Strata's Settings: no dropdown rows")
        if d is not None:
            H.close_settings(d)
            U.pump(app, 0.3)
    finally:
        del app.engine.find_installs
        app.destroy()


def run() -> int:
    for t in (test_engine_side, test_auto_config, test_card_and_switch, test_strata_unchanged):
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
