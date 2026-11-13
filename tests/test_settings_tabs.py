"""Settings as three tabs (Model / Behaviour / About), the llama.cpp context / vision / port controls and the GPU-fit
note. Window test (not CI). Fakes only: nothing is searched, started or contacted; config / state writes are
recorded; the GPU-fit check is a recorder (no llama tool runs).
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import traceback
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_ui_smoke as U  # noqa: E402  (first: it redirects the data dir)

import config_store  # noqa: E402
import engines  # noqa: E402
import history_dialog  # noqa: E402
import llama_finder as LF  # noqa: E402
import main  # noqa: E402
import settings_dialog as SD  # noqa: E402
import theme as T  # noqa: E402
import ui_helpers as H  # noqa: E402
from engines.base import split_args  # noqa: E402
from engines.llama_cpp import FoundLlama  # noqa: E402
from whitelist_modal import WhitelistModal  # noqa: E402

PASSES = 0
FAILS: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="agentstart-tabs-"))
pump, pump_until = U.pump, U.pump_until


def check(cond, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


# ------------------------------------------------------------------------------------------------ fixtures
def _file(*parts: str, data: bytes = b"GGUF") -> str:
    p = TMP.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return str(p)


BUILD_DIR = TMP / "llama.cpp-cuda-b12314"
EXE = _file("llama.cpp-cuda-b12314", "llama-server.exe", data=b"")
OTHER_EXE = _file("elsewhere", "llama-server.exe", data=b"")
MODEL_A = _file("a", "Qwen-A-Q4.gguf")
PROJ_A = _file("a", "mmproj-F16.gguf")
MODEL_B = _file("b", "Qwen-B-Q4.gguf")                 # no projector in its folder
MODEL_C = _file("c", "Qwen-C-Q4.gguf")
PROJ_C = _file("c", "mmproj-F16.gguf")                 # the same projector file name as A's
STRAY_PROJ = _file("stray", "mmproj-BF16.gguf")


def _found(models=True, builds=True) -> FoundLlama:
    bl = [LF.Build(EXE, str(BUILD_DIR), "cuda", ("cuda",), "b12314", "folder")] if builds else []
    ml = [LF.Model(p, os.path.splitext(os.path.basename(p))[0], 1024 ** 3, os.path.dirname(p))
          for p in (MODEL_A, MODEL_B, MODEL_C)] if models else []
    return FoundLlama(tuple(bl), tuple(ml), LF.choose(bl, ml, True) if bl and ml else None)


def _llama_app(launch_cmd: list[str], found: FoundLlama):
    cfg = dict(U.CFG, backend="llama_cpp", launch_cmd=list(launch_cmd), model_name="", port=9931,
               endpoint_url="http://127.0.0.1:9931/v1")
    det, ep = U.StubDetector(), U.StubEndpoint()
    ep.engine = engines.get_engine(cfg)
    app = main.AgentStartApp(detector=det, endpoint=ep, config=cfg, metrics_fetcher=U.FakeMetrics(),
                             vram_sampler=U.FakeVram())
    app.attributes("-topmost", True)
    app._no_finder, app._finder_ran = True, True     # nothing is searched
    app._found_strata = found                        # what the start-up search would have found
    pump(app, 0.3)
    return app, det, ep


class FakeFit:
    """on_fit_preview recorder: (args, done, on the Tk thread) per call; returns `ret`."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, object, bool]] = []
        self.ret = True

    def __call__(self, info, done) -> bool:
        self.calls.append((tuple(info.args), done, threading.current_thread() is threading.main_thread()))
        return self.ret


class Saves:
    """Records config.json / state.json writes (nothing reaches the disk)."""

    def __enter__(self) -> "Saves":
        self.config: list[dict] = []
        self.state: list[dict] = []
        self._orig = (config_store.save_config, config_store.save_state)
        config_store.save_config = lambda cfg, *a, **k: self.config.append(dict(cfg))
        config_store.save_state = lambda st, *a, **k: self.state.append(dict(st))
        return self

    def __exit__(self, *exc) -> None:
        config_store.save_config, config_store.save_state = self._orig


def _shown(w) -> bool:
    return w.winfo_manager() == "grid"


def _args(d) -> list[str]:
    return list(split_args(d._args.get()))


def _label_of(d, path: str) -> str:
    return next(la for la, p in d._opt_models if os.path.normcase(p) == os.path.normcase(path))


def _tab_texts_ok(d, tab: str, want: list[str]) -> tuple[bool, list[str]]:
    have = H.settings_tab_texts(d, tab)
    return all(any(t == w or t.startswith(w) for t in have) for w in want), have


def _heights_fixed(app, d) -> tuple[bool, dict]:
    """The window keeps one height on every tab, and every tab fits in it."""
    seen = {}
    for name in H.settings_tabs(d):
        H.show_settings_tab(d, name)
        pump(app, 0.1)
        seen[name] = d.winfo_height()
    need = H.settings_tab_req_heights(d)
    H.show_settings_tab(d, "Model")
    pump(app, 0.1)
    return len(set(seen.values())) == 1 and all(v <= d.logical_height + 1 for v in need.values()), {
        "window": seen, "need": need, "logical": d.logical_height}


