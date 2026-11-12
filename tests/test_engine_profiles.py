"""engine_profiles: per-engine launch settings parked in config.json. Pure; no files, no processes."""
from __future__ import annotations

import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engine_profiles as P  # noqa: E402
import engines  # noqa: E402

PASSED = FAILED = 0


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print(f"[FAIL] {msg}")


DEFAULTS = {"backend": "strata", "model_name": "swift-1.5-iq3_s", "strata_dir": "", "launcher_bat": "",
            "launch_cmd": [], "endpoint_url": "http://127.0.0.1:8080/v1", "port": 8080, "user_whitelist": []}
PY = r"C:\AI\Strata\.venv\Scripts\python.exe"
STRATA = dict(DEFAULTS, strata_dir=r"C:\AI\Strata", launcher_bat=r"C:\AI\Strata\run-a.bat",
              launch_cmd=[PY, r"C:\AI\Strata\serve\server.py", "--engine", "strata", "--port", "8080"],
              launch_cwd=r"C:\AI\Strata", model_name="model-a", user_whitelist=["x.exe"], game_dirs=["steamapps"])
LL = [r"C:\AI\llama\llama-server.exe", "-m", r"C:\models\m.gguf", "--port", "9931"]


def t_switch() -> None:
    before = copy.deepcopy(STRATA)
    out = P.switch(STRATA, "strata", "llama_cpp", DEFAULTS)
    check(STRATA == before, "the input is not changed")
    check(out["backend"] == "llama_cpp" and out["launch_cmd"] == [] and out["model_name"] == ""
          and out["strata_dir"] == "" and out["launcher_bat"] == "" and "launch_cwd" not in out
          and out["port"] == 8080, f"first switch: the new engine starts unconfigured ({out})")
    check(out["user_whitelist"] == ["x.exe"] and out["game_dirs"] == ["steamapps"], "other settings are untouched")
    parked = out[P.PROFILES_KEY]["strata"]
    check(parked == {k: STRATA[k] for k in P.PROFILE_KEYS if k in STRATA}, "the old engine's settings are parked whole")
    check("llama_cpp" not in out[P.PROFILES_KEY], "the engine in use has no parked copy")
    # configure llama, switch back, and forth
    out.update(launch_cmd=LL, launch_cwd=r"C:\AI\llama", port=9931, endpoint_url="http://127.0.0.1:9931/v1",
               api_key="k")
    back = P.switch(out, "llama_cpp", "strata", DEFAULTS)
    check(all(back.get(k) == STRATA.get(k) for k in P.PROFILE_KEYS if k != "api_key") and "api_key" not in back
          and back["backend"] == "strata", f"back: Strata's settings are restored exactly ({back})")
    check(back[P.PROFILES_KEY] == {"llama_cpp": {"launch_cmd": LL, "launch_cwd": r"C:\AI\llama", "port": 9931,
                                                 "endpoint_url": "http://127.0.0.1:9931/v1", "api_key": "k",
                                                 "model_name": "", "strata_dir": "", "launcher_bat": ""}},
          f"llama.cpp's settings are parked, the key with them ({back[P.PROFILES_KEY]})")
    again = P.switch(back, "strata", "llama_cpp", DEFAULTS)
    check(again["launch_cmd"] == LL and again["port"] == 9931 and again["api_key"] == "k"
          and again[P.PROFILES_KEY].keys() == {"strata"}, "and forth: llama.cpp's settings are live again")
    same = P.switch(STRATA, "strata", "strata", DEFAULTS)
    check(same == dict(STRATA, backend="strata"), "the same engine: nothing moves")
    # a parked profile is a copy
    out2 = P.switch(STRATA, "strata", "llama_cpp", DEFAULTS)
    out2[P.PROFILES_KEY]["strata"]["launch_cmd"].append("x")
    check("x" not in STRATA["launch_cmd"], "parked settings are copies")


def t_garbage() -> None:
    for bad in ("x", 5, None, ["a"], {"strata": "x"}, {"strata": None, 3: {"port": 1}}):
        cfg = dict(STRATA, **{P.PROFILES_KEY: bad})
        out = P.switch(cfg, "strata", "llama_cpp", DEFAULTS)
        check(out["backend"] == "llama_cpp" and isinstance(out.get(P.PROFILES_KEY), dict)
              and out[P.PROFILES_KEY]["strata"]["launch_cmd"] == STRATA["launch_cmd"],
              f"an unreadable profile table ({bad!r}) is replaced, the live settings are still parked")
    out = P.switch({"backend": "strata"}, "strata", "llama_cpp", None)
    check(out["backend"] == "llama_cpp" and out["launch_cmd"] == [], "a minimal config and no defaults")
    out = P.switch(dict(STRATA, **{P.PROFILES_KEY: {"llama_cpp": {"launch_cmd": LL, "evil": 1, "user_whitelist": []}}}),
                   "strata", "llama_cpp", DEFAULTS)
    check("evil" not in out and out["user_whitelist"] == ["x.exe"] and out["launch_cmd"] == LL,
          "only launch settings are taken from a parked profile")
    check(P.has_profile(out, "strata") and not P.has_profile(out, "llama_cpp") and not P.has_profile({}, "strata"),
          "has_profile")


def t_park_foreign() -> None:
    strata, llama = engines.get_engine({"backend": "strata"}), engines.get_engine({"backend": "llama_cpp"})
    both = [strata, llama]
    # 1.11.0 left Strata's command live under backend=llama_cpp
    cfg = dict(STRATA, backend="llama_cpp")
    out = P.park_foreign(cfg, llama, both, DEFAULTS)
    check(out is not None and out["backend"] == "llama_cpp" and out["launch_cmd"] == []
          and out[P.PROFILES_KEY]["strata"]["launch_cmd"] == STRATA["launch_cmd"],
          "Strata's command under backend=llama_cpp is parked under Strata")
    back = P.switch(out or {}, "llama_cpp", "strata", DEFAULTS)
    check(strata.is_configured(back) and back["launcher_bat"] == STRATA["launcher_bat"], "...and comes back on switching")
    # the reverse
    cfg = dict(DEFAULTS, backend="strata", launch_cmd=LL, launch_cwd=r"C:\AI\llama", port=9931)
    out = P.park_foreign(cfg, strata, both, DEFAULTS)
    check(out is not None and out[P.PROFILES_KEY]["llama_cpp"]["launch_cmd"] == LL and out["port"] == 8080,
          "a llama.cpp command under backend=strata is parked under llama.cpp")
    check(P.park_foreign(STRATA, strata, both, DEFAULTS) is None, "a configured engine: nothing to do")
    check(P.park_foreign(dict(DEFAULTS), strata, both, DEFAULTS) is None, "nothing configured at all: nothing to do")
    kept = dict(STRATA, backend="llama_cpp", **{P.PROFILES_KEY: {"strata": {"launch_cmd": ["keep"]}}})
    check(P.park_foreign(kept, llama, both, DEFAULTS) is None, "an existing parked profile is never overwritten")

    class Boom:
        id = "boom"

        def is_configured(self, cfg):
            raise RuntimeError("x")
    check(P.park_foreign(STRATA, Boom(), both, DEFAULTS) is None, "never raises")


def main() -> int:
    for t in (t_switch, t_garbage, t_park_foreign):
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
