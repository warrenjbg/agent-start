r"""Self-check for llama_finder.py. Run: .venv\Scripts\python.exe tests\test_llama_finder.py

Safe by construction: every fake llama.cpp tree lives in a fresh temp dir that is removed at exit. Nothing is executed
(the llama-server.exe / .dll files are tiny stubs; llama_finder never runs them). The only subprocess is
`cmd /c mklink /J` creating a junction INSIDE the temp dir. Model size rules use the injectable min_bytes, so no big
files are written. Every search passes a fake `env`, so the PC's real PATH / winget / caches are not used - except the
final real-PC smoke, a read-only walk whose result is printed, not asserted.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llama_finder as lf  # noqa: E402
from llama_finder import Build, Model  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="hc-llama-finder-"))
CREATE_NO_WINDOW = 0x08000000
GGUF = b"GGUF" + b"\x03\x00\x00\x00" + b"\x00" * 56
SMALL = 32            # min_bytes used by the tests: our fake models are 64 bytes, "small" files 8 bytes
JUNCTIONS: list[Path] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def make_build(folder: Path, backends=("cuda",), core: str | None = "llama.dll", zips=()) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "llama-server.exe").write_bytes(b"MZ")
    if core:
        (folder / core).write_bytes(b"MZ")
    (folder / "ggml-base.dll").write_bytes(b"MZ")
    (folder / "ggml-cpu-x64.dll").write_bytes(b"MZ")
    (folder / "ggml-rpc.dll").write_bytes(b"MZ")
    for b in backends:
        (folder / f"ggml-{b}.dll").write_bytes(b"MZ")
    for z in zips:
        (folder / z).write_bytes(b"PK")
    return folder / "llama-server.exe"


def make_model(path: Path, data: bytes = GGUF) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def env_for(base: Path, path: str = "", **extra: str) -> dict[str, str]:
    """A fake environment: nothing in it points outside `base`."""
    e = {"LOCALAPPDATA": str(base / "lad"), "USERPROFILE": str(base / "home"), "SystemRoot": str(base / "win"),
         "PATH": path}
    e.update(extra)
    return e


def k(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def progs(builds) -> set[str]:
    return {k(b.program) for b in builds}


def paths(models) -> set[str]:
    return {k(m.path) for m in models}


def mklink_j(link: Path, target: Path) -> bool:
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=20)
        ok = r.returncode == 0 and link.exists()
    except Exception:
        ok = False
    if ok:
        JUNCTIONS.append(link)
    return ok


# ------------------------------------------------------------------------------------------- tests
def test_backends() -> None:
    section("backend + build number detection")
    base = TMP / "backends"
    env = env_for(base)
    cases = {
        "cuda": make_build(base / "llama-b12314-bin-win-cuda-13.5-x64", ("cuda",)),
        "vulkan": make_build(base / "vk", ("vulkan",), core="ggml.dll"),
        "sycl": make_build(base / "sycl", ("sycl",)),
        "opencl": make_build(base / "ocl", ("opencl",)),
        "hip": make_build(base / "hip", ("hip",)),
        "cpu": make_build(base / "cpu", ()),
    }
    multi = make_build(base / "multi", ("vulkan", "cuda", "opencl"))
    zipped = make_build(base / "llama.cpp-zip", ("cuda",), zips=("llama-b9001-bin-win-cuda-13.5-x64.zip",
                                                                  "cudart-llama-bin-win-cuda-13.5-x64.zip"))
    nested = make_build(base / "llama-b777" / "bin", ())
    found = lf.find_builds([str(base)], time_budget_s=10, env=env)
    by = {k(b.program): b for b in found}
    for be, exe in cases.items():
        b = by.get(k(exe))
        check(f"{be} build found with backend '{be}'", b is not None and b.backend == be and b.backends == (be,),
              f"{b}")
    b = by.get(k(multi))
    check("several backend DLLs: all listed, cuda preferred in the label",
          b is not None and b.backends == ("cuda", "vulkan", "opencl") and b.backend == "cuda", f"{b}")
    check("ggml-cpu-* / ggml-rpc / ggml-base are not backends", by[k(cases["cpu"])].backends == ("cpu",))
    check("build number from the folder name", by[k(cases["cuda"])].build == "b12314")
    check("build number from a zip in the folder", by[k(zipped)].build == "b9001", f"{by[k(zipped)].build}")
    check("build number from the parent folder name", by[k(nested)].build == "b777", f"{by[k(nested)].build}")
    check("no build number -> None", by[k(cases["vulkan"])].build is None)
    check("source 'folder' for a walked folder", all(b.source == "folder" for b in found))
    check("Build is frozen", _frozen(found[0]))
    check("program/folder are absolute", all(os.path.isabs(b.program) and os.path.isabs(b.folder) for b in found))
    check("build-number regex ignores the winget publisher hash",
          lf._build_number("ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe") is None)


def _frozen(obj) -> bool:
    try:
        obj.program = "x"
        return False
    except Exception:
        return True


def test_lookalikes() -> None:
    section("lookalike llama-server.exe is not offered")
    base = TMP / "lookalike"
    env = env_for(base)
    bare = make_build(base / "bare", ("cuda",), core=None)        # backend dll but no llama.dll / ggml.dll
    lone = base / "lone"
    lone.mkdir(parents=True)
    (lone / "llama-server.exe").write_bytes(b"MZ")
    dirname = base / "dir-named"
    (dirname / "llama-server.exe").mkdir(parents=True)              # a folder with that name
    (dirname / "llama.dll").write_bytes(b"MZ")
    good = make_build(base / "good", ("vulkan",))
    found = lf.find_builds([str(base)], time_budget_s=10, env=env)
    got = progs(found)
    check("exe without llama.dll/ggml.dll NOT offered", k(bare) not in got)
    check("lone exe NOT offered", k(lone / "llama-server.exe") not in got)
    check("a directory named llama-server.exe NOT offered", k(dirname / "llama-server.exe") not in got)
    check("the real build IS offered", got == {k(good)}, f"{got}")


def test_winget_path_dedupe() -> None:
    section("winget root, PATH entries, dedupe")
    base = TMP / "wg"
    env0 = env_for(base)
    pkg = base / "lad" / "Microsoft" / "WinGet" / "Packages"
    wg = make_build(pkg / "ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe", ("vulkan",), core="ggml.dll")
    other = make_build(pkg / "Someone.Else_xyz", ("cuda",))          # not a llama.cpp package
    machine = make_build(base / "pf" / "WinGet" / "Packages" / "ggml.llamacpp_x", ("vulkan",))
    on_path = make_build(base / "tools" / "llama", ("cuda",))
    deep_path = make_build(base / "tools2" / "sub" / "llama", ("cuda",))   # PATH lists tools2 only: not walked
    links = base / "lad" / "Microsoft" / "WinGet" / "Links"
    links.mkdir(parents=True)
    (links / "llama-server.exe").write_bytes(b"MZ")                    # winget's Links stub: no DLLs beside it
    env = dict(env0, ProgramFiles=str(base / "pf"),
               PATH=os.pathsep.join([str(links), str(on_path.parent), '"' + str(on_path.parent) + '"',
                                     str(wg.parent), "relative\\dir", "", str(base / "tools2"),
                                     str(base / "missing")]))
    t0 = time.monotonic()
    found = lf.find_builds([], time_budget_s=10, env=env)
    dt = time.monotonic() - t0
    by = {k(b.program): b for b in found}
    check("winget package folder found via LOCALAPPDATA", k(wg) in by and by[k(wg)].source == "winget",
          f"{[(b.program, b.source) for b in found]}")
    check("machine-scope winget (ProgramFiles) found", k(machine) in by and by[k(machine)].source == "winget")
    check("non-llama winget package NOT walked", k(other) not in by)
    check("PATH folder found with source 'path'", k(on_path) in by and by[k(on_path)].source == "path")
    check("PATH folders are listed, not walked", k(deep_path) not in by)
    check("winget Links stub NOT offered", k(links / "llama-server.exe") not in by)
    check("winget folder on PATH reported once, as winget",
          sum(1 for b in found if k(b.program) == k(wg)) == 1 and by[k(wg)].source == "winget")
    check("no duplicates (quoted PATH duplicate ignored)", len(found) == len(by), f"{len(found)} vs {len(by)}")
    check("roots=[] with only env roots is fast", dt < 3, f"{dt:.2f}s")
    # the same folder reached through two roots -> one Build
    found2 = lf.find_builds([str(base / "tools"), str(base / "tools"), str(base)], time_budget_s=10,
                            env=dict(env, PATH=str(on_path.parent)))
    check("overlapping roots + PATH: one Build per real exe", len(found2) == len(progs(found2)),
          f"{[b.program for b in found2]}")
    check("winget_dirs lists only ggml.llamacpp_* folders",
          {k(p) for p in lf.winget_dirs(env)} == {k(wg.parent), k(machine.parent)})


def test_ranking() -> None:
    section("rank_builds / has_nvidia")

    def B(backend, build=None, source="folder", name="x", backends=None):
        return Build(program=f"C:\\l\\{name}\\llama-server.exe", folder=f"C:\\l\\{name}", backend=backend,
                     backends=backends or (backend,), build=build, source=source)

    cuda_old = B("cuda", "b100", name="cuda_old")
    cuda_new = B("cuda", "b200", name="cuda_new")
    vk_wg = B("vulkan", None, "winget", name="vk_wg")
    vk_folder = B("vulkan", None, "folder", name="vk_folder")
    vk_path = B("vulkan", None, "path", name="vk_path")
    cpu = B("cpu", "b999", name="cpu")
    hip = B("hip", name="hip")
    items = [cpu, vk_path, cuda_old, vk_wg, hip, cuda_new, vk_folder]
    r_nv = lf.rank_builds(items, True)
    check("NVIDIA: cuda first, newest build first", r_nv[:2] == [cuda_new, cuda_old], f"{[b.folder for b in r_nv]}")
    check("NVIDIA: vulkan next, folder > winget > path", r_nv[2:5] == [vk_folder, vk_wg, vk_path])
    check("cpu last", r_nv[-1] == cpu and lf.rank_builds(items, False)[-1] == cpu)
    r_other = lf.rank_builds(items, False)
    check("no NVIDIA: vulkan first", r_other[0] == vk_folder, f"{[b.folder for b in r_other]}")
    check("no NVIDIA: cuda after the other GPU backends, before cpu",
          [b.backend for b in r_other] == ["vulkan", "vulkan", "vulkan", "hip", "cuda", "cuda", "cpu"])
    check("pure: input list untouched", items == [cpu, vk_path, cuda_old, vk_wg, hip, cuda_new, vk_folder])
    a, b2 = B("vulkan", name="a"), B("vulkan", name="b")
    check("stable for equal keys", lf.rank_builds([b2, a], True) == [b2, a] and lf.rank_builds([a, b2], True) == [a, b2])
    mixed = B("vulkan", backends=("cuda", "vulkan"), name="mixed")
    check("a cuda+vulkan build ranks as cuda on NVIDIA", lf.rank_builds([vk_folder, mixed], True)[0] == mixed)
    check("rank_builds tolerates garbage", lf.rank_builds([None, "x", cpu], True) == [cpu]
          and lf.rank_builds(None, True) == [])  # type: ignore[arg-type]
    check("has_nvidia: nvml.dll present", lf.has_nvidia({"SystemRoot": "C:\\W"},
                                                        exists=lambda p: p.lower().endswith("nvml.dll")))
    seen: list[str] = []
    lf.has_nvidia({"SystemRoot": "C:\\W"}, exists=lambda p: seen.append(p) or False)
    check("has_nvidia looks at %SystemRoot%\\System32\\nvml.dll", seen == [os.path.join("C:\\W", "System32",
                                                                                        "nvml.dll")], f"{seen}")
    check("has_nvidia: absent -> False", not lf.has_nvidia({"SystemRoot": "C:\\W"}, exists=lambda p: False))
    check("has_nvidia: no SystemRoot -> False", not lf.has_nvidia({}, exists=lambda p: True))
    check("has_nvidia: raising exists -> False", not lf.has_nvidia({"SystemRoot": "C:\\W"}, exists=lambda p: 1 / 0))
    base = TMP / "nv"
    (base / "win" / "System32").mkdir(parents=True)
    (base / "win" / "System32" / "nvml.dll").write_bytes(b"MZ")
    make_build(base / "a-vk", ("vulkan",))
    make_build(base / "b-cu", ("cuda",))
    nv = lf.find_builds([str(base)], time_budget_s=10, env=env_for(base))
    check("find_builds ranks with has_nvidia(env): cuda first", [b.backend for b in nv] == ["cuda", "vulkan"])
    nonv = lf.find_builds([str(base)], time_budget_s=10, env=env_for(base, SystemRoot=str(base / "nowin")))
    check("find_builds without nvml.dll: vulkan first", [b.backend for b in nonv] == ["vulkan", "cuda"])


def test_choose() -> None:
    section("choose()")
    b1 = Build("C:\\a\\llama-server.exe", "C:\\a", "cuda", ("cuda",), "b5", "folder")
    b2 = Build("C:\\b\\llama-server.exe", "C:\\b", "vulkan", ("vulkan",), None, "winget")
    m1 = Model("C:\\m\\one.gguf", "one", 2 * 1024 ** 3, "C:\\m")
    m2 = Model("C:\\m\\two.gguf", "two", 3 * 1024 ** 3, "C:\\m")
    c = lf.choose([], [], True)
    check("0 builds, 0 models: nothing, not clear", c.build is None and c.model is None and not c.clear and c.why)
    c = lf.choose([], [m1], True)
    check("0 builds, 1 model: model offered, not clear", c.build is None and c.model == m1 and not c.clear)
    c = lf.choose([b2], [m1], True)
    check("1 build, 1 model: clear", c.build == b2 and c.model == m1 and c.clear, c.why)
    c = lf.choose([b2, b1], [m1], True)
    check("many builds: best ranked chosen (cuda on NVIDIA)", c.build == b1 and c.clear, c.why)
    check("many builds without NVIDIA: vulkan", lf.choose([b1, b2], [m1], False).build == b2)
    c = lf.choose([b1], [m1, m2], True)
    check("many models: no model, not clear", c.build == b1 and c.model is None and not c.clear, c.why)
    c = lf.choose([b1], [], True)
    check("build but no model: not clear", c.build == b1 and c.model is None and not c.clear)
    whys = [lf.choose(bs, ms, True).why for bs in ([], [b1], [b1, b2]) for ms in ([], [m1], [m1, m2])]
    check("why is one plain sentence each time",
          all(w and w.endswith(".") and w.count(". ") == 0 and "\n" not in w for w in whys), f"{whys}")
    c = lf.choose(None, None, True)  # type: ignore[arg-type]
    check("choose(None, None) does not raise", c.build is None and not c.clear)


def test_models() -> None:
    section("model rules")
    base = TMP / "models"
    env = env_for(base)
    d = base / "dl" / "llama-models"
    good = make_model(d / "Qwen3.8-27B-UD-Q3_K_XL.gguf", GGUF + b"\x00" * 10)
    upper = make_model(d / "Other-Model.GGUF")
    mmproj = make_model(d / "mmproj-Qwen-F16.gguf")
    vocab = make_model(d / "ggml-vocab-llama.gguf")
    small = make_model(d / "tiny.gguf", b"GGUF1234")
    wrong = make_model(d / "fake.gguf", b"NOPE" + b"\x00" * 60)
    empty = make_model(d / "empty.gguf", b"")
    p1 = make_model(d / "Big-Model-Q4-00001-of-00003.gguf", GGUF)
    p2 = make_model(d / "Big-Model-Q4-00002-of-00003.gguf", GGUF + b"\x00" * 100)
    p3 = make_model(d / "Big-Model-Q4-00003-of-00003.gguf", GGUF + b"\x00" * 100)
    lone_part = make_model(base / "dl" / "orphan" / "Orphan-00002-of-00002.gguf")
    notgguf = make_model(d / "readme.txt")
    (d / "folder.gguf").mkdir()
    found = lf.find_models([str(base / "dl")], time_budget_s=10, env=env, min_bytes=SMALL)
    got = paths(found)
    by = {k(m.path): m for m in found}
    check("a valid model is found", k(good) in got, f"{got}")
    check(".GGUF extension case-insensitive", k(upper) in got)
    check("name drops .gguf", by.get(k(good)) is not None and by[k(good)].name == "Qwen3.8-27B-UD-Q3_K_XL")
    check("size_bytes and folder", by[k(good)].size_bytes == len(GGUF) + 10 and k(by[k(good)].folder) == k(d))
    check("mmproj excluded", k(mmproj) not in got)
    check("ggml-vocab excluded", k(vocab) not in got)
    check("file under min_bytes excluded", k(small) not in got)
    check("wrong magic excluded", k(wrong) not in got)
    check("empty file excluded", k(empty) not in got)
    check("non-.gguf / folders excluded", k(notgguf) not in got and k(d / "folder.gguf") not in got)
    check("split parts 2..N excluded", k(p2) not in got and k(p3) not in got and k(lone_part) not in got)
    m = by.get(k(p1))
    check("first split part kept with a clean name", m is not None and m.name == "Big-Model-Q4", f"{m}")
    check("split model size = sum of its parts", m is not None and m.size_bytes == 3 * len(GGUF) + 200, f"{m}")
    check("sorted by name then size", [x.name.casefold() for x in found] == sorted(x.name.casefold() for x in found))
    check("exactly the 3 models", len(found) == 3, f"{[x.name for x in found]}")
    # the 100 MB default really applies
    check("default min_bytes is 100 MB", lf.MIN_MODEL_BYTES == 100 * 1024 * 1024)
    check("default threshold drops the tiny fakes",
          lf.find_models([str(base / "dl")], time_budget_s=10, env=env) == [])
    # sparse file check for the real threshold (no data written; skipped if the FS refuses)
    sp = base / "sparse" / "Huge-Model.gguf"
    sp.parent.mkdir(parents=True)
    try:
        subprocess.run(["fsutil", "sparse", "setflag", str(make_model(sp))], capture_output=True,
                       creationflags=CREATE_NO_WINDOW, timeout=20)
        with open(sp, "r+b") as f:
            f.truncate(lf.MIN_MODEL_BYTES + 1)
        sparse_ok = os.path.getsize(sp) == lf.MIN_MODEL_BYTES + 1
    except Exception:
        sparse_ok = False
    if sparse_ok:
        big = lf.find_models([str(base / "sparse")], time_budget_s=10, env=env)
        check("a > 100 MB GGUF passes the default threshold", paths(big) == {k(sp)}, f"{big}")
    else:
        print("  NOTE: could not make a sparse file; real-threshold check skipped")
    # same model twice via overlapping roots + extra_dirs -> once
    dup = lf.find_models([str(base / "dl"), str(d)], time_budget_s=10, env=env, extra_dirs=[str(d), str(d)],
                         min_bytes=SMALL)
    check("dedupe across roots/extra_dirs", len(dup) == 3 and len(paths(dup)) == 3, f"{[x.path for x in dup]}")


def test_model_places() -> None:
    section("model cache roots, extra_dirs, Program Files, skip-list")
    base = TMP / "places"
    env = env_for(base)
    lad, home = base / "lad", base / "home"
    llama_cache = make_model(lad / "llama.cpp" / "org_repo_model-Q4.gguf")
    lms = make_model(home / ".lmstudio" / "models" / "org" / "repo" / "lms-Q4.gguf")
    lms2 = make_model(home / ".cache" / "lm-studio" / "models" / "org" / "repo" / "lms2-Q4.gguf")
    ollama = make_model(home / ".ollama" / "models" / "ollama-model.gguf")
    extra = make_model(base / "elsewhere" / "deep" / "user-picked.gguf")
    pf = base / "drive"
    pf_llama = make_model(pf / "Program Files" / "llama.cpp" / "models" / "pf-model.gguf")
    pf_other = make_model(pf / "Program Files" / "Vendor" / "pf-other.gguf")
    pf_build = make_build(pf / "Program Files" / "llama.cpp", ("cuda",))
    lms_runtime = make_build(home / ".lmstudio" / "extensions" / "backends" / "llama.cpp-win-x86_64-vulkan", ("vulkan",))
    strata_model = make_model(base / "home" / "Strata-main" / "Strata-data" / "models" / "strata-IQ3_S.gguf")
    skipped = {n: make_model(pf / n / f"in-{n.replace('$', '')}.gguf")
               for n in ("Windows", "AppData", "node_modules", ".git", "site-packages", "$Recycle.Bin", "steamapps",
                         "SteamLibrary", "ProgramData")}
    found = lf.find_models([str(home), str(pf)], time_budget_s=10, env=env, extra_dirs=[str(base / "elsewhere")],
                           min_bytes=SMALL)
    got = paths(found)
    check("llama.cpp cache (%LOCALAPPDATA%\\llama.cpp) searched", k(llama_cache) in got, f"{got}")
    check("LM Studio .lmstudio\\models searched", k(lms) in got)
    check("LM Studio .cache\\lm-studio\\models searched", k(lms2) in got)
    check(".ollama skipped", k(ollama) not in got)
    check("Strata-data skipped", k(strata_model) not in got)
    check("LM Studio's private runtimes NOT offered as builds",
          k(lms_runtime) not in progs(lf.find_builds([str(home)], time_budget_s=10, env=env)))
    check("extra_dirs searched", k(extra) in got)
    check("Program Files\\llama* entered", k(pf_llama) in got)
    check("other Program Files children NOT entered", k(pf_other) not in got)
    for n, p in skipped.items():
        check(f"'{n}' not entered", k(p) not in got)
    builds = lf.find_builds([str(pf)], time_budget_s=10, env=env)
    check("build in Program Files\\llama.cpp found", progs(builds) == {k(pf_build)}, f"{progs(builds)}")
    cache = lf.model_cache_dirs({"LLAMA_CACHE": "C:\\lc", "HF_HUB_CACHE": "C:\\hf", "HF_HOME": "C:\\hh",
                                 "LOCALAPPDATA": "C:\\la", "USERPROFILE": "C:\\up"})
    check("LLAMA_CACHE / HF_HUB_CACHE / HF_HOME honoured",
          ("C:\\lc", False) in cache and ("C:\\hf", True) in cache and (os.path.join("C:\\hh", "hub"), True) in cache)


def test_hf_cache() -> None:
    section("Hugging Face cache: snapshot links resolved once")
    base = TMP / "hf"
    env = env_for(base)
    hub = base / "home" / ".cache" / "huggingface" / "hub"
    repo = hub / "models--org--Some-GGUF"
    blob = make_model(repo / "blobs" / ("a" * 64))
    mm_blob = make_model(repo / "blobs" / ("b" * 64))
    s1, s2 = repo / "snapshots" / "1111", repo / "snapshots" / "2222"
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
    try:
        os.symlink(blob, s1 / "Some-Q4_K_M.gguf")
        os.symlink(blob, s2 / "Some-Q4_K_M.gguf")
        os.symlink(mm_blob, s1 / "mmproj-Some-F16.gguf")
        links_ok = True
    except OSError:
        links_ok = False
    copy = make_model(repo / "snapshots" / "3333" / "Copied-Q8_0.gguf")      # no-symlink mode: a real copy
    found = lf.find_models([], time_budget_s=10, env=env, min_bytes=SMALL)
    got_real = {k(m.path) for m in found}
    check("a copied (non-link) snapshot file is found", k(copy) in got_real, f"{[m.path for m in found]}")
    if links_ok:
        some = [m for m in found if m.name == "Some-Q4_K_M"]
        check("linked model reported once (two snapshots, one blob)", len(some) == 1, f"{some}")
        check("its path keeps the .gguf link name", some and some[0].path.casefold().endswith(".gguf"))
        check("its size is the blob's", some and some[0].size_bytes == len(GGUF))
        check("mmproj link excluded", not any("mmproj" in m.name for m in found))
        # outside the HF cache a .gguf file link is NOT followed
        out = base / "plain"
        out.mkdir()
        os.symlink(blob, out / "Linked-Q4.gguf")
        check("file link outside the HF cache NOT followed",
              lf.find_models([str(out)], time_budget_s=10, env=env_for(base / "x"), min_bytes=SMALL) == [])
    else:
        print("  NOTE: cannot create symlinks (no Developer Mode / privilege); HF link checks skipped")


def test_junction_not_followed() -> None:
    section("junction / directory symlink not followed")
    base = TMP / "junc"
    root = base / "root"
    root.mkdir(parents=True)
    target = base / "outside"
    make_model(target / "behind-link.gguf")
    make_build(target, ("cuda",))
    make_model(root / "visible.gguf")
    j_ok = mklink_j(root / "J", target)
    s_ok = False
    try:
        os.symlink(target, root / "S", target_is_directory=True)
        s_ok = True
    except OSError:
        pass
    env = env_for(base)
    models = lf.find_models([str(root)], time_budget_s=10, env=env, min_bytes=SMALL)
    builds = lf.find_builds([str(root)], time_budget_s=10, env=env)
    check("visible model found", [m.name for m in models] == ["visible"],
          f"{[m.path for m in models]}")
    if j_ok or s_ok:
        check("nothing behind the link reported", all("behind" not in m.name for m in models) and builds == [],
              f"{[m.path for m in models]} {builds}")
    if not j_ok:
        print("  NOTE: mklink /J failed; junction check skipped")
    if not s_ok:
        print("  NOTE: cannot create a directory symlink; symlink check skipped")
    # a root that is itself a junction to a local folder may be walked (like strata_finder), but never via a link
    # found inside it: covered above.


def test_budget_and_stop() -> None:
    section("time budget, stop event, max_dirs")
    base = TMP / "wide"
    for a in range(30):
        for b in range(20):
            (base / f"a{a}" / f"b{b}" / "c").mkdir(parents=True, exist_ok=True)
    make_model(base / "a29" / "b19" / "c" / "last.gguf")
    env = env_for(base)
    evt = threading.Event()
    evt.set()
    t0 = time.monotonic()
    r = lf.find_models([str(base)], time_budget_s=30, stop_evt=evt, env=env, min_bytes=SMALL)
    dt = time.monotonic() - t0
    check("stop_evt already set: returns at once, no exception", dt < 1.0 and isinstance(r, list), f"{dt:.2f}s")
    evt2 = threading.Event()
    threading.Timer(0.05, evt2.set).start()
    t0 = time.monotonic()
    r = lf.find_all([str(base)] * 3, time_budget_s=30, stop_evt=evt2, env=env, min_bytes=SMALL)
    dt = time.monotonic() - t0
    check("stop_evt set mid-walk: returns quickly", dt < 1.5 and isinstance(r, tuple), f"{dt:.2f}s")
    t0 = time.monotonic()
    r2 = lf.find_builds([str(base)], time_budget_s=0.0, env=env)
    dt = time.monotonic() - t0
    check("time_budget_s=0: returns within the grace", dt < 1.0 and r2 == [], f"{dt:.2f}s")
    t0 = time.monotonic()
    r3 = lf.find_models([str(base)], time_budget_s=0.2, env=env, min_bytes=SMALL)
    dt = time.monotonic() - t0
    check("small budget honoured (budget + grace, 3x margin)", dt < 1.5 and isinstance(r3, list), f"{dt:.2f}s")
    full = lf.find_models([str(base)], time_budget_s=30, env=env, min_bytes=SMALL)
    check("full walk finds the last model (depth 3)", [m.name for m in full] == ["last"])
    capped = lf.find_models([str(base)], time_budget_s=30, env=env, min_bytes=SMALL, max_dirs=50)
    check("max_dirs stops the walk early", capped == [])
    check("max_depth=1 does not reach depth 3",
          lf.find_models([str(base)], time_budget_s=30, env=env, min_bytes=SMALL, max_depth=1) == [])

    class Boom:
        def is_set(self):
            raise RuntimeError("boom")
    check("a stop_evt that raises is tolerated",
          isinstance(lf.find_models([str(base)], time_budget_s=1, stop_evt=Boom(), env=env), list))  # type: ignore


def test_with_model_and_args() -> None:
    section("default_args / with_model")
    M = "D:\\models\\new model.gguf"
    check("default_args", lf.default_args("C:\\m\\a.gguf", 8090) == ("-m", "C:\\m\\a.gguf", "--port", "8090"))
    cases = [
        (["-m", "old.gguf", "--port", "8090"], ("-m", M, "--port", "8090")),
        (("--model", "old.gguf", "-c", "4096"), ("-m", M, "-c", "4096")),
        (["--model=old.gguf", "-ngl", "99"], ("-m", M, "-ngl", "99")),
        (["--port", "1", "-m", "a.gguf", "-c", "8", "--model", "b.gguf"], ("--port", "1", "-m", M, "-c", "8")),
        (["--port", "8090"], ("-m", M, "--port", "8090")),
        ([], ("-m", M)),
        (["-hf", "org/repo:Q4_K_M", "--port", "8090"], ("-m", M, "--port", "8090")),
        (["--hf-repo", "org/repo", "--hf-file", "x.gguf", "-c", "2"], ("-m", M, "-c", "2")),
        (["--hf_repo=org/repo", "-c", "2"], ("-m", M, "-c", "2")),
        (["-hf", "org/r", "-m", "a.gguf", "-c", "2"], ("-m", M, "-c", "2")),
        (["-mu", "https://x/y.gguf", "-dr", "ai/x"], ("-m", M)),
        (["-c", "2", "-m"], ("-c", "2", "-m", M)),
        (["-hfd", "org/draft", "-m", "a.gguf"], ("-hfd", "org/draft", "-m", M)),
        ([1, 2], ("-m", M, "1", "2")),
    ]
    for args, want in cases:
        got = lf.with_model(args, M)
        check(f"with_model({args!r})", got == want, f"got {got}")
    check("with_model returns a tuple, input untouched", isinstance(lf.with_model(["-m", "x"], M), tuple))
    src = ["-m", "x", "-c", "1"]
    lf.with_model(src, M)
    check("input list not modified", src == ["-m", "x", "-c", "1"])
    for bad in (None, "-m x", 42, object()):
        try:
            r = lf.with_model(bad, M)  # type: ignore[arg-type]
            check(f"with_model({type(bad).__name__}) does not raise", r == ("-m", M), f"{r}")
        except Exception as exc:
            check(f"with_model({type(bad).__name__}) does not raise", False, repr(exc))


def test_labels() -> None:
    section("labels")
    cu = Build("C:\\d\\llama.cpp-cuda-b12314\\llama-server.exe", "C:\\d\\llama.cpp-cuda-b12314", "cuda",
               ("cuda",), "b12314", "folder")
    wg = Build("C:\\w\\ggml.llamacpp_x\\llama-server.exe", "C:\\w\\ggml.llamacpp_x", "vulkan", ("vulkan",), None,
               "winget")
    check("build_label cuda", lf.build_label(cu) == "CUDA · b12314 · llama.cpp-cuda-b12314", lf.build_label(cu))
    check("build_label winget", lf.build_label(wg) == "Vulkan · winget", lf.build_label(wg))
    m = Model("C:\\m\\Qwen3.8-27B-UD-Q3_K_XL.gguf", "Qwen3.8-27B-UD-Q3_K_XL", 13146393504, "C:\\m")
    check("model_label", lf.model_label(m) == "Qwen3.8-27B-UD-Q3_K_XL · 12.2 GB", lf.model_label(m))
    check("model_label small file in MB", lf.model_label(Model("a", "a", 300 * 1024 ** 2, "C:\\")) == "a · 300 MB")
    a = Model("C:\\one\\x.gguf", "x", 5, "C:\\one")
    b = Model("C:\\two\\x.gguf", "x", 5, "C:\\two")
    c = Model("C:\\p\\two\\x.gguf", "x", 5, "C:\\p\\two")
    labels = lf.unique_labels([a, b, c], lf.model_label)
    check("unique_labels: all unique", len(set(labels)) == 3, f"{labels}")
    check("unique_labels: parent folder added", labels[0].endswith("· one"), f"{labels}")
    same = lf.unique_labels([a, a, a], lf.model_label)
    check("unique_labels: identical items numbered", len(set(same)) == 3, f"{same}")
    w2 = Build("C:\\w2\\ggml.llamacpp_x\\llama-server.exe", "C:\\w2\\ggml.llamacpp_x", "vulkan", ("vulkan",), None,
               "winget")
    bl = lf.unique_labels([cu, wg, w2], lf.build_label)
    check("unique_labels on builds", len(set(bl)) == 3 and bl[0] == lf.build_label(cu), f"{bl}")
    check("unique_labels keeps unique labels as they are",
          lf.unique_labels([a, cu], lambda i: str(type(i).__name__)) == ["Model", "Build"])
    check("unique_labels never raises", lf.unique_labels([1, None], lambda i: 1 / 0) == ["1", "None"]
          and lf.unique_labels(None, str) == [])  # type: ignore[arg-type]
    check("labels never raise on garbage", isinstance(lf.build_label(None), str)  # type: ignore[arg-type]
          and isinstance(lf.model_label(None), str))  # type: ignore[arg-type]
    words = " ".join(bl + labels + [lf.model_label(m)]).casefold()
    check("no marketing words", not any(w in words for w in ("best", "ultra", "fast", "powerful", "!")))


def test_never_raises() -> None:
    section("never raises on garbage input")
    for roots in (["Z:\\no\\such"], ["\\\\evilhost\\share"], [""], [None], 42, "C:\\no\\such\\dir", [b"x"]):
        for fn in (lf.find_builds, lf.find_models):
            try:
                r = fn(roots, time_budget_s=2, env={})  # type: ignore[arg-type]
                check(f"{fn.__name__}({roots!r}) -> list", isinstance(r, list))
            except Exception as exc:
                check(f"{fn.__name__}({roots!r}) -> list", False, repr(exc))
    for kw in ({"time_budget_s": "x"}, {"time_budget_s": None}, {"env": None}, {"env": 5}, {"max_depth": "?"},
               {"extra_dirs": 7}, {"extra_dirs": [None, 3]}, {"min_bytes": "x"}):
        try:
            fn = lf.find_models if "extra_dirs" in kw or "min_bytes" in kw else lf.find_builds
            r = fn([str(TMP / "nothing")], **kw)  # type: ignore[arg-type]
            check(f"{fn.__name__}(**{kw}) -> list", isinstance(r, list))
        except Exception as exc:
            check(f"{fn.__name__}(**{kw}) -> list", False, repr(exc))
    try:
        r = lf.find_all([str(TMP / "nothing")], time_budget_s=1, env={})
        check("find_all on empty -> ([], [])", r == ([], []))
    except Exception as exc:
        check("find_all on empty -> ([], [])", False, repr(exc))


def test_find_all() -> None:
    section("find_all: one walk, both kinds")
    base = TMP / "all"
    exe = make_build(base / "dl" / "llama-b5-bin", ("cuda",))
    mod = make_model(base / "dl" / "models" / "m.gguf")
    pexe = make_build(base / "p" / "llama", ("vulkan",))
    env = env_for(base, PATH=str(pexe.parent))
    builds, models = lf.find_all([str(base / "dl"), str(base / "p")], time_budget_s=10, env=env, min_bytes=SMALL)
    check("find_all builds", progs(builds) == {k(exe), k(pexe)}, f"{builds}")
    check("find_all models", paths(models) == {k(mod)}, f"{models}")
    check("find_all keeps the PATH source", {b.source for b in builds if k(b.program) == k(pexe)} == {"path"})
    check("find_all agrees with find_builds",
          [b.program for b in builds] == [b.program for b in lf.find_builds([str(base / "dl"), str(base / "p")],
                                                                             time_budget_s=10, env=env)])


def test_no_personal_data() -> None:
    section("no personal data / absolute user paths in the source")
    user = os.environ.get("USERNAME", "")
    for f in (ROOT / "llama_finder.py", Path(__file__)):
        text = f.read_text(encoding="utf-8")
        check(f"{f.name}: no user name", not user or len(user) < 3 or user.casefold() not in text.casefold())
        check(f"{f.name}: no C:\\Users\\<name> path", not re.search(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z]", text))
    check("importing llama_finder has no side effects worth noting (module-level state only constants)",
          not any(isinstance(v, threading.Thread) for v in vars(lf).values()))


def smoke_real_pc() -> None:
    section("real-PC smoke (read-only, printed, not asserted)")
    t0 = time.monotonic()
    builds = lf.find_builds()
    dt_b = time.monotonic() - t0
    print(f"  find_builds(): {dt_b:.2f}s, {len(builds)} build(s), nvidia={lf.has_nvidia()}")
    for b, lab in zip(builds, lf.unique_labels(builds, lf.build_label), strict=True):
        print(f"    {lab:55s} {b.source:7s} {b.backends} {b.program}")
    t0 = time.monotonic()
    models = lf.find_models()
    dt_m = time.monotonic() - t0
    print(f"  find_models(): {dt_m:.2f}s, {len(models)} model(s)")
    for m, lab in zip(models, lf.unique_labels(models, lf.model_label), strict=True):
        print(f"    {lab:55s} {m.path}")
    t0 = time.monotonic()
    b2, m2 = lf.find_all()
    dt_a = time.monotonic() - t0
    print(f"  find_all(): {dt_a:.2f}s, {len(b2)} build(s), {len(m2)} model(s)")
    c = lf.choose(builds, models, lf.has_nvidia())
    print(f"  choose(): clear={c.clear} -- {c.why}")
    check("real-PC searches stay within budget (8 s + grace)", max(dt_b, dt_m, dt_a) < 9.0,
          f"{dt_b:.2f}/{dt_m:.2f}/{dt_a:.2f}s")


def main() -> int:
    try:
        for fn in (test_backends, test_lookalikes, test_winget_path_dedupe, test_ranking, test_choose, test_models,
                   test_model_places, test_hf_cache, test_junction_not_followed, test_budget_and_stop,
                   test_with_model_and_args, test_labels, test_never_raises, test_find_all, test_no_personal_data,
                   smoke_real_pc):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
    finally:
        for link in JUNCTIONS:
            try:
                if os.path.lexists(link):
                    os.rmdir(link)        # drop junctions before rmtree so nothing outside TMP is touched
            except OSError:
                pass
        shutil.rmtree(TMP, ignore_errors=True)
        check("temp dir removed", not TMP.exists(), str(TMP))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