# ------------------------------------------------------------------------------------------------ Strata
def test_strata_tabs() -> None:
    app, det, ep = U.new_app()
    try:
        app._extras = True                                   # the Behaviour rows are live
        d = H.open_settings(app)
        check(d is not None, "Strata: Settings opens")
        if d is None:
            return
        pump(app, 0.4)
        check(H.settings_tabs(d) == ["Model", "Behaviour", "About"] and H.settings_tab(d) == "Model",
              f"Strata: three tabs, opens on Model ({H.settings_tabs(d)}, {H.settings_tab(d)})")
        ok, have = _tab_texts_ok(d, "Model", ["Engine", "Strata launcher", "Browse", "Context length", "Vision"])
        check(ok and not any(t in have for t in ("Port", "Build", SD.ADVANCED_TITLE)),
              f"Strata Model tab: Engine, the launcher dropdown + Browse, the detail line, Context length and "
              f"Vision; no llama.cpp rows ({have})")
        check(str(d._tune_ctx_menu.cget("state")) == "disabled" and d._tune_ctx.get() == "As in the launcher",
              "no launcher chosen / nothing to tune: the two rows are greyed out")
        check(H.settings_tab_of(d, d._detail) == "Model" and H.settings_tab_of(d, d._menu) == "Model",
              "Strata: launcher dropdown and its detail line are on Model")
        check(_shown(d._save_btn) and _shown(d._cancel_btn) and not _shown(d._close_btn) and _shown(d._msg),
              "Model tab: Cancel / Save and the message line")
        U.shot(d, "settings_tabs_strata_model")
        want_b = [SD.GROUP_OTHER_APPS, "A game starts", "Memory runs low", "That app closes",
                  "Idle with no requests", SD.GROUP_AFTER_OFF,
                  SD.REOPEN_LABEL, SD.PROTECTED_LABEL, SD.GROUP_APP, "Keep running in the tray",
                  "Start with Windows"]
        ok, have = _tab_texts_ok(d, "Behaviour", want_b)
        order = [next(i for i, t in enumerate(have) if t == w or t.startswith(w)) for w in want_b] if ok else []
        check(ok and order == sorted(order), f"Behaviour: three groups, rows in order ({have})")
        ok, have = _tab_texts_ok(d, "About", ["Updates", "Version", "Check for updates", "Check for updates daily",
                                              "Endpoint", "Open Strata dashboard", "Copy"])
        check(ok, f"About: version + updates, the daily switch, the endpoint block ({have})")
        check((d._history_btn is not None) == (app.usage is not None)
              and (d._history_btn is None or H.settings_tab_of(d, d._history_btn) == "About"),
              "About: the Usage history row (when the app keeps a history)")
        for key, tab in (("reopen", "Behaviour"), ("tray", "Behaviour"), ("autostart", "Behaviour"),
                         ("games", "Behaviour"), ("memory", "Behaviour"), ("resume", "Behaviour"),
                         ("idle", "Behaviour"), ("updates", "About")):
            check(H.settings_tab_of(d, d.switches[key][1]) == tab, f"switch {key} sits on {tab}")
        check(list(d.switches) == ["reopen", "tray", "autostart", "games", "memory", "resume", "idle", "updates"],
              f"switch keys keep the app's order ({list(d.switches)})")
        H.show_settings_tab(d, "Behaviour")
        pump(app, 0.1)
        check(H.settings_tab(d) == "Behaviour" and not _shown(d._save_btn) and not _shown(d._cancel_btn)
              and _shown(d._close_btn), "Behaviour: only a Close button")
        U.shot(d, "settings_tabs_strata_behaviour")
        H.show_settings_tab(d, "About")
        pump(app, 0.1)
        check(not _shown(d._save_btn) and _shown(d._close_btn), "About: only a Close button")
        U.shot(d, "settings_tabs_strata_about")
        ok, info = _heights_fixed(app, d)
        check(ok, f"Strata: one fixed height on every tab, nothing clipped ({info})")
        check(d.logical_height < 700, f"Strata: shorter than the old window ({d.logical_height})")
        print(f"       Strata Settings: {d.logical_height} px, tabs {d.tab_heights}")

        # ---- nothing is saved or applied by switching tabs
        cfg_before = dict(app.cfg)
        with Saves() as s:
            for name in ("Behaviour", "About", "Model", "About", "Behaviour", "Model"):
                H.show_settings_tab(d, name)
                pump(app, 0.05)
            pump(app, 0.6)
            check(not s.config and not s.state and app.cfg == cfg_before,
                  f"switching tabs saves and changes nothing ({s.config}, {s.state})")

        # ---- Behaviour rows: the same callbacks with the same values as before
        with Saves() as s:
            check(H.choose_settings_option(d, "A game starts", "Do nothing") and app.watch.game_action == "none",
                  f"'A game starts' -> set_game_action ({app.watch.game_action})")
            check(H.choose_settings_option(d, "Memory runs low", "Turn Strata off") and app.watch.mem_action == "off",
                  f"'Memory runs low' -> set_memory_action ({app.watch.mem_action})")
            check(H.choose_settings_option(d, "That app closes", "Turn Strata back on")
                  and app.watch.resume_action == "on", f"'That app closes' -> set_resume_action "
                  f"({app.watch.resume_action})")
            before = app._reopen_pref
            H.toggle_settings_switch(d, SD.REOPEN_LABEL)
            check(app._reopen_pref is (not before), "'Reopen the apps that were closed' -> set_reopen_pref")
            H.toggle_settings_switch(d, SD.REOPEN_LABEL)
            sw = H.settings_switch(d, "Start with Windows")
            check(sw["caption"] == "Start with Windows (installed app only)" and not sw["enabled"],
                  f"from source: 'Start with Windows (installed app only)' greyed ({sw})")
            tray = H.settings_switch(d, "Keep running in the tray")
            check(tray["enabled"] and tray["value"] == app._tray_pref, f"'Keep running in the tray' ({tray})")
            daily = H.settings_switch(d, "Check for updates daily")
            check(not daily["enabled"], f"from source: the daily check is greyed, as before ({daily})")
            pump_until(app, lambda: any("game_action" in st for st in s.state), 3)
            pump(app, 0.5)
            merged: dict = {}
            for st in s.state:
                merged.update(st)
            check(merged.get("game_action") == "none" and merged.get("memory_action") == "off"
                  and merged.get("resume_action") == "on", f"saved under the same state.json keys ({merged})")
            app.watch.set_game_action("Turn Strata off")
            app.watch.set_memory_action("Ask me")
            app.watch.set_resume_action("Ask me")
            pump(app, 0.5)

        # ---- Protected apps (Behaviour) and Usage history (About) still open
        H.show_settings_tab(d, "Behaviour")
        pump(app, 0.1)
        check(H.click_settings_button(d, "Protected apps") and isinstance(d._child, WhitelistModal),
              "Protected apps opens the protected-apps list")
        child = d._child
        if isinstance(child, WhitelistModal):
            pump_until(app, lambda: child._loaded)
            pump(app, 0.5)
            child._cancel_btn.invoke()
            check(pump_until(app, lambda: d._child is None, 3), "the list closes back to Settings")
        pump(app, 0.3)
        if d._history_btn is not None:
            H.show_settings_tab(d, "About")
            pump(app, 0.1)
            check(H.click_settings_button(d, "Usage history")
                  and pump_until(app, lambda: isinstance(d._child, history_dialog.HistoryDialog), 3),
                  "Usage history opens the history window")
            child = d._child
            if child is not None:
                pump(app, 0.6)
                child.close()
                check(pump_until(app, lambda: d._child is None, 3), "the history window closes back to Settings")
        pump(app, 0.3)
        check(H.close_settings(d) and H.open_dialog(app) is None, "Close (About tab) closes Settings")
    finally:
        app.destroy()
        gc.collect()


