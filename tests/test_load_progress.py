"""Unit test: Strata log -> phase/progress. Run: .venv\\Scripts\\python.exe tests\\test_load_progress.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import load_progress as lp  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "logs"
LINES = [
    "the vision encoder starts with the first picture (elastic) ...",
    "loading the model (the first start takes a minute or two) ...",
    "[strata] starting the engine: reading the model's weights ...",
    "[strata] loading the experts into RAM (about 45 GB) and locking part of them for the GPU.",
    "[strata] still starting (27 s) - please wait ...",
    "[strata] filling the GPU's expert cache (1095 experts, 2.11 GiB of VRAM) ...",
    "[strata] almost ready ...",
    "ready: http://127.0.0.1:8080/v1  (OpenAI: /v1/chat/completions ...)",
]
EXPECT = ["Launching", "Starting engine", "Reading weights", "Loading experts into RAM", "Loading experts into RAM",
          "Filling GPU cache", "Almost ready", "Ready"]
fails = 0
last = -1.0
for i, want in enumerate(EXPECT):
    text = "\n".join(LINES[: i + 1])
    label, frac, detail = lp.parse(text, elapsed_s=i * 8, expected_s=60)
    ok = label == want and frac >= last and 0 <= frac <= 1
    fails += not ok
    last = frac
    print(f"[{'PASS' if ok else 'FAIL'}] {want:26s} -> {label!r} {frac:.2f} {detail or ''}")
label, frac, detail = lp.parse("\n".join(LINES[:4]), 0, 60)
ok = detail == "about 45 GB" and frac == 0.25
fails += not ok
print(f"[{'PASS' if ok else 'FAIL'}] experts detail + phase floor ({detail}, {frac})")
label, frac, _ = lp.parse("\n".join(LINES[:4]), 600, 60)
ok = frac == 0.78
fails += not ok
print(f"[{'PASS' if ok else 'FAIL'}] long phase capped at next floor ({frac})")
ok = lp.fmt_elapsed(63) == "1:03" and lp.read_tail(None) == "" and lp.read_tail("C:/nope/x.log") == ""
fails += not ok
print(f"[{'PASS' if ok else 'FAIL'}] fmt_elapsed / read_tail edge cases")
real = sorted(REAL.glob("strata-*.log"))
if real:
    label, frac, _ = lp.parse(lp.read_tail(str(real[-1])), 61, 60)
    ok = label == "Ready" and frac == 1.0
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] real log {real[-1].name}: {label} {frac}")
print("RESULT:", "all passed" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
