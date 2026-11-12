"""strata_tuning + the Strata engine's context / vision controls. Fakes only: made-up Strata folders in a temp dir;
nothing is started, no port is contacted. Headless (CI)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="agentstart-tuning-home-"))
os.environ["AGENT_START_HOME"] = str(HOME)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_store  # noqa: E402
import engines  # noqa: E402
import strata_tuning as st  # noqa: E402
from engines.base import LaunchError, LaunchPlan  # noqa: E402
from engines.strata import TunedLaunch  # noqa: E402

PASSED = FAILED = 0
TMP = Path(tempfile.mkdtemp(prefix="agentstart-tuning-"))
ENG = engines.get_engine({"backend": "strata"})


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print(f"[FAIL] {msg}")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


ARGS = ["--pack", "C:\\packs\\m", "--max-context", "262144", "--kv", "int8", "--vision", "--vram-reserve-mib", "700"]


def make(name: str, port: int, args=None, vision=True) -> tuple[Path, Path]:
    sdir = TMP / name
    (sdir / "serve").mkdir(parents=True)
    (sdir / ".venv" / "Scripts").mkdir(parents=True)
    (sdir / "serve" / "server.py").write_text("", encoding="utf-8")
    (sdir / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    run_cfg = {"exe": "x", "args": list(ARGS if args is None else args), "model_name": "model-a", "port": port,
               "log": "C:\\logs\\engine.log", "args_written": list(ARGS)}
    if vision:
        run_cfg["vision"] = {"exe": "v.exe", "mmproj": "p.gguf"}
    cfgp = sdir / "strata-a.json"
    cfgp.write_text(json.dumps(run_cfg), encoding="utf-8")
    bat = sdir / "run-a.bat"
    bat.write_text(f'@echo off\r\ncd /d "{sdir}"\r\n"{sdir}\\.venv\\Scripts\\python.exe" "{sdir}\\serve\\server.py" '
                   f'"--engine" "strata" "--config" "{cfgp}" "--port" "{port}" "--open"\r\n', encoding="utf-8",
                   newline="")
    return bat, cfgp


def t_module() -> None:
    run = {"args": list(ARGS), "vision": {"a": 1}, "args_written": list(ARGS), "model_name": "m"}
    t = st.tuning_of(run)
    check(t == st.Tuning(262144, True, True), f"tuning_of reads context and vision ({t})")
    check(st.tuning_of({"args": ["--max-context", "8192"]}) == st.Tuning(8192, False, False)
          and st.tuning_of({"args": ["--vision"]}) == st.Tuning(None, False, False)
          and st.tuning_of({"args": ["--max-context", "x"], "vision": {}}) == st.Tuning(None, False, False),
          "vision needs the flag AND the block; a bad context is None")
    check(st.tuning_of(None) is None and st.tuning_of({}) is None and st.tuning_of({"args": "x"}) is None
          and st.tuning_of([]) is None, "tuning_of: garbage is None")  # type: ignore[arg-type]
    before = json.dumps(run, sort_keys=True)
    new = st.tuned(run, 131072, None)
    check(new["args"] == ["--pack", "C:\\packs\\m", "--max-context", "131072", "--kv", "int8", "--vision",
                          "--vram-reserve-mib", "700"] and "vision" in new and "args_written" not in new
          and new["model_name"] == "m", f"tuned: only the context changes ({new['args']})")
    check(json.dumps(run, sort_keys=True) == before, "tuned never changes its input")
    off = st.tuned(run, None, False)
    check("--vision" not in off["args"] and "vision" not in off and off["args"].count("--max-context") == 1
          and off["args"][off["args"].index("--max-context") + 1] == "262144", "tuned: vision off removes flag and block")
    check(st.tuned(run, None, True) == st.tuned(run, None, None) and "--vision" in st.tuned(run, None, True)["args"],
          "tuned: True / None leave vision as in the file")
    plain = {"args": ["--pack", "p"]}
    check(st.tuned(plain, 65536, True)["args"] == ["--pack", "p", "--max-context", "65536"]
          and "--vision" not in st.tuned(plain, None, True)["args"], "tuned: context appended; vision never switched ON")
    check(st.tuned({"args": ["--max-context", "1", "-x", "--max-context", "2"]}, 9, None)["args"]
          == ["--max-context", "9", "-x"], "tuned: duplicate context options collapse")
    # the stored choice
    cfg: dict = {"port": 8080}
    st.store(cfg, "C:\\S\\strata-a.json", t, 131072, True)
    check(st.stored(cfg, "c:/s/strata-a.json".replace("/", "\\")) == {"context": 131072}
          and st.stored(cfg, "C:\\S\\other.json") == {}, f"store / stored: keyed by the run config ({cfg})")
    check(st.effective(cfg, "C:\\S\\strata-a.json", t) == (131072, True), "effective: the choice over the file")
    st.store(cfg, "C:\\S\\strata-a.json", t, 131072, False)
    check(st.stored(cfg, "C:\\S\\strata-a.json") == {"context": 131072, "vision": False}
          and st.effective(cfg, "C:\\S\\strata-a.json", t) == (131072, False), "vision off is stored")
    st.store(cfg, "C:\\S\\strata-a.json", t, 262144, True)
    check(st.TUNING_KEY not in cfg and cfg == {"port": 8080}, "the file's own values store nothing (entry removed)")
    st.store(cfg, "C:\\S\\strata-a.json", st.Tuning(8192, False, False), 8192, False)
    check(st.TUNING_KEY not in cfg, "vision off for a model without vision stores nothing")
    for bad in ({st.TUNING_KEY: "x"}, {st.TUNING_KEY: {"k": 5}}, {st.TUNING_KEY: {st._key("C:\\S\\a.json"): {
            "context": "big", "vision": "no"}}}, {st.TUNING_KEY: {st._key("C:\\S\\a.json"): {"context": True}}}, {}):
        check(st.stored(bad, "C:\\S\\a.json") == {}, f"stored: garbage is empty ({bad})")
    check(st.stored({}, None) == {} and st.effective({}, None, None) == (None, False), "no run config: nothing")
    check(st.context_label(131072) == "128K tokens" and st.context_label(48000) == "48,000 tokens"
          and st.context_label(None) == "As in the launcher", "labels")


def t_write_copy() -> None:
    bat, cfgp = make("Strata-W", 18101)
    h = sha(cfgp)
    out = TMP / "data" / "strata"
    p = Path(st.write_copy(str(cfgp), str(out), {"context": 65536, "vision": False}))
    made = json.loads(p.read_text(encoding="utf-8"))
    check(p.parent == out and p.name == "strata-a.agentstart.json" and sha(cfgp) == h,
          f"the copy goes to the given folder; the original is untouched ({p.name})")
    check(made["args"][made["args"].index("--max-context") + 1] == "65536" and "--vision" not in made["args"]
          and "vision" not in made and made["model_name"] == "model-a" and made["log"] == "C:\\logs\\engine.log"
          and "args_written" not in made, "the copy: only context and vision differ (log, model name kept)")
    check([f.name for f in out.iterdir()] == ["strata-a.agentstart.json"], "no temp file left behind")
    p2 = Path(st.write_copy(str(cfgp), str(out), {"context": 32768}))
    check(p2 == p and json.loads(p.read_text(encoding="utf-8"))["vision"] == {"exe": "v.exe", "mmproj": "p.gguf"},
          "a later start overwrites the copy from the ORIGINAL (vision back)")
    for bad in (TMP / "nope.json", bat):
        try:
            st.write_copy(str(bad), str(out), {"context": 1})
            check(False, f"unreadable original must raise ({bad.name})")
        except (OSError, ValueError):
            check(True, "unreadable original raises")


def t_engine() -> None:
    bat, cfgp = make("Strata-E", 18102)
    h = sha(cfgp)
    info = ENG.read_launcher(str(bat))
    cfg: dict = dict(config_store.DEFAULTS)
    ENG.apply_launcher(cfg, info)
    check(st.TUNING_KEY not in cfg and ENG.same_launch(cfg, info), "a plain launcher stores no choice")
    c = ENG.launcher_controls(info, cfg)
    check(c is not None and c["context"] == 262144 and c["vision"] is True and c["vision_possible"] is True
          and [n for _l, n in c["context_choices"]] == [32768, 65536, 131072, 262144],
          f"controls show the file's values ({c})")
    tuned = ENG.with_controls(info, 131072, True)
    check(isinstance(tuned, TunedLaunch) and tuned.path == info.path and tuned.model_name == info.model_name
          and tuned.file_name == info.file_name and tuned.summary() == info.summary(),
          "the tuned launcher still looks like the launcher")
    check(not ENG.same_launch(cfg, tuned), "a new choice is a change (Save / applies next start)")
    ENG.apply_launcher(cfg, tuned)
    check(st.stored(cfg, str(cfgp)) == {"context": 131072} and ENG.same_launch(cfg, tuned)
          and cfg["launch_cmd"][cfg["launch_cmd"].index("--config") + 1] == str(cfgp),
          "saved: the choice is in config.json; launch_cmd still names Strata's own file")
    check(ENG.same_launch(cfg, info) and st.stored(cfg, str(cfgp)) == {"context": 131072},
          "the model menu (plain launcher) keeps the saved choice")
    ENG.apply_launcher(cfg, info)
    check(st.stored(cfg, str(cfgp)) == {"context": 131072}, "re-applying the plain launcher does not drop it")
    c = ENG.launcher_controls(info, cfg)
    check(c is not None and c["context"] == 131072 and c["vision"] is True, "controls show the saved choice")
    # before_launch: the copy, and only --config changes
    plan = ENG.plan_launch(cfg)
    new = ENG.before_launch(cfg, plan)
    copy_file = HOME / "strata" / "strata-a.agentstart.json"
    i = plan.cmd.index("--config")
    check(new.cmd[i + 1] == str(copy_file) and new.cmd[:i + 1] == plan.cmd[:i + 1] and new.cmd[i + 2:] == plan.cmd[i + 2:]
          and new.cwd == plan.cwd and new.env == plan.env and plan.cmd[i + 1] == str(cfgp),
          f"before_launch swaps only the --config value ({new.cmd[i + 1]})")
    made = json.loads(copy_file.read_text(encoding="utf-8"))
    check(made["args"][made["args"].index("--max-context") + 1] == "131072" and "--vision" in made["args"]
          and sha(cfgp) == h, "the copy has the chosen context; Strata's file is untouched")
    check(ENG.config_path(cfg) == str(cfgp), "the engine log is still found through Strata's own file")
    # vision off, then back to the file's values
    ENG.apply_launcher(cfg, ENG.with_controls(info, 131072, False))
    new = ENG.before_launch(cfg, ENG.plan_launch(cfg))
    made = json.loads(copy_file.read_text(encoding="utf-8"))
    check("--vision" not in made["args"] and "vision" not in made and ENG.launcher_controls(info, cfg)["vision"] is False,
          "vision off reaches the copy and the controls")
    ENG.apply_launcher(cfg, ENG.with_controls(info, 262144, True))
    plan = ENG.plan_launch(cfg)
    check(st.TUNING_KEY not in cfg and ENG.before_launch(cfg, plan) is plan, "the file's own values: no copy, the plan as it is")
    # failures
    ENG.apply_launcher(cfg, ENG.with_controls(info, 65536, True))
    cfgp.write_text("not json", encoding="utf-8")
    try:
        ENG.before_launch(cfg, ENG.plan_launch(cfg))
        check(False, "an unreadable run config must refuse the start")
    except LaunchError as exc:
        check("context / vision" in str(exc), f"unreadable run config: the start is refused with a reason ({exc})")
    check(ENG.launcher_controls(info, cfg) is None, "unreadable run config: no controls")
    # a model without vision / without a context option
    bat2, cfgp2 = make("Strata-N", 18103, args=["--pack", "p", "--max-context", "8192"], vision=False)
    info2 = ENG.read_launcher(str(bat2))
    c2 = ENG.launcher_controls(info2, {})
    check(c2 is not None and c2["vision_possible"] is False and c2["vision"] is False and c2["context"] == 8192
          and 8192 in [n for _l, n in c2["context_choices"]], f"no vision in the file: not offered; odd context listed ({c2})")
    bat3, cfgp3 = make("Strata-Z", 18104, args=["--pack", "p"], vision=False)
    check(ENG.launcher_controls(ENG.read_launcher(str(bat3)), {}) is None, "nothing to tune: no controls")
    # other engines / the default
    ll = engines.get_engine({"backend": "llama_cpp"})
    p = LaunchPlan(cmd=["x.exe"], cwd=".")
    check(ll.before_launch({}, p) is p and ll.launcher_controls(object(), {}) is None and ll.with_controls(5, 1, True) == 5,
          "llama.cpp: the defaults (nothing changes)")


def main() -> int:
    for t in (t_module, t_write_copy, t_engine):
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"{t.__name__} raised {exc!r}")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