# ------------------------------------------------------------------------------------------------ llama.cpp
def _open_llama(app, fit: FakeFit | None = None):
    if fit is not None:
        app.fit_preview = fit                         # main.open_settings passes on_fit_preview=self.fit_preview
    d = H.open_settings(app)
    pump(app, 0.45)
    return d


def test_fit_largest_entry() -> None:
    """Context length > 'Largest that fits the GPU' in the window: where the entry sits, one check at a time, the
    measured size written as -c N, the answers that change nothing, and the greyed entry with its reason. The
    app's two calls are recorders here: nothing is measured and no tool runs."""
    base = [EXE, "-m", MODEL_A, "--port", "9931", "-c", "65536", "-np", "1"]
    app, det, ep = _llama_app(base, _found())
    why: list[str | None] = [None]
    asks: list[tuple] = []
    real_reason, real_largest = app.fit_largest_reason, app.fit_largest
    app.fit_largest_reason = lambda info: why[0]                                    # type: ignore[method-assign]
    app.fit_largest = lambda info, done: bool(asks.append((tuple(info.args), done)) or True)   # type: ignore[method-assign]
    label = "Largest that fits the GPU"
    try:
        d = _open_llama(app, FakeFit())
        if d is None:
            check(False, "Settings did not open")
            return
        menu = d._context_menu
        opt = H.settings_option(d, "Context length")
        check(opt["choices"][0].startswith("Auto") and opt["choices"][1] == label and opt["choices"].count(label) == 1,
              f"the entry is second in the Context list, after Auto ({opt['choices'][:3]})")
        check(str(menu._dropdown_menu.entrycget(1, "state")) == "normal" and not asks and d.fit_largest_calls == 0,
              "it can be picked; opening Settings measures nothing")
        note = d._fit_note
        with Saves() as s:
            H.choose_settings_option(d, "Context length", label)
            pump(app, 0.1)
            check(len(asks) == 1 and d.fit_largest_calls == 1 and asks[0][0] == tuple(base[1:]),
                  f"picking it asks the app once, for the arguments in the box ({asks and asks[0][0]})")
            check(d.fit_state == "checking" and _shown(note) and note.cget("text") == SD.FIT_CHECKING
                  and _args(d) == base[1:], "while it runs: 'Checking…' under the row, the arguments untouched")
            H.choose_settings_option(d, "Context length", label)
            pump(app, 0.1)
            check(len(asks) == 1, "picking it again while the check runs starts no second one")
            long = main.fit_largest_text(26112, "ok", 2.7)       # the app's own, longest result line
            check(long == "Fits the GPU at 26,112 tokens · measured with 2.7 GB of video memory in use by other apps",
                  f"the result line's wording ({long})")
            asks[0][1](26112, "ok", long)
            pump(app, 0.3)
            args = _args(d)
            check(args.count("-c") == 1 and args[args.index("-c") + 1] == "26112"
                  and [a for a in args if a not in ("26112",)] == [a for a in base[1:] if a != "65536"],
                  f"the answer is written as -c 26112, everything else kept ({args})")
            shown = H.settings_option(d, "Context length")["value"]
            check("26" in shown and shown != label, f"the row shows the size, not the entry's name ({shown!r})")
            check(d.fit_state == "largest" and _shown(note) and note.cget("text") == long
                  and str(note.cget("text_color")) != str(T.AMBER), "the result line under the row, not amber")
            path = U.shot(d, "settings_tabs_llama_largest")
            ok, info = _heights_fixed(app, d)
            check(ok, f"with the longest result line shown nothing is clipped ({info})")
            for tokens, state, text in ((None, "nofit", "Does not fit the GPU even at the smallest context llama.cpp tries"),
                                        (None, "unconfirmed", "Two checks disagreed (video memory in use changed?) · try again"),
                                        (None, "error", "The check gave no answer"), (4096, "weird", "x")):
                n = len(asks)
                before = _args(d)
                H.choose_settings_option(d, "Context length", label)
                pump(app, 0.1)
                check(len(asks) == n + 1, f"{state}: asked")
                asks[-1][1](tokens, state, text)
                pump(app, 0.3)
                check(_args(d) == before and d.fit_state == "warn" and note.cget("text") == text
                      and str(note.cget("text_color")) == str(T.AMBER)
                      and H.settings_option(d, "Context length")["value"] == shown,
                      f"{state}: nothing is written, the row shows what the arguments say, amber line")
            # the whole context fits: back to Auto
            H.choose_settings_option(d, "Context length", label)
            pump(app, 0.1)
            asks[-1][1](0, "full", "The model's whole context fits the GPU · left on Auto")
            pump(app, 0.3)
            check("-c" not in _args(d) and H.settings_option(d, "Context length")["value"].startswith("Auto")
                  and d.fit_state == "largest", f"'the whole context fits': -c removed, the row on Auto ({_args(d)})")
            # an answer belongs to the model, context and vision it was asked for: another edit made meanwhile is kept
            H.choose_settings_option(d, "Context length", label)
            pump(app, 0.1)
            d._args.set(d._args.get() + " --threads 4")
            pump(app, 0.4)
            asks[-1][1](8192, "ok", "Fits the GPU at 8,192 tokens")
            pump(app, 0.3)
            args = _args(d)
            check(args.count("-c") == 1 and args[args.index("-c") + 1] == "8192" and args[-4:-2] == ["--threads", "4"],
                  f"typing another option while the check runs: the answer is written and the typed option kept ({args})")
            check(not s.config and not s.state, "none of it is saved before Save")
            # ...and it is dropped when the model was changed meanwhile (the size was measured for the other model)
            H.choose_settings_option(d, "Context length", label)
            pump(app, 0.1)
            late = asks[-1][1]
            check(H.choose_settings_option(d, "Model", _label_of(d, MODEL_B)), "another model picked while the check runs")
            pump(app, 0.5)
            before = _args(d)
            late(4096, "ok", "Fits the GPU at 4,096 tokens")
            pump(app, 0.3)
            check(MODEL_B in before and _args(d) == before and "4096" not in before and d._fit_busy is False
                  and note.cget("text") != "Fits the GPU at 4,096 tokens",
                  f"an answer for the model that was replaced changes nothing ({_args(d)})")
            if "-c" in _args(d):                         # the greyed-entry checks below start from Auto
                H.choose_settings_option(d, "Context length", H.settings_option(d, "Context length")["choices"][0])
                pump(app, 0.4)
        # greyed out with the reason in its own text
        why[0] = "turn llama.cpp off first"
        d._resync()
        pump(app, 0.2)
        opt = H.settings_option(d, "Context length")
        blocked = SD.CONTEXT_FIT_BLOCKED.format(reason=why[0])
        check(opt["choices"][1] == blocked and label not in opt["choices"]
              and str(menu._dropdown_menu.entrycget(1, "state")) == "disabled",
              f"when it cannot be measured the entry is greyed and says why ({opt['choices'][1]!r})")
        n, before = len(asks), _args(d)
        menu._dropdown_callback(blocked)
        pump(app, 0.2)
        check(len(asks) == n and _args(d) == before and H.settings_option(d, "Context length")["value"].startswith("Auto"),
              "picking the greyed entry anyway (keyboard) measures nothing and the row shows the truth")
        why[0] = None
        d._resync()
        pump(app, 0.2)
        check(H.settings_option(d, "Context length")["choices"][1] == label
              and str(menu._dropdown_menu.entrycget(1, "state")) == "normal", "and it comes back when the reason is gone")
        ok, info = _heights_fixed(app, d)
        check(ok, f"with the result line shown nothing is clipped ({info})")
        H.close_settings(d)
        # the app's own answer while the engine is up: never measured then (its own video memory would count)
        from engines.base import CommandLaunch
        launch = CommandLaunch(program=EXE, args=tuple(base[1:]))
        ran: list[str] = []
        orig = app._run_worker
        app._run_worker = lambda name, fn, *a: ran.append(name) if name == "hc-fit-largest" else orig(name, fn, *a)  # type: ignore[method-assign]
        app.fit_largest_reason, app.fit_largest = real_reason, real_largest          # type: ignore[method-assign]
        for st in (main.ACTIVE, main.STARTING, main.STOPPING):
            app.state_name = st
            got = real_reason(launch)
            check(got == "turn llama.cpp off first" and real_largest(launch, lambda *x: None) is False and not ran,
                  f"the app, state {st}: '{got}', and no check is started")
        app.state_name = main.IDLE
        check(path is not None, "screenshot taken")
    finally:
        app.destroy()


