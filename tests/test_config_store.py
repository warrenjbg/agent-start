"""config_store: the one-time move of the pre-v1.5 data folder ("Hermes Controller") to the new name."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AGENT_START_HOME"] = tempfile.mkdtemp(prefix="as_cfg_")   # never the real %APPDATA%

import config_store as cs  # noqa: E402

PASSES, FAILS = 0, []


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"[PASS] {msg}")
    else:
        FAILS.append(msg)
        print(f"[FAIL] {msg}")


def main() -> int:
    check(cs.LEGACY_DATA_DIR is None, "no legacy migration when AGENT_START_HOME is set (tests, from source)")
    tmp = Path(tempfile.mkdtemp(prefix="as_mig_"))
    old, new = tmp / "HermesController", tmp / "AgentStart"
    (old / "logs").mkdir(parents=True)
    (old / "config.json").write_text(json.dumps({"strata_dir": "X"}), encoding="utf-8")
    (old / "state.json").write_text(json.dumps({"pinned": ["vram"], "reopen_pending": {"records": [1]}}),
                                    encoding="utf-8")
    (old / "logs" / "controller.log").write_text("old log\n", encoding="utf-8")

    check(cs.migrate_legacy_data(old, new) is True, "old data copied to the new folder")
    check(json.loads((new / "config.json").read_text(encoding="utf-8")) == {"strata_dir": "X"}, "settings kept")
    st = json.loads((new / "state.json").read_text(encoding="utf-8"))
    check(st.get("pinned") == ["vram"] and st.get("reopen_pending"), "pins + pending reopen kept")
    check((new / "logs" / "controller.log").is_file(), "logs kept")
    check((old / "config.json").is_file(), "old folder left in place (nothing deleted)")

    (new / "config.json").write_text(json.dumps({"strata_dir": "NEW"}), encoding="utf-8")
    check(cs.migrate_legacy_data(old, new) is False
          and json.loads((new / "config.json").read_text(encoding="utf-8")) == {"strata_dir": "NEW"},
          "never overwrites an existing new config")
    check(cs.migrate_legacy_data(tmp / "missing", tmp / "other") is False and not (tmp / "other").exists(),
          "no old folder -> nothing happens")
    check(cs.migrate_legacy_data(None, tmp / "x") is False, "no legacy dir -> nothing happens")

    d = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
    check(d.get("strata_dir") == "" and d.get("launch_cmd") == [] and d.get("launcher_bat") == "",
          "shipped default config has no machine paths")
    check(d.get("endpoint_url") == "http://127.0.0.1:8080/v1" and isinstance(d.get("user_whitelist"), list),
          "shipped default config is complete")
    print(f"\nRESULT: {PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
