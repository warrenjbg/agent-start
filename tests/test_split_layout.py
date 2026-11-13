"""The split of main.py stays sound: main.py (on/off logic and wiring), main_view.py and main_tray.py (mixins of
the app class), app_workers.py, metrics_worker.py, alias_controller.py, text_util.py, app_consts.py.

What this pins, without a window, a process or the network:
  * every name main.py re-exports from those modules is the very object of its home module;
  * no test assigns such a name on `main`: the moved code reads its own module, so the write would change nothing
    and nothing would say so (patch the home module instead);
  * none of those modules imports main (run as a script, main.py is `__main__`: `import main` would load a second
    copy of the app), and app_consts.py imports nothing at all;
  * the app class takes its drawing and tray methods from the mixins, each method defined in exactly one place;
  * the frozen build lists every one of the modules.

    .venv\\Scripts\\python.exe tests\\test_split_layout.py
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ["AGENT_START_HOME"] = tempfile.mkdtemp(prefix="agentstart-split-test-")   # before any app module
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402

SPLIT = ("main_view", "main_tray", "app_workers", "metrics_worker", "alias_controller", "text_util", "app_consts")
PASSED = FAILED = 0


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {msg}")
    else:
        FAILED += 1
        print(f"  [FAIL] {msg}")


def _tree(name: str) -> ast.Module:
    return ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))


def reexports() -> dict[str, tuple[str, str]]:
    """Name on main -> (home module, name there), from main.py's own `from MODULE import ...` lines."""
    out: dict[str, tuple[str, str]] = {}
    for node in _tree("main").body:
        if isinstance(node, ast.ImportFrom) and node.module in SPLIT and node.level == 0:
            for a in node.names:
                out[a.asname or a.name] = (node.module, a.name)
    return out


def test_reexports_are_the_same_objects() -> None:
    names = reexports()
    homes = {m for m, _n in names.values()}
    check(len(names) >= 60 and homes == set(SPLIT), f"main.py imports from every split module ({len(names)} names from {sorted(homes)})")
    wrong = [n for n, (mod, home) in names.items()
             if getattr(main, n, object()) is not getattr(importlib.import_module(mod), home, None)]
    check(not wrong, f"every re-exported name on main is its home module's own object ({wrong})")
    for old in ("ALIAS_LABEL", "LIVE_POLL_S", "FIT_PRECHECK_WAIT_S", "STATS_MS", "ACTIVE", "APP_TITLE", "_FitProbe",
                "_fit_check_worker", "format_preview", "_short", "_gb", "STATUS", "PANELS", "HEIGHT"):
        check(hasattr(main, old), f"main.{old} is still there for code that reads it")


def test_no_test_assigns_a_moved_name_on_main() -> None:
    names = reexports()
    hits: list[str] = []
    scanned = 0
    for path in sorted((ROOT / "tests").glob("*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
                   if a.name == "main"}
        if not aliases:
            continue
        scanned += 1
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = [x for t in node.targets for x in ast.walk(t)]
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id in aliases \
                        and t.attr in names:
                    hits.append(f"{path.name}:{node.lineno} {t.value.id}.{t.attr} = ... (set {names[t.attr][0]}.{names[t.attr][1]})")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr" \
                    and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases \
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value in names:
                hits.append(f"{path.name}:{node.lineno} setattr({node.args[0].id}, {node.args[1].value!r}, ...)")
    check(scanned >= 10, f"the test files that import main were read ({scanned})")
    check(not hits, f"no test assigns a moved name on main; the code would not see it ({hits})")
    # the check itself notices such a write
    probe = ast.parse("import main as M\nM.LIVE_POLL_S = 0.2\n")
    seen = [t.attr for n in ast.walk(probe) if isinstance(n, ast.Assign) for t in n.targets
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "M" and t.attr in names]
    check(seen == ["LIVE_POLL_S"], "a write like `M.LIVE_POLL_S = 0.2` would be reported")


def test_no_split_module_imports_main() -> None:
    for name in SPLIT:
        mods: set[str] = set()
        for node in ast.walk(_tree(name)):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        check("main" not in mods, f"{name}.py does not import main")
        if name == "app_consts":
            check(not mods, f"app_consts.py imports nothing ({sorted(mods)})")
    text = "".join((ROOT / f"{n}.py").read_text(encoding="utf-8") for n in SPLIT)
    check(not re.search(r"import_module\(\s*['\"]main['\"]|__import__\(\s*['\"]main['\"]", text),
          "...and none loads it by name either")


def test_app_class_and_mixins() -> None:
    import main_tray
    import main_view
    app, view, tray = main.AgentStartApp, main_view.AppView, main_tray.AppTray
    mro = app.__mro__
    check(mro[0] is app and mro[1] is view and mro[2] is tray, f"the app class is built on AppView, AppTray, then the window ({[c.__name__ for c in mro[:4]]})")
    check("__init__" not in vars(view) and "__init__" not in vars(tray) and view.__bases__ == (object,)
          and tray.__bases__ == (object,), "the mixins have no base class and no __init__")

    def own(cls) -> set[str]:
        return {k for k, v in vars(cls).items() if callable(v) or isinstance(v, (staticmethod, classmethod, property))}

    twice = sorted((own(app) & own(view)) | (own(app) & own(tray)) | (own(view) & own(tray)))
    check(not twice, f"no method is defined in two of the three classes ({twice})")
    check({"_build_ui", "_apply_state", "_refresh_stats", "_layout"} <= own(view), "the drawing methods are AppView's")
    check({"_start_tray", "_sync_tray", "_tray_toggle"} <= own(tray), "the tray methods are AppTray's")
    check({"turn_on", "turn_off", "on_status", "_set_state", "_apply_metrics", "destroy"} <= own(app),
          "starting, stopping and the status path stay in main.py")
    check(len(own(app)) <= 125, f"the app class has not grown back ({len(own(app))} methods; it was 168 before the split)")
    lines = len((ROOT / "main.py").read_text(encoding="utf-8").splitlines())
    check(lines <= 2700, f"main.py has not grown back ({lines} lines; it was 3,812 before the split)")


def test_frozen_build_lists_the_modules() -> None:
    spec = (ROOT / "AgentStart.spec").read_text(encoding="utf-8")
    missing = [n for n in SPLIT if f'"{n}"' not in spec]
    check(not missing, f"AgentStart.spec hiddenimports names every split module ({missing})")


def main_() -> int:
    for fn in (test_reexports_are_the_same_objects, test_no_test_assigns_a_moved_name_on_main,
               test_no_split_module_imports_main, test_app_class_and_mixins, test_frozen_build_lists_the_modules):
        print(f"-- {fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"{fn.__name__} raised {exc!r}")
    print(f"\nRESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main_())