def test_alias_switch() -> None:
    """Keep Hermes' "llama" alias in step: a row for llama.cpp only, greyed when hermes.exe was not found; the
    switch saves the choice. A stub-built app never runs hermes (no worker is started)."""
    app, det, ep = U.new_app()
    try:
        d = H.open_settings(app)
        check(d is not None and "hermes_alias" not in d.switches and app.alias.pref is False,
              "Strata: no alias row; the setting is off by default")
        if d is not None:
            H.close_settings(d)
    finally:
        app.destroy()
    app, det, ep = _llama_app([EXE, "-m", MODEL_A, "--port", "9931"], _found())
    ran: list[str] = []
    orig = app._run_worker
    app._run_worker = lambda name, fn, *a: ran.append(name) if name == "hc-alias" else orig(name, fn, *a)  # type: ignore[method-assign]
    try:
        check(app.alias.pref is False and app.alias.can_run is False and app.alias.exe is None,
              "a stub-built app: off, never runs hermes, does not look for it")
        d = _open_llama(app)
        if d is not None:
            sw = H.settings_switch(d, main.ALIAS_LABEL_MISSING)
            check(sw["caption"] == main.ALIAS_LABEL_MISSING and not sw["enabled"] and not sw["value"],
                  f"hermes.exe not found: the row is greyed and says so ({sw})")
            keys = list(d.switches)
            check("hermes_alias" in keys and keys.index("hermes_alias") == keys.index("autostart") + 1
                  and H.settings_tab_of(d, d.switches["hermes_alias"][1]) == "Behaviour",
                  f"the row sits on Behaviour, after Start with Windows ({keys})")
            with Saves() as s:
                H.toggle_settings_switch(d, main.ALIAS_LABEL_MISSING)
                pump(app, 0.5)
                check(app.alias.pref is False and not s.state, "a greyed switch cannot be turned on")
            ok, info = _heights_fixed(app, d)
            check(ok, f"llama.cpp with the alias row: one height, nothing clipped ({info})")
            H.show_settings_tab(d, "Behaviour")
            pump(app, 0.2)
            U.shot(d, "settings_tabs_llama_alias_missing")
            H.close_settings(d)
        app.alias.exe = os.path.join(str(TMP), "hermes", "bin", "hermes.exe")        # found (never run: gated)
        d = _open_llama(app)
        if d is not None:
            sw = H.settings_switch(d, main.ALIAS_LABEL)
            check(sw["caption"] == main.ALIAS_LABEL and sw["enabled"] and not sw["value"],
                  f"hermes.exe found: the row is live and off ({sw})")
            with Saves() as s:
                H.toggle_settings_switch(d, main.ALIAS_LABEL)
                check(app.alias.pref is True, "the switch -> alias.set_pref")
                pump_until(app, lambda: any("hermes_alias" in st for st in s.state), 3)
                check(any(st.get("hermes_alias") is True for st in s.state) and not s.config,
                      f"saved as hermes_alias in state.json, config.json untouched ({s.state})")
                H.toggle_settings_switch(d, main.ALIAS_LABEL)
                pump_until(app, lambda: any(st.get("hermes_alias") is False for st in s.state), 3)
                check(app.alias.pref is False and any(st.get("hermes_alias") is False for st in s.state),
                      "and off again")
            H.show_settings_tab(d, "Behaviour")
            pump(app, 0.2)
            U.shot(d, "settings_tabs_llama_alias")
            H.close_settings(d)
        check(not ran, f"no hermes worker was started ({ran})")
        # The controller reads the app through its host. With the test gate lifted for a moment and recorders in
        # place of the worker launcher and the note, a serving app starts exactly one update for the model and the
        # port it serves, and the result becomes the window's note. Nothing is pumped, nothing runs.
        calls: list = []
        notes: list[str] = []
        keep = (app.state_name, app.loaded, app.model_id, app._set_note)
        app._run_worker = lambda name, fn, *a: calls.append((name, fn, a)) if name == "hc-alias" else orig(name, fn, *a)  # type: ignore[method-assign]
        app._set_note = notes.append  # type: ignore[method-assign]
        url = "http://127.0.0.1:9931/v1"
        try:
            app.alias.can_run, app.alias.pref, app.alias.done = True, True, None
            app.loaded, app.model_id = True, "Qwen-W"         # so that only the state can refuse
            app.alias.check()
            check(not calls and app.alias.done is None and app.state_name != main.ACTIVE,
                  "wired to the app's state: nothing starts while the engine is off")
            app.state_name = main.ACTIVE
            app._destroyed = True
            app.alias.check()
            app._destroyed = False
            check(not calls and app.alias.done is None, "wired to the app's closing flag: nothing starts while it closes")
            app.loaded = False
            app.alias.check()
            app.loaded = True
            check(not calls, "wired to the app's loaded flag: nothing starts while the model loads")
            app.alias.check()
            check(len(calls) == 1 and calls[0][0] == "hc-alias" and calls[0][1] is main._alias_worker
                  and calls[0][2][:3] == (app.alias.exe, "Qwen-W", url) and calls[0][2][4] == app.alias.result,
                  f"serving: one update for the app's model and port, through the app's worker launcher ({calls})")
            import hermes_alias
            app.alias.result(("Qwen-W", url), hermes_alias.AliasResult(True, True, "x"))
            check(notes == ["Hermes alias llama updated\nQwen-W"], f"the result is shown as the window's note ({notes})")
        finally:
            app.alias.can_run, app.alias.pref, app.alias.done, app.alias.busy = False, False, None, False
            app.state_name, app.loaded, app.model_id, app._set_note = keep  # type: ignore[method-assign]
    finally:
        app.destroy()


