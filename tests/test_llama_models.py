"""llama_models: per-model settings for llama.cpp. Pure; no processes, no network, never the real %APPDATA%."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AGENT_START_HOME"] = tempfile.mkdtemp(prefix="as_lm_")     # config_store: never the real data folder

import config_store  # noqa: E402
import engine_profiles  # noqa: E402
import engines  # noqa: E402
import llama_finder as LF  # noqa: E402
import llama_models as LM  # noqa: E402
from engines.base import CommandLaunch  # noqa: E402
from engines.llama_cpp import LlamaLaunch  # noqa: E402

PASSED = FAILED = 0


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print(f"[FAIL] {msg}")


PROG = r"C:\AI\llama\llama-server.exe"
A = r"C:\models\Alpha-27B-IQ4_XS.gguf"
B = r"C:\models\Beta-27B-Q3_K_XL.gguf"
C = r"D:\other\Gamma.gguf"
PROJ_A = r"C:\models\mmproj-alpha.gguf"
KA, KB, KC = (LM.model_key(p) for p in (A, B, C))


def yes(_p: str) -> bool:
    return True


def no(_p: str) -> bool:
    return False


def t_split() -> None:
    p = LM.split(("-m", A, "--port", "9931", "-c", "65536"))
    check(p == LM.Parts(A, ("--port", "9931"), ("-c", "65536")), f"the usual launch ({p})")
    p = LM.split(("-c", "8192", "--host", "127.0.0.1", "--model", f'"{A}"', "--api-key", "k", "-np", "1",
                  "--ssl-key-file", "x.pem", "--jinja", "--port", "8080"))
    check(p.model == A and p.shared == ("--host", "127.0.0.1", "--api-key", "k", "--ssl-key-file", "x.pem",
                                        "--port", "8080")
          and p.own == ("-c", "8192", "-np", "1", "--jinja"), f"shared options are picked out in order ({p})")
    p = LM.split((f"--model={A}", "--port=9931", "--api_key", "k", "--temp", "0.2"))
    check(p.model == A and p.shared == ("--port=9931", "--api_key", "k") and p.own == ("--temp", "0.2"),
          f"--x=value and --under_score spellings ({p})")
    p = LM.split(("-m", A, "-m", B, "-c", "1"))
    check(p.model == B and p.own == ("-c", "1"), "the last -m is the model, as the server reads it")
    p = LM.split(("-hf", "org/repo", "--hf-file", "x.gguf", "--port", "1", "-c", "2"))
    check(p == LM.Parts(None, ("--port", "1"), ("-c", "2")), f"a model from the network: no model file ({p})")
    check(LM.split(("-m",)) == LM.Parts(None, (), ()) and LM.split(("--port",)).shared == ("--port",),
          "an option without its value does not raise")
    for junk in (None, "text", b"x", 5, object()):
        check(LM.split(junk) == LM.Parts(None, (), ()), f"junk {type(junk).__name__}: empty parts")
    check(LM.split([1, 2]).own == ("1", "2"), "non-text arguments are read as text")


def t_keys_names() -> None:
    check(LM.model_key(A) == os.path.normcase(A) and LM.model_key(A.upper()) == LM.model_key(A)
          and LM.model_key(A.replace("\\", "/")) == LM.model_key(A) and LM.model_key(f'"{A}"') == LM.model_key(A),
          "one key for every spelling of a path")
    check(LM.model_key(r"m\x.gguf", r"C:\AI\llama") == os.path.normcase(r"C:\AI\llama\m\x.gguf"),
          "a relative model counts from the program's folder")
    check(LM.model_key("") == "" and LM.model_key(None) == "" and LM.model_key("  ") == "", "nothing to key on")
    check(LM.short_name(A) == "Alpha-27B-IQ4_XS" and LM.short_name("C:/m/x.GGUF") == "x"
          and LM.short_name(r"C:\m\big-00001-of-00003.gguf") == "big", "the short name: file name without .gguf / split")
    check(LM.short_name(r"C:\m\a,b.gguf") == "" and LM.short_name(r"C:\m\-x.gguf") == "" and LM.short_name("") == ""
          and LM.short_name(None) == "", "no short name when it would not survive as one --alias value")
    check(LM.with_short_name(("-c", "1"), A) == ("-c", "1", "--alias", "Alpha-27B-IQ4_XS"), "alias added")
    for have in (("--alias", "mine"), ("-a", "mine"), ("--alias=mine",)):
        check(LM.with_short_name(have, A) == have, f"an alias the arguments have is kept ({have})")
    check(LM.with_short_name(("-c", "1"), r"C:\m\a,b.gguf") == ("-c", "1"), "no usable short name: nothing added")
    fv = LM.first_visit(("-c", "65536", "-np", "1", "--mmproj", PROJ_A, "--alias", "x", "--jinja", "--ctx-size=4",
                         "--no-mmproj", "-a", "y", "-mmu", "http://e/x"))
    check(fv == ("-np", "1", "--jinja"), f"first visit: no context, vision or alias ({fv})")


def t_session() -> None:
    cfg: dict = {}
    s = LM.Session(cfg, exists=yes)
    cur = ("-m", A, "--port", "9931", "-c", "65536", "-np", "1", "--mmproj", PROJ_A)
    b = s.switch(cur, B)
    check(b == ("-m", B, "--port", "9931", "-np", "1", "--alias", "Beta-27B-Q3_K_XL"),
          f"first visit of B: shared + extras, no context / vision, its short name ({b})")
    back = s.switch(b, A)
    check(back == ("-m", A, "--port", "9931", "-c", "65536", "-np", "1", "--mmproj", PROJ_A, "--alias",
                   "Alpha-27B-IQ4_XS"), f"back to A: its own arguments, and its short name once ({back})")
    check(cfg == {}, "the config is not touched by a session")
    snap = dict(s.touched())
    check(set(snap) == {KA, KB} and json.loads(snap[KB]) == {"alias": True, "args": ["-np", "1", "--alias",
                                                                                     "Beta-27B-Q3_K_XL"]},
          f"touched: both models, as JSON ({snap})")
    # edits made to B before leaving it are remembered within the session
    b2 = LF.with_context(s.switch(back, B), 32768)
    a2 = s.switch(b2, A)
    b3 = s.switch(a2, B)
    check(LF.context_of(b3) == 32768 and LF.context_of(a2) == 65536, "each model keeps its own context")
    # the shared options follow the model that is shown
    a3 = s.switch(LF.with_port(b3, 8080) + ("--api-key", "sek"), A)
    check(LM.split(a3).shared == ("--port", "8080", "--api-key", "sek") and LF.context_of(a3) == 65536,
          f"port and key are shared ({a3})")
    # an alias the user removed is not put back
    no_alias = LF.with_option(a3, LM.ALIAS_FLAGS, None)
    again = s.switch(s.switch(no_alias, B), A)
    check(LF.option_value(again, LM.ALIAS_FLAGS) is None, f"a removed alias stays removed ({again})")
    # the same model: only the path text is replaced
    same = s.switch(("-c", "1", "-m", A.upper(), "--port", "2"), A)
    check(same == ("-c", "1", "-m", A, "--port", "2"), f"same model: nothing swaps ({same})")
    # a model named by -hf has nothing to remember
    s2 = LM.Session({})
    out = s2.switch(("-hf", "org/repo", "--port", "9931", "-c", "4096", "-np", "2"), A)
    check(out == ("-m", A, "--port", "9931", "-np", "2", "--alias", "Alpha-27B-IQ4_XS")
          and set(dict(s2.touched())) == {KA}, f"from a network model ({out})")
    # a remembered vision file that is gone is taken out
    table = {LM.KEY: {KA: {"args": ["-c", "8192", "--mmproj", PROJ_A], "alias": True}}}
    gone = LM.Session(table, exists=no).switch(("-m", B, "--port", "1"), A)
    there = LM.Session(table, exists=yes).switch(("-m", B, "--port", "1"), A)
    check(LF.vision_of(gone) is None and LF.context_of(gone) == 8192 and LF.vision_of(there) == PROJ_A,
          f"a deleted vision file is dropped, an existing one kept ({gone})")
    # first set-up
    s3 = LM.Session({})
    fs = s3.first_setup(A, 9931)
    check(fs == ("-m", A, "--port", "9931", "--alias", "Alpha-27B-IQ4_XS") and set(dict(s3.touched())) == {KA},
          f"first set-up: model, port, short name ({fs})")
    # hostile / junk input never raises
    for bad_args in (None, "x", 5, ("-m",), ("--alias",)):
        try:
            r = LM.Session({LM.KEY: "junk"}).switch(bad_args, A)
            check(isinstance(r, tuple) and r[:2] == ("-m", A), f"junk arguments {bad_args!r} -> {r}")
        except Exception as exc:  # noqa: BLE001
            check(False, f"junk arguments {bad_args!r} raised {exc!r}")


def t_apply() -> None:
    # a config from before this version: no table; the current arguments are the current model's settings
    old = ["-m", A, "--port", "9931", "-c", "65536"]
    cfg: dict = {"launch_cmd": [PROG, *old]}
    shallow = dict(cfg)
    args_b, snap = LM.switch_args(cfg, old, B)
    check(args_b == ("-m", B, "--port", "9931", "--alias", "Beta-27B-Q3_K_XL") and LM.KEY not in cfg,
          f"-c 65536 does not follow to another model ({args_b})")
    LM.apply(cfg, old, args_b, snap)
    check(cfg[LM.KEY] == {KA: {"args": ["-c", "65536"]},
                          KB: {"args": ["--alias", "Beta-27B-Q3_K_XL"], "alias": True}},
          f"the outgoing and the incoming model are remembered ({cfg.get(LM.KEY)})")
    check(LM.KEY not in shallow, "a shallow copy of the config taken before keeps its (absent) table")
    before = copy.deepcopy(cfg)
    trial = dict(cfg)
    LM.apply(trial, args_b, LF.with_context(args_b, 8192), ())
    check(cfg == before and trial[LM.KEY][KB]["args"] == ["--alias", "Beta-27B-Q3_K_XL", "-c", "8192"]
          and trial[LM.KEY] is not cfg[LM.KEY], "applying to a trial copy never changes the real table")
    LM.apply(cfg, args_b, args_b, ())
    check(cfg == before, "applying the same launch again changes nothing")
    args_a, snap = LM.switch_args(cfg, args_b, A)
    check(args_a == ("-m", A, "--port", "9931", "-c", "65536", "--alias", "Alpha-27B-IQ4_XS"),
          f"A comes back with its context, and gets its short name once ({args_a})")
    LM.apply(cfg, args_b, args_a, snap)
    check(cfg[LM.KEY][KA] == {"args": ["-c", "65536", "--alias", "Alpha-27B-IQ4_XS"], "alias": True},
          "A's entry carries the alias mark now")
    # old_args None: the config held another engine's command
    cfg2: dict = {"launch_cmd": ["python.exe", "server.py", "-m", "not-a-model", "--port", "8080"]}
    LM.apply(cfg2, None, args_a, ())
    check(set(cfg2[LM.KEY]) == {KA}, "another engine's command is never read as a model's settings")
    # junk never raises, and a launch without a model file leaves no entry
    cfg3: dict = {LM.KEY: ["junk"]}
    LM.apply(cfg3, ("-hf", "o/r"), ("-hf", "o/r", "--port", "1"), "junk")
    check(LM.KEY not in cfg3, "no model file: no table")
    for junk in (None, 5, "x"):
        LM.apply({}, junk, junk, junk)
    check(True, "junk input does not raise")
    # the table is bounded, newest kept
    big: dict = {}
    for i in range(LM.MAX_MODELS + 5):
        LM.apply(big, None, ("-m", rf"C:\m\{i}.gguf", "-c", "1"), ())
    check(len(big[LM.KEY]) == LM.MAX_MODELS and LM.model_key(rf"C:\m\{LM.MAX_MODELS + 4}.gguf") in big[LM.KEY]
          and LM.model_key(r"C:\m\0.gguf") not in big[LM.KEY], "at most MAX_MODELS entries, the oldest go first")
    # validation of what is read
    bad = {LM.KEY: {KA: {"args": "x"}, KB: {"args": ["-c", 1]}, "": {"args": []}, KC: {"args": ["a"], "alias": "yes",
                                                                                      "fit_context": 5, "x": 1}}}
    check(LM.table_of(bad) == {KC: {"args": ["a"]}}, f"junk entries and junk fields are dropped ({LM.table_of(bad)})")
    check(LM.table_of(None) == {} and LM.table_of({LM.KEY: 3}) == {} and LM.entry_of(bad, C) == {"args": ["a"]}
          and LM.entry_of(bad, A) is None, "table_of / entry_of on junk")
    check(LM.same(cfg, snap) and not LM.same({}, snap) and LM.same(cfg, ()) and LM.same(cfg, "junk"),
          "same(): the entries a launch carries are already stored / not")


def t_fit_context() -> None:
    args = ("-m", A, "--port", "9931", "-c", "26112")
    snap = LM.set_fit_context((), {}, args, 26112)
    check(json.loads(dict(snap)[KA]) == {"args": ["-c", "26112"], "fit_context": 26112}, f"the mark is set ({snap})")
    check(LM.fit_context_of({}, snap, args) == 26112, "and read while the arguments name that context")
    check(LM.fit_context_of({}, snap, LF.with_context(args, 8192)) is None, "another context: no mark")
    cfg: dict = {}
    LM.apply(cfg, None, args, snap)
    check(cfg[LM.KEY][KA].get("fit_context") == 26112 and LM.fit_context_of(cfg, (), args) == 26112, "it is saved")
    LM.apply(cfg, args, LF.with_context(args, 32768), ())
    check("fit_context" not in cfg[LM.KEY][KA], "and dropped when the model's context changes")
    check(LM.set_fit_context((), {}, ("-hf", "o/r", "-c", "1"), 1) == (), "no model file: nothing to mark")
    check(LM.set_fit_context((), {}, args, 999) == ((KA, json.dumps({"args": ["-c", "26112"]}, sort_keys=True)),),
          "a size the arguments do not carry is not marked")


def t_engine() -> None:
    eng = engines.get_engine({"backend": "llama_cpp"})
    models = [LF.Model(path=p, name=os.path.basename(p)[:-5], size_bytes=2 ** 30, folder=os.path.dirname(p))
              for p in (A, B)]
    found = SimpleNamespace(models=tuple(models), builds=(), choice=SimpleNamespace(build=None, model=None))
    cfg = {"backend": "llama_cpp", "launch_cmd": [PROG, "-m", A, "--port", "9931", "-c", "65536"],
           "launch_cwd": os.path.dirname(PROG), "port": 9931, "endpoint_url": "http://127.0.0.1:9931/v1"}
    saved = copy.deepcopy(cfg)
    check(eng.same_launch(cfg, CommandLaunch(PROG, tuple(cfg["launch_cmd"][1:]))) and LM.KEY not in cfg,
          "an existing config (no table) is the same launch as before: nothing to save")
    choices = eng.switch_choices(found, cfg)
    check(cfg == saved and len(choices) == 2 and [c[2] for c in choices] == [True, False],
          f"the model menu: A is the current one ({[(c[0], c[2]) for c in choices]})")
    info_a, info_b = choices[0][1], choices[1][1]
    check(eng.same_launch(cfg, info_a), "the current model's entry is the launch the config has")
    check(tuple(info_b.args) == ("-m", B, "--port", "9931", "--alias", "Beta-27B-Q3_K_XL")
          and isinstance(info_b, LlamaLaunch) and not eng.same_launch(cfg, info_b),
          f"B's entry: the context of A does not follow ({info_b.args})")
    eng.apply_launcher(cfg, info_b)
    check(cfg["launch_cmd"] == [PROG, *info_b.args] and cfg[LM.KEY][KA] == {"args": ["-c", "65536"]}
          and cfg["port"] == 9931, f"switching saves A's settings and makes B live ({cfg.get(LM.KEY)})")
    check(eng.same_launch(cfg, info_b), "and then it is the configured launch")
    # the shared port changes while B is live; A gets it on the way back
    b8080 = LlamaLaunch(PROG, LF.with_port(info_b.args, 8080))
    eng.apply_launcher(cfg, b8080)
    back = dict((lab, inf) for lab, inf, _cur in eng.switch_choices(found, cfg))
    info_a2 = next(inf for inf in back.values() if LM.split(inf.args).model == A)
    check(tuple(info_a2.args) == ("-m", A, "--port", "8080", "-c", "65536", "--alias", "Alpha-27B-IQ4_XS"),
          f"back to A: its context, the shared port ({info_a2.args})")
    eng.apply_launcher(cfg, info_a2)
    check(cfg["port"] == 8080 and cfg["endpoint_url"] == "http://127.0.0.1:8080/v1"
          and cfg[LM.KEY][KB]["args"] == ["--alias", "Beta-27B-Q3_K_XL"], "port and endpoint follow the launch")
    # a launch that carries another model's changed settings is not 'the same launch'
    other = LlamaLaunch(PROG, tuple(cfg["launch_cmd"][1:]),
                        models=((KB, json.dumps({"args": ["-c", "4096"], "alias": True})),))
    check(not eng.same_launch(cfg, other), "only another model's settings changed: still something to save")
    eng.apply_launcher(cfg, other)
    check(cfg[LM.KEY][KB]["args"] == ["-c", "4096"] and eng.same_launch(cfg, other), "saved with the launch")
    # Settings: one session per window
    opts = eng.launch_options(found, cfg)
    assert opts is not None
    check(opts.get("per_model") is True and callable(opts.get("launch_info")), "options: the per-model hooks")
    cur = tuple(cfg["launch_cmd"][1:])
    to_b = tuple(opts["with_model"](LF.with_context(cur, 16384), B))
    check(LF.context_of(to_b) == 4096 and LM.split(to_b).shared == ("--port", "8080"), f"Settings: pick B ({to_b})")
    wrapped = opts["launch_info"](CommandLaunch(PROG, to_b))
    check(isinstance(wrapped, LlamaLaunch) and KA in dict(wrapped.models)
          and json.loads(dict(wrapped.models)[KA])["args"][:2] == ["-c", "16384"],
          "the launch Settings saves carries A's unsaved edit")
    trial = dict(cfg)
    eng.apply_launcher(trial, wrapped)
    check(LF.context_of(trial[LM.KEY][KA]["args"]) == 16384 and LF.context_of(cfg[LM.KEY][KA]["args"]) == 65536,
          "saving writes it; the check Settings runs on a trial copy does not")
    fresh = engines.get_engine({"backend": "llama_cpp"}).launch_options(found, {})
    assert fresh is not None
    first = tuple(fresh["default_args"](A))
    check(first == ("-m", A, "--port", "9931", "--alias", "Alpha-27B-IQ4_XS"), f"first set-up arguments ({first})")
    # a config that still holds Strata's command
    scfg = {"backend": "llama_cpp", "launch_cmd": [r"C:\S\.venv\Scripts\python.exe", r"C:\S\serve\server.py", "--port",
                                                   "8080"], "port": 8080}
    eng.apply_launcher(scfg, LlamaLaunch(PROG, ("-m", A, "--port", "9931")))
    check(set(scfg[LM.KEY]) == {KA}, "Strata's command is not remembered as a model")
    # the table stays where it is when the engine changes, and survives the config file
    parked = engine_profiles.switch(cfg, "llama_cpp", "strata", {"launch_cmd": [], "port": 8080})
    check(parked.get(LM.KEY) == cfg[LM.KEY], "switching engines keeps the per-model table")
    path = Path(os.environ["AGENT_START_HOME"]) / "roundtrip.json"
    config_store.save_config(cfg, path)
    check(config_store.load_config(path)[LM.KEY] == cfg[LM.KEY], "config.json round trip")
    # the fit pre-check never sees the alias (llama-fit-params refuses it)
    from engines import llama_cpp
    real = dict(cfg, launch_cmd=[PROG, "-m", str(ROOT / "pyproject.toml"), "--port", "9931", "--alias", "x", "-c", "8"])
    fa = llama_cpp.fit_args(real)
    check(fa is not None and "--alias" not in fa and "x" not in fa, f"fit arguments carry no alias ({fa})")


def main() -> int:
    for t in (t_split, t_keys_names, t_session, t_apply, t_fit_context, t_engine):
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"{t.__name__} raised {exc!r}")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