def test_download_link() -> None:
    """Download a model…: only for an engine with .gguf models; the link closes Settings and opens the app's
    download window (here a recorder: no window, no network)."""
    app, det, ep = U.new_app()
    try:
        d = H.open_settings(app)
        check(d is not None and d._download_btn is None, "Strata: no download link")
        check(app._download_dir().endswith("llama-models"), f"no model in use: <Downloads>\\llama-models ({app._download_dir()})")
        if d is not None:
            H.close_settings(d)
    finally:
        app.destroy()
    app, det, ep = _llama_app([EXE, "-m", MODEL_A, "--port", "9931"], _found())
    opened: list = []
    app.open_download = lambda: opened.append(1)      # type: ignore[method-assign]
    try:
        d = _open_llama(app)
        check(d is not None and d._download_btn is not None and d._download_btn.text == SD.DOWNLOAD_LABEL,
              "llama.cpp: the link is there")
        if d is not None and d._download_btn is not None:
            d._args.set(d._args.get() + " -c 8192")       # an unsaved change
            with Saves() as s:
                d._download_btn.invoke()
                check(pump_until(app, lambda: bool(opened) and H.open_dialog(app) is None, 3),
                      "the link closes Settings and opens the download window")
                check(not s.config, "nothing is saved by it (unsaved changes are dropped, as Cancel does)")
    finally:
        app.destroy()


def test_llama_tabs_and_controls() -> None:
    SD.FIT_DEBOUNCE_MS = 200                          # the debounce itself is the same code; shorter for the test
    fit = FakeFit()
    app, det, ep = _llama_app([EXE, "-m", MODEL_A, "--port", "9931"], _found())
    try:
        d = _open_llama(app, fit)
        check(d is not None, "llama.cpp: Settings opens")
        if d is None:
            return
        ok, have = _tab_texts_ok(d, "Model", ["Engine", "Build", "Model", "Context length", "Vision", "Port",
                                              SD.ADVANCED_TITLE])
        check(ok and "llama.cpp launcher" not in have, f"llama.cpp Model tab: labelled rows + Advanced ({have})")
        check(d.option_rows == 2, f"option_rows counts Build + Model ({d.option_rows})")
        check(not d.advanced_open and not _shown(d._adv_body),
              "Advanced is folded by default (configured, everything shown by the dropdowns)")
        check(H.settings_option(d, "Build")["value"].startswith("CUDA")
              and H.settings_option(d, "Model")["value"] == _label_of(d, MODEL_A)
              and H.settings_option(d, "Context length")["value"] == "Auto (llama.cpp decides)"
              and H.settings_option(d, "Vision")["value"] == SD.VISION_OFF
              and H.settings_option(d, "Port")["value"] == "Own port · 9931",
              f"the rows show the arguments ({[H.settings_option(d, x)['value'] for x in ('Build', 'Model', 'Context length', 'Vision')]})")
        check(not fit.calls, "opening Settings asks nothing about the GPU (only changes do)")
        ok, have = _tab_texts_ok(d, "Behaviour", ["A game starts", "Memory runs low", "That app closes",
                                                  SD.REOPEN_LABEL, SD.PROTECTED_LABEL, "Keep running in the tray"])
        check(ok, f"llama.cpp Behaviour tab: the same rows ({have})")
        ok, have = _tab_texts_ok(d, "About", ["Version", "Endpoint", "Open llama.cpp dashboard", "Copy"])
        check(ok, f"llama.cpp About tab ({have})")
        ok, have = _tab_texts_ok(d, "Model", ["Model", SD.DOWNLOAD_LABEL, "Context length"])
        order = [have.index(t) for t in ("Model", SD.DOWNLOAD_LABEL, "Context length")] if ok else []
        check(ok and order == sorted(order) and d._download_btn is not None
              and H.settings_tab_of(d, d._download_btn) == "Model",
              f"llama.cpp Model tab: '{SD.DOWNLOAD_LABEL}' sits under Model, before Context length ({have})")
        check(os.path.normcase(app._download_dir()) == os.path.normcase(os.path.dirname(MODEL_A)),
              f"a download goes beside the model in use ({app._download_dir()})")

        # ---- context length: rewrites -c, follows typed arguments, a custom value
        check(H.choose_settings_option(d, "Context length", "32K tokens") and _args(d)[-2:] == ["-c", "32768"],
              f"32K tokens -> -c 32768 ({_args(d)})")
        check(H.choose_settings_option(d, "Context length", "Auto (llama.cpp decides)") and "-c" not in _args(d),
              f"Auto removes -c ({_args(d)})")
        d._args.set(d._args.get() + " -c 48000")
        pump(app, 0.05)
        ctx = H.settings_option(d, "Context length")
        check(ctx["value"] == "48,000 tokens (custom)" and "48,000 tokens (custom)" not in ctx["choices"],
              f"a typed value that is not a choice: '48,000 tokens (custom)' ({ctx['value']})")
        d._args.set(d._args.get().replace("-c 48000", "--ctx-size 16384"))
        pump(app, 0.05)
        check(H.settings_option(d, "Context length")["value"] == "16K tokens", "typed --ctx-size 16384 -> 16K tokens")
        H.choose_settings_option(d, "Context length", "Auto (llama.cpp decides)")

        # ---- vision: the list follows the model; on / off; a file the list does not have
        vis = H.settings_option(d, "Vision")
        check(vis["enabled"] and vis["choices"] == [SD.VISION_OFF, "On · mmproj-F16"],
              f"model A: Off + its projector ({vis})")
        check(H.choose_settings_option(d, "Vision", "On · mmproj-F16")
              and LF.vision_of(_args(d)) == PROJ_A, f"Vision on -> --mmproj <A's file> ({_args(d)})")
        check(H.choose_settings_option(d, "Vision", SD.VISION_OFF) and LF.vision_of(_args(d)) is None,
              f"Vision off removes it ({_args(d)})")
        d._args.set(d._args.get() + f' --mmproj "{STRAY_PROJ}"')
        pump(app, 0.05)
        check(H.settings_option(d, "Vision")["value"] == SD.VISION_OTHER and d.advanced_open,
              "a projector the list does not have: 'Another file (in the arguments)', Advanced opens")
        d.set_advanced(False)
        H.choose_settings_option(d, "Vision", SD.VISION_OFF)
        check(LF.vision_of(_args(d)) is None, "and Off removes it")

        # ---- model change: each model has its own context, vision file and extras; the port is shared
        H.choose_settings_option(d, "Vision", "On · mmproj-F16")
        H.choose_settings_option(d, "Context length", "32K tokens")
        check(H.choose_settings_option(d, "Model", _label_of(d, MODEL_C)) and LF.vision_of(_args(d)) is None
              and LF.context_of(_args(d)) is None and LF.port_of(_args(d)) == 9931
              and LF.option_value(_args(d), ("--alias", "-a")) == "Qwen-C-Q4"
              and d._msg.cget("text") != SD.VISION_DROPPED,
              f"model C for the first time: no vision, no context, the same port, its short name ({_args(d)})")
        check(H.choose_settings_option(d, "Vision", "On · mmproj-F16") and LF.vision_of(_args(d)) == PROJ_C,
              f"C's own projector can be turned on ({_args(d)})")
        check(H.choose_settings_option(d, "Model", _label_of(d, MODEL_B)) and LF.vision_of(_args(d)) is None
              and LF.context_of(_args(d)) is None, f"model B for the first time: nothing follows from C ({_args(d)})")
        vis = H.settings_option(d, "Vision")
        check(not vis["enabled"] and vis["value"] == SD.VISION_NONE, f"model B: '{SD.VISION_NONE}', greyed ({vis})")
        check(H.choose_settings_option(d, "Model", _label_of(d, MODEL_A)) and LF.vision_of(_args(d)) == PROJ_A
              and LF.context_of(_args(d)) == 32768, f"back to A: its vision file and its context ({_args(d)})")
        check(H.choose_settings_option(d, "Model", _label_of(d, MODEL_C)) and LF.vision_of(_args(d)) == PROJ_C
              and LF.context_of(_args(d)) is None, f"and C has its own again ({_args(d)})")
        d._args.set(f'-m "{MODEL_A}" --port 9931')
        pump(app, 0.05)
        check(H.settings_option(d, "Model")["value"] == _label_of(d, MODEL_A)
              and H.settings_option(d, "Vision")["choices"] == [SD.VISION_OFF, "On · mmproj-F16"]
              and H.settings_option(d, "Context length")["value"] == "Auto (llama.cpp decides)",
              "typed arguments: Model, Vision list and Context follow")

        # ---- port: a dropdown - the other engine's port (clients need no change) or this engine's own port;
        # any other port is typed in the arguments and the row says so
        port = H.settings_option(d, "Port")
        same = port["choices"][0] if port["choices"] else ""
        check(len(port["choices"]) == 2 and same.startswith("Same as Strata · ") and port["choices"][1] == "Own port · 9931"
              and port["value"] == "Own port · 9931" and port["enabled"] and d._port_entry is None,
              f"Port row: the other engine's port, or its own ({port})")
        shared = int(same.rsplit(" ", 1)[-1]) if same else 0
        check(H.choose_settings_option(d, "Port", same) and LF.port_of(_args(d)) == shared and shared != 9931
              and str(d._save_btn.cget("state")) == "normal",
              f"'{same}' writes --port {shared}, can be saved ({_args(d)})")
        pump(app, 0.05)
        check(H.settings_option(d, "Port")["value"] == same and H.settings_option(d, "Port")["choices"] == port["choices"],
              "the row shows it; the choices stay the two")
        d._args.set(d._args.get().replace(f"--port {shared}", "--port 9940"))
        pump(app, 0.05)
        check(H.settings_option(d, "Port")["value"] == SD.PORT_OTHER.format(port=9940),
              f"typed --port 9940: the row says it is in the arguments ({H.settings_option(d, 'Port')['value']})")
        check(H.choose_settings_option(d, "Port", SD.PORT_OTHER.format(port=9940)) and LF.port_of(_args(d)) == 9940,
              "picking that entry changes nothing")
        check(H.choose_settings_option(d, "Port", "Own port · 9931") and LF.port_of(_args(d)) == 9931,
              f"back to its own port ({_args(d)})")
        pump(app, 0.3)
        check(H.settings_option(d, "Port")["value"] == "Own port · 9931", "and the row follows")

        # ---- the GPU-fit note
        fit.calls.clear()
        for choice in ("8K tokens", "16K tokens", "32K tokens"):
            H.choose_settings_option(d, "Context length", choice)
        pump(app, 0.05)
        check(not fit.calls, "nothing asked during a burst of changes")
        pump(app, 0.5)
        check(len(fit.calls) == 1 and "32768" in fit.calls[0][0] and fit.calls[0][2],
              f"debounced: one request for the burst, with the last value, on the Tk thread ({len(fit.calls)})")
        pump(app, 0.5)
        check(len(fit.calls) == 1, "and not repeated")
        fit.calls[0][1]("Won't fully fit on the GPU · will run partly on the CPU (slower)")
        pump(app, 0.05)
        note = d._fit_note
        check(_shown(note) and note.cget("text").startswith("Won't fully fit") and str(note.cget("text_color"))
              == str(T.AMBER) and d.fit_state == "warn", "a warning: amber line under Context length")
        check(int(note.grid_info()["row"]) == int(d._context_menu.master.grid_info()["row"]) + 1,
              "the note sits right under Context length")
        d.set_advanced(False)
        U.shot(d, "settings_tabs_llama_model")
        need = H.settings_tab_req_heights(d)
        d.set_advanced(True)
        need2 = H.settings_tab_req_heights(d)
        check(max(max(need.values()), max(need2.values())) <= d.logical_height + 1,
              f"with the note and Advanced open nothing is clipped ({need2} vs {d.logical_height})")
        U.shot(d, "settings_tabs_llama_advanced")
        d.set_advanced(False)
        # an answer to an older request is ignored
        H.choose_settings_option(d, "Context length", "64K tokens")
        check(not _shown(note) and d.fit_state is None, "a new change clears the old note")
        pump(app, 0.4)
        first = fit.calls[-1]
        H.choose_settings_option(d, "Context length", "16K tokens")
        pump(app, 0.4)
        second = fit.calls[-1]
        check(len(fit.calls) == 3 and first is not second, f"one request per pause ({len(fit.calls)})")
        first[1]("stale warning")
        pump(app, 0.05)
        check(not _shown(note) and d.fit_state is None, "the answer to the older request is ignored")
        second[1]("")
        pump(app, 0.05)
        check(_shown(note) and note.cget("text") == SD.FITS_TEXT and str(note.cget("text_color")) == str(T.TEXT_2)
              and d.fit_state == "fits", "'' -> 'Fits on the GPU' in the secondary colour")
        second[1](None)
        pump(app, 0.05)
        check(not _shown(note), "None -> the note goes")
        # nothing will be checked: no note
        fit.ret = False
        H.choose_settings_option(d, "Context length", "32K tokens")
        pump(app, 0.4)
        check(len(fit.calls) == 4 and not _shown(note) and d.fit_state is None, "False -> no note")
        fit.ret = True
        # not a launch the engine accepts: no request
        n = len(fit.calls)
        missing = str(TMP / "missing.gguf")
        d._args.set(f'-m "{missing}" --port 9931 -c 32768')
        pump(app, 0.4)
        check(len(fit.calls) == n and str(d._save_btn.cget("state")) == "disabled",
              "a launch the engine refuses: no GPU request")
        d._args.set(f'-m "{MODEL_A}" --port 9931 -c 32768')
        pump(app, 0.4)
        check(len(fit.calls) == n + 1, "valid again: one request")
        # a late answer after Settings closed touches nothing (checked below once it is closed)
        late = fit.calls[-1][1]

        # ---- the tabs: one fixed height; Close on the others; nothing saved by switching
        with Saves() as s:
            ok, info = _heights_fixed(app, d)
            check(ok, f"llama.cpp: one fixed height on every tab, nothing clipped ({info})")
            check(d.logical_height < 600, f"llama.cpp: clearly shorter than the old ~700 px ({d.logical_height})")
            print(f"       llama.cpp Settings: {d.logical_height} px, tabs {d.tab_heights}")
            H.show_settings_tab(d, "Behaviour")
            pump(app, 0.1)
            U.shot(d, "settings_tabs_llama_behaviour")
            H.show_settings_tab(d, "About")
            pump(app, 0.1)
            U.shot(d, "settings_tabs_llama_about")
            H.show_settings_tab(d, "Model")
            pump(app, 0.5)
            check(not s.config and not s.state, f"switching tabs saves nothing ({s.config}, {s.state})")
            check(app.cfg["launch_cmd"] == [EXE, "-m", MODEL_A, "--port", "9931"], "and changes no setting")

        # ---- Save saves exactly the arguments shown
        H.choose_settings_option(d, "Vision", "On · mmproj-F16")
        H.choose_settings_option(d, "Port", same)           # one port for both engines
        pump(app, 0.1)
        shown = [d._program.get()] + _args(d)
        with Saves() as s:
            check(H.click_settings_button(d, "Save"), "Save clicked")
            check(pump_until(app, lambda: s.config and H.open_dialog(app) is None, 4), "Save writes and closes")
            got = s.config[-1].get("launch_cmd") if s.config else None
            check(got == shown and LF.vision_of(got[1:]) == PROJ_A and LF.context_of(got[1:]) == 32768,
                  f"saved launch_cmd == program + the arguments shown ({got} vs {shown})")
            check(bool(s.config) and s.config[-1].get("port") == shared
                  and s.config[-1].get("endpoint_url") == f"http://127.0.0.1:{shared}/v1",
                  f"the port and the endpoint come from --port, as before ({shared})")
        late("too late")
        pump(app, 0.1)
        check(H.open_dialog(app) is None, "a late GPU answer after closing touches nothing")
    finally:
        SD.FIT_DEBOUNCE_MS = 600
        app.destroy()


def test_advanced_fold() -> None:
    # nothing configured and nothing found: Advanced is open (it is the only way to set it up)
    app, det, ep = _llama_app([], _found(models=False, builds=False))
    try:
        d = _open_llama(app)
        check(d is not None and d.advanced_open and _shown(d._program_entry) and _shown(d._adv_body)
              and d.option_rows == 0, "not configured, nothing found: Advanced opens by itself")
        if d is not None:
            check(H.settings_option(d, "Context length")["enabled"] and H.settings_option(d, "Port")["enabled"],
                  "the context / vision / port controls are there for a hand-typed set-up")
            H.close_settings(d)
    finally:
        app.destroy()
    # a program that is not one of the builds found: open
    app, det, ep = _llama_app([OTHER_EXE, "-m", MODEL_A, "--port", "9931"], _found())
    try:
        d = _open_llama(app)
        check(d is not None and d.advanced_open and H.settings_option(d, "Build")["value"] == "Another program (below)",
              "a program the Build list does not have: Advanced opens")
        if d is not None:
            H.close_settings(d)
    finally:
        app.destroy()
    # a model file that is not one of the models found: open
    app, det, ep = _llama_app([EXE, "-m", str(TMP / "stray" / "Other.gguf"), "--port", "9931"], _found())
    try:
        d = _open_llama(app)
        check(d is not None and d.advanced_open, "a model the Model list does not have: Advanced opens")
        if d is not None:
            H.close_settings(d)
    finally:
        app.destroy()
    # configured with what was found: folded; the header opens and closes it
    app, det, ep = _llama_app([EXE, "-m", MODEL_B, "--port", "9931"], _found())
    try:
        d = _open_llama(app)
        check(d is not None and not d.advanced_open, "configured from the lists: folded")
        if d is not None:
            head = next(w for w in H._walk(d._adv_head) if H._text(w) == SD.ADVANCED_TITLE)
            H._click_label(head)
            pump(app, 0.1)
            check(d.advanced_open and _shown(d._adv_body) and d._program_entry.winfo_ismapped(),
                  "a click on the header unfolds the program and arguments boxes")
            H._click_label(head)
            pump(app, 0.1)
            check(not d.advanced_open and not _shown(d._adv_body), "and folds them again")
            d._args.set(d._args.get() + " -ngl 99")
            pump(app, 0.05)
            check(not d.advanced_open, "extra options the dropdowns do not cover keep it folded (they are kept as typed)")
            H.close_settings(d)
    finally:
        app.destroy()


def run() -> int:
    for t in (test_strata_tabs, test_fit_largest_entry, test_alias_switch, test_download_link,
              test_llama_tabs_and_controls,
              test_advanced_fold):
        print(f"-- {t.__name__}")
        try:
            t()
        except Exception:
            FAILS.append(f"{t.__name__} raised:\n{traceback.format_exc()}")
            print(FAILS[-1])
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
