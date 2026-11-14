"""Self-check for model_files.py. Run: .venv\\Scripts\\python.exe tests\\test_model_files.py

Safe by construction:
  * Every file this suite makes, changes or deletes lives in one temp folder outside the repo, removed at the end.
    The real models folder and %APPDATA% are never read; AGENT_START_HOME points into the temp folder before any
    app import.
  * The only process started is `cmd /c mklink /J` to make directory junctions inside that temp folder.
  * serving_paths is fed fake process lists; one real call checks that a read-only scan returns without raising.
  * Refusal tests pass a recorder as `remove`; only the success, hard-link and in-use tests use the real os.remove,
    always on a file this suite just wrote.
"""
from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
import types
from pathlib import Path

# realpath: a temp folder can be spelled with a short name (C:\Users\RUNNER~1\...), and the module reports long ones
TMP = Path(os.path.realpath(tempfile.mkdtemp(prefix="agentstart-modelfiles-test-")))
os.environ["AGENT_START_HOME"] = str(TMP / "home")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

import model_files as mf  # noqa: E402
from model_files import ModelFile, ModelFileError  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[str] = []
KINDS_SEEN: set[str] = set()
JUNCTIONS: list[Path] = []
REPARSE = 0x400


class Grab(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.INFO:
            self.lines.append(record.getMessage())


GRAB = Grab()
_LOG = logging.getLogger("agent_start")
_LOG.addHandler(GRAB)
_LOG.setLevel(logging.DEBUG)
_LOG.propagate = False


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def skip(name: str, why: str) -> None:
    SKIPPED.append(name)
    print(f"  [SKIP] {name} -- {why}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------------------------------- helpers

def write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def junction(link: Path, target: Path) -> bool:
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    ok = r.returncode == 0 and os.path.lexists(link)
    if ok:
        JUNCTIONS.append(link)
    return ok


def as_listed(p: Path) -> ModelFile:
    """A ModelFile built the way list_folder builds one, for files list_folder would never offer."""
    st = os.lstat(p)
    return ModelFile(str(p), p.name, st.st_size, st.st_mtime_ns)


def pick(folder: Path, name: str) -> ModelFile:
    found = [m for m in mf.list_folder(str(folder)) if m.name == name]
    assert len(found) == 1, f"{name} not listed once in {folder}: {found}"
    return found[0]


def snapshot(root: Path = TMP) -> dict[str, tuple]:
    """Every entry under root (links are recorded, never followed): kind plus bytes and mtime for files."""
    out: dict[str, tuple] = {}
    stack = [str(root)]
    while stack:
        d = stack.pop()
        with os.scandir(d) as it:
            for e in it:
                st = e.stat(follow_symlinks=False)
                if e.is_symlink() or st.st_file_attributes & REPARSE:      # (a junction is a reparse point)
                    try:
                        out[e.path] = ("link", os.readlink(e.path))
                    except OSError:
                        out[e.path] = ("link", "?")
                elif e.is_dir(follow_symlinks=False):
                    out[e.path] = ("dir",)
                    stack.append(e.path)
                else:
                    out[e.path] = ("file", Path(e.path).read_bytes(), st.st_mtime_ns)
    return out


class Rec:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, p: str) -> None:
        self.calls.append(p)


def expect_refusal(label: str, kind: str, file: object, folder: object, serving: object = frozenset(), *,
                   remove: object = None) -> None:
    rec = Rec() if remove is None else remove
    before = snapshot()
    n0 = len(GRAB.lines)
    got: BaseException | None = None
    try:
        mf.delete(file, folder, serving, remove=rec)
    except Exception as e:  # noqa: BLE001
        got = e
    ok = isinstance(got, ModelFileError) and got.kind == kind
    if ok:
        KINDS_SEEN.add(kind)
    check(f"{label}: refused as '{kind}'", ok,
          f"got {type(got).__name__}: {getattr(got, 'kind', '')} {got}" if got else "nothing was refused")
    if isinstance(rec, Rec):
        check(f"{label}: remove was not called", rec.calls == [], repr(rec.calls))
    check(f"{label}: every file in the temp tree is still there with the same bytes", snapshot() == before)
    if isinstance(got, ModelFileError):
        msg = got.message
        name = getattr(file, "name", "")
        check(f"{label}: one plain sentence that names the file, no path",
              str(got) == msg and isinstance(name, str) and name in msg and "\\" not in msg and str(M) not in msg
              and msg.endswith(".") and msg.count(". ") == 0, msg)
        new = GRAB.lines[n0:]
        check(f"{label}: one log line with the kind", len(new) == 1 and f"({kind})" in new[0], repr(new))


def fake_lstat(overrides: dict[str, str]):
    """os.lstat, except that the named files (normcase path) report a symlink ('link': S_IFLNK, no attribute) or
    a regular file carrying a reparse point ('reparse'), which the suite cannot make without admin rights."""
    real = os.lstat

    def lstat(p):
        st = real(p)
        how = overrides.get(os.path.normcase(os.fspath(p)))
        if how == "link":
            return types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o666, st_size=st.st_size,
                                         st_mtime_ns=st.st_mtime_ns, st_file_attributes=0x20)
        if how == "reparse":
            return types.SimpleNamespace(st_mode=stat.S_IFREG | 0o666, st_size=st.st_size,
                                         st_mtime_ns=st.st_mtime_ns, st_file_attributes=0x20 | REPARSE)
        return st
    return lstat


# ----------------------------------------------------------------------------------------------- list_folder

def test_list_folder() -> None:
    section("list_folder")
    L = TMP / "List"
    write(L / "b-model.gguf", b"b" * 30)
    write(L / "A-model.GGUF", b"a" * 10)
    write(L / "c.Gguf", b"c" * 20)
    write(L / "notes.txt", b"n")
    write(L / "x.gguf.part", b"p" * 5)
    write(L / "x.gguf.part.json", b"{}")
    write(L / "dir.gguf" / "inner.gguf", b"i")
    write(L / "sub" / "deep.gguf", b"d")
    write(TMP / "Elsewhere" / "far.gguf", b"f" * 7)
    write(L / "seam-link.gguf", b"s" * 3)
    write(L / "seam-reparse.gguf", b"r" * 4)
    j1 = junction(L / "junc.gguf", TMP / "Elsewhere")
    j2 = junction(L / "jdir", TMP / "Elsewhere")
    (TMP / "Gone").mkdir()
    j3 = junction(L / "dangle.gguf", TMP / "Gone")
    os.rmdir(TMP / "Gone")
    check("junctions made for the listing (mklink /J)", j1 and j2 and j3)
    real = os.path.realpath(L)
    saved = mf._lstat
    mf._lstat = fake_lstat({os.path.normcase(os.path.join(real, "seam-link.gguf")): "link",
                            os.path.normcase(os.path.join(real, "seam-reparse.gguf")): "reparse"})
    try:
        got = mf.list_folder(str(L))
    finally:
        mf._lstat = saved
    check("test seam restored", mf._lstat is os.lstat)
    names = [m.name for m in got]
    check("only the direct regular .gguf files, any case, sorted by name (casefold)",
          names == ["A-model.GGUF", "b-model.gguf", "c.Gguf"], repr(names))
    check("sizes are the file sizes", [m.size for m in got] == [10, 30, 20], repr([m.size for m in got]))
    check("paths are absolute: the folder's realpath joined with the name",
          [m.path for m in got] == [os.path.join(real, n) for n in names], repr([m.path for m in got]))
    check("mtime_ns is the file's", all(m.mtime_ns == os.lstat(m.path).st_mtime_ns for m in got))
    check("a tuple of ModelFile", isinstance(got, tuple) and all(type(m) is ModelFile for m in got))
    check("a subfolder's file, a .gguf.part, a directory named x.gguf, junctions and links are not listed",
          not {"deep.gguf", "inner.gguf", "x.gguf.part", "dir.gguf", "junc.gguf", "dangle.gguf", "far.gguf",
               "seam-link.gguf", "seam-reparse.gguf"} & set(names))
    plain = [m.name for m in mf.list_folder(str(L))]
    check("without the seam the two plain files are listed (the seam is what hid them)",
          "seam-link.gguf" in plain and "seam-reparse.gguf" in plain, repr(plain))
    check("a missing folder gives ()", mf.list_folder(str(TMP / "no-such-folder")) == ())
    check("a file as the folder gives ()", mf.list_folder(str(L / "notes.txt")) == ())
    check("an empty, relative or non-path folder gives ()",
          mf.list_folder("") == () and mf.list_folder("List") == () and mf.list_folder(None) == ()
          and mf.list_folder(12) == ())
    check("forward slashes and other letter case list the same files",
          [m.name for m in mf.list_folder(str(L).upper().replace("\\", "/"))] == plain)
    lj = TMP / "ListJ"
    if junction(lj, L):
        via = mf.list_folder(str(lj))
        check("a folder reached through a junction lists paths under the real folder",
              [m.path for m in via] == [os.path.join(real, m.name) for m in via] and len(via) == len(plain))
    else:
        check("junction to the list folder made", False)


# ----------------------------------------------------------------------------------------------- delete

M = TMP / "Models"
KEEP = {"keep-1.gguf": b"k1" * 50, "keep-2.GGUF": b"k2" * 60, "readme.txt": b"hello"}


def setup_models() -> None:
    for n, b in KEEP.items():
        write(M / n, b)


def test_not_gguf() -> None:
    section("refusal: not_gguf")
    write(M / "notes.txt", b"user notes")
    expect_refusal("a .txt file", "not_gguf", as_listed(M / "notes.txt"), str(M))
    write(M / "big.gguf.part", b"part")
    expect_refusal("a .gguf.part", "not_gguf", as_listed(M / "big.gguf.part"), str(M))
    st = os.lstat(M / "notes.txt")
    expect_refusal("the shown name is .gguf but the path is notes.txt", "not_gguf",
                   ModelFile(str(M / "notes.txt"), "notes.gguf", st.st_size, st.st_mtime_ns), str(M))
    k = as_listed(M / "keep-1.gguf")
    expect_refusal("not a ModelFile (a look-alike object)", "not_gguf",
                   types.SimpleNamespace(path=k.path, name=k.name, size=k.size, mtime_ns=k.mtime_ns), str(M))
    expect_refusal("a stream name (name:stream.gguf)", "not_gguf",
                   ModelFile(k.path + ":evil.gguf", "keep-1.gguf:evil.gguf", k.size, k.mtime_ns), str(M))


def test_outside() -> None:
    section("refusal: outside")
    sub = write(M / "sub" / "inner.gguf", b"inner")
    expect_refusal("a file in a subfolder", "outside", as_listed(sub), str(M))
    sib = write(TMP / "Models-old" / "old.gguf", b"old")
    expect_refusal("a file in a sibling folder whose name starts like the folder", "outside", as_listed(sib), str(M))
    k = as_listed(M / "keep-1.gguf")
    dotted = str(M / "sub") + "\\..\\keep-1.gguf"
    check("(the '..' path names a real file in the folder)", os.path.samefile(dotted, k.path))
    expect_refusal("a path spelled with '..'", "outside",
                   ModelFile(dotted, "keep-1.gguf", k.size, k.mtime_ns), str(M))
    expect_refusal("a path spelled with '.'", "outside",
                   ModelFile(str(M) + "\\.\\keep-1.gguf", "keep-1.gguf", k.size, k.mtime_ns), str(M))
    store = TMP / "store.gguf"
    write(store / "a.gguf", b"a")
    st = os.lstat(store)
    expect_refusal("the folder itself", "outside",
                   ModelFile(str(store), "store.gguf", st.st_size, st.st_mtime_ns), str(store))
    drive = os.path.splitdrive(str(M))[0].upper()
    other = "Q:" if drive != "Q:" else "R:"
    expect_refusal("another drive", "outside",
                   ModelFile(other + "\\Models\\keep-1.gguf", "keep-1.gguf", k.size, k.mtime_ns), str(M))
    expect_refusal("a relative path", "outside", ModelFile("keep-1.gguf", "keep-1.gguf", k.size, k.mtime_ns), str(M))
    expect_refusal("an empty folder argument", "outside", k, "")
    expect_refusal("a relative folder argument", "outside", k, "Models")
    expect_refusal("a device-path spelling (\\\\?\\)", "outside",
                   ModelFile("\\\\?\\" + k.path, "keep-1.gguf", k.size, k.mtime_ns), str(M))


def test_missing() -> None:
    section("refusal: missing")
    p = write(M / "gone.gguf", b"g" * 9)
    m = pick(M, "gone.gguf")
    os.remove(p)
    expect_refusal("deleted after it was listed", "missing", m, str(M))


def test_link() -> None:
    section("refusal: link")
    write(TMP / "Elsewhere" / "far.gguf", b"f" * 7)
    if junction(M / "junc.gguf", TMP / "Elsewhere"):
        expect_refusal("a directory junction named x.gguf", "link", as_listed(M / "junc.gguf"), str(M))
    else:
        check("junction junc.gguf made", False)
    (TMP / "Gone2").mkdir()
    if junction(M / "dangle.gguf", TMP / "Gone2"):
        os.rmdir(TMP / "Gone2")
        expect_refusal("a junction whose target is gone", "link", as_listed(M / "dangle.gguf"), str(M))
    else:
        check("junction dangle.gguf made", False)
    if junction(M / "loop", M):
        k = as_listed(M / "keep-1.gguf")
        expect_refusal("a file reached through a junction (inside the folder) back to the folder", "link",
                       ModelFile(str(M / "loop" / "keep-1.gguf"), "keep-1.gguf", k.size, k.mtime_ns), str(M))
    else:
        check("junction loop made", False)
    write(TMP / "Side" / "x.txt", b"x")
    if junction(TMP / "Side" / "toModels", M):
        k = as_listed(M / "keep-2.GGUF")
        expect_refusal("a file reached through a junction elsewhere that points at the folder", "link",
                       ModelFile(str(TMP / "Side" / "toModels" / "keep-2.GGUF"), "keep-2.GGUF", k.size, k.mtime_ns),
                       str(M))
    else:
        check("junction Side\\toModels made", False)
    if junction(M / "jout", TMP / "Elsewhere"):
        f = as_listed(TMP / "Elsewhere" / "far.gguf")
        expect_refusal("a file reached through a junction to another folder", "outside",
                       ModelFile(str(M / "jout" / "far.gguf"), "far.gguf", f.size, f.mtime_ns), str(M))
    else:
        check("junction jout made", False)
    # File symlinks need SeCreateSymbolicLinkPrivilege (admin or Developer Mode).
    try:
        os.symlink(str(TMP / "Elsewhere" / "far.gguf"), str(M / "sym.gguf"))
        made = True
    except OSError as e:
        made = False
        skip("a file symlink", f"Windows refused os.symlink ({e.strerror or e}); covered by the seam test below")
    if made:
        st = os.lstat(M / "sym.gguf")
        expect_refusal("a file symlink", "link",
                       ModelFile(str(M / "sym.gguf"), "sym.gguf", st.st_size, st.st_mtime_ns), str(M))
        check("a file symlink is not listed", "sym.gguf" not in [m.name for m in mf.list_folder(str(M))])
    # Seam: a symlink and a reparse-point file as lstat reports them, on real regular files.
    sl = write(M / "seam-link.gguf", b"s" * 3)
    sr = write(M / "seam-reparse.gguf", b"r" * 4)
    ml, mr = pick(M, "seam-link.gguf"), pick(M, "seam-reparse.gguf")
    saved = mf._lstat
    mf._lstat = fake_lstat({os.path.normcase(str(sl)): "link", os.path.normcase(str(sr)): "reparse"})
    try:
        expect_refusal("lstat says symlink (seam)", "link", ml, str(M))
        expect_refusal("lstat says regular file with a reparse point (seam)", "link", mr, str(M))
    finally:
        mf._lstat = saved
    check("test seam restored", mf._lstat is os.lstat)


def test_hard_link() -> None:
    section("a hard link is a regular file")
    data = b"H" * 777
    h = write(M / "hard.gguf", data)
    other = TMP / "Elsewhere" / "hard-other.gguf"
    twin = M / "hard-twin.gguf"
    os.link(h, other)
    os.link(h, twin)
    m = pick(M, "hard.gguf")
    before = {k: v for k, v in snapshot().items() if os.path.normcase(k) != os.path.normcase(str(h))}
    n = mf.delete(m, str(M), remove=os.remove)
    check("deleting one name of a hard link returns its size", n == len(data), repr(n))
    check("that name is gone", not os.path.lexists(h))
    check("the other names keep the bytes", other.read_bytes() == data and twin.read_bytes() == data)
    after = snapshot()
    check("nothing else changed (names, kinds, bytes)", {k: v[:2] for k, v in after.items()}
          == {k: v[:2] for k, v in before.items()})


def test_changed() -> None:
    section("refusal: changed")
    p = write(M / "grow.gguf", b"g" * 100)
    m = pick(M, "grow.gguf")
    p.write_bytes(b"g" * 101)
    os.utime(p, ns=(m.mtime_ns, m.mtime_ns))
    st = os.lstat(p)
    check("(same name, same mtime, other size)", st.st_mtime_ns == m.mtime_ns and st.st_size != m.size)
    expect_refusal("same name, different size", "changed", m, str(M))
    p = write(M / "touch.gguf", b"t" * 100)
    m = pick(M, "touch.gguf")
    p.write_bytes(b"u" * 100)
    os.utime(p, ns=(m.mtime_ns + 5_000_000_000, m.mtime_ns + 5_000_000_000))
    st = os.lstat(p)
    check("(same name, same size, other mtime)", st.st_size == m.size and st.st_mtime_ns != m.mtime_ns)
    expect_refusal("same size, different mtime", "changed", m, str(M))


class FakeProc:
    def __init__(self, name: str, cmd: list, cwd: str | None = None, *, deny_cmdline: bool = False,
                 deny_cwd: bool = False, gone: bool = False) -> None:
        self._name, self._cmd, self._cwd = name, cmd, cwd
        self.deny_cmdline, self.deny_cwd, self.gone = deny_cmdline, deny_cwd, gone

    def name(self) -> str:
        if self.gone:
            raise psutil.NoSuchProcess(4242)
        return self._name

    def cmdline(self) -> list:
        if self.deny_cmdline:
            raise psutil.AccessDenied(4242)
        return [str(c) for c in self._cmd]

    def cwd(self) -> str:
        if self.deny_cwd or self._cwd is None:
            raise psutil.AccessDenied(4242)
        return self._cwd


def norm(p: object) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def test_serving() -> None:
    section("refusal: serving")
    p = write(M / "Served-Model.GGUF", b"S" * 64)
    m = pick(M, "Served-Model.GGUF")
    shout = str(p).upper().replace("\\", "/")
    check("(the serving spelling differs in case and slashes)", shout != str(p) and "/" in shout)
    expect_refusal("serving path in other letter case with forward slashes", "serving", m, str(M), {shout})
    expect_refusal("serving given as one plain string", "serving", m, str(M), shout)
    exe = "C:\\llama\\llama-server.exe"
    sp = mf.serving_paths(lambda: [FakeProc("llama-server.exe", [exe, "-m", str(p), "--port", "18080"])])
    expect_refusal("serving from serving_paths()", "serving", m, str(M), sp)
    expect_refusal("an unreadable serving list", "serving", m, str(M), [object()])


def test_in_use_and_failed() -> None:
    section("refusal: in_use / failed")
    p = write(M / "Locked.gguf", b"L" * 333)
    m = pick(M, "Locked.gguf")
    with open(p, "rb"):
        expect_refusal("held open by another handle (real os.remove)", "in_use", m, str(M), remove=os.remove)
    check("the held file survived with its bytes", p.read_bytes() == b"L" * 333)

    def denied(path: str) -> None:
        raise PermissionError(errno.EACCES, "Access is denied")
    expect_refusal("remove raises PermissionError", "in_use", m, str(M), remove=denied)

    class Sharing(OSError):
        winerror = 32

    def sharing(path: str) -> None:
        raise Sharing(errno.EBUSY, "used by another process")
    expect_refusal("remove raises winerror 32", "in_use", m, str(M), remove=sharing)

    class Mapped(OSError):
        winerror = 1224

    def mapped(path: str) -> None:
        raise Mapped(errno.EINVAL, "the file has a user-mapped section open")
    expect_refusal("remove raises winerror 1224 (an engine has the model mapped)", "in_use", m, str(M), remove=mapped)

    def eio(path: str) -> None:
        raise OSError(errno.EIO, "Input/output error")
    expect_refusal("remove raises OSError(EIO)", "failed", m, str(M), remove=eio)


def test_success() -> None:
    section("success")
    data = b"V" * 4096
    p = write(M / "Victim-Q8.gguf", data)
    m = pick(M, "Victim-Q8.gguf")
    rec = Rec()
    n0 = len(GRAB.lines)
    n = mf.delete(m, str(M), {norm(M / "keep-1.gguf")}, remove=rec)
    check("with a recorder: one call, with exactly the file's path", rec.calls == [m.path], repr(rec.calls))
    check("with a recorder: returns the size", n == 4096, repr(n))
    check("with a recorder: one log line with the name and size",
          len(GRAB.lines) - n0 == 1 and "Victim-Q8.gguf" in GRAB.lines[-1] and "4096" in GRAB.lines[-1])
    before = {k: v for k, v in snapshot().items() if os.path.normcase(k) != os.path.normcase(str(p))}
    n = mf.delete(m, str(M), {norm(M / "keep-1.gguf"), norm(M / "keep-2.GGUF")}, remove=os.remove)
    check("real delete: returns the size", n == 4096, repr(n))
    check("real delete: the file is gone", not os.path.lexists(p))
    check("real delete: every other file untouched", snapshot() == before)
    check("the kept files are still listed", {"keep-1.gguf", "keep-2.GGUF"} <= {x.name for x in mf.list_folder(str(M))})

    q = write(M / "Spelled.gguf", b"Q" * 10)
    mq = pick(M, "Spelled.gguf")
    n = mf.delete(mq, str(M).upper().replace("\\", "/"), remove=os.remove)
    check("the folder spelled in other case with forward slashes still matches", n == 10 and not q.exists())
    r = write(M / "Via-Junction.gguf", b"J" * 11)
    mj = TMP / "ModelsJ"
    if junction(mj, M):
        mr = [x for x in mf.list_folder(str(mj)) if x.name == "Via-Junction.gguf"][0]
        n = mf.delete(mr, str(mj), remove=os.remove)
        check("a models folder reached through a junction: its listed file is deleted", n == 11 and not r.exists())
    else:
        check("junction ModelsJ made", False)
    t = write(M / "Lower.gguf", b"l" * 12)
    mt = pick(M, "Lower.gguf")
    rec = Rec()
    mf.delete(ModelFile(mt.path.lower(), "lower.gguf", mt.size, mt.mtime_ns), str(M), remove=rec)
    check("a path in other letter case is the same file (normcase)", rec.calls == [mt.path.lower()] and t.exists())
    os.remove(t)


def test_serving_paths() -> None:
    section("serving_paths")
    S = TMP / "Srv"
    exe = "C:\\llama\\llama-server.exe"
    procs = [
        FakeProc("llama-server.exe", [exe, "-m", S / "a.gguf", "--ctx-size", "4096"]),
        FakeProc("LLAMA-SERVER.EXE", [exe, "--model", S / "b.gguf", "--mmproj", S / "MMPROJ~1.GGU"]),
        FakeProc("llama-server.exe", [exe, f"--model={S / 'C~1.GGU'}", f"--mmproj={S / 'd.gguf'}",
                                      "--lora", S / "e.gguf"]),
        FakeProc("llama-server.exe", [exe, "-m", "rel\\f.gguf"], cwd=str(S / "work")),
        FakeProc("llama-server.exe", [exe, "-m", "g.gguf", "--mmproj", S / "h.gguf"], deny_cwd=True),
        FakeProc("llama-server.exe", [exe, "-m", S / "denied.gguf"], deny_cmdline=True),
        FakeProc("llama-server.exe", [exe, "-m", S / "vanished.gguf"], gone=True),
        FakeProc("python.exe", ["python.exe", "-m", S / "other.gguf", S / "x.gguf"]),
        FakeProc("llama-server.exe", [exe, "-m"]),
    ]
    got = mf.serving_paths(lambda: procs)
    want = {norm(S / "a.gguf"), norm(S / "b.gguf"), norm(S / "MMPROJ~1.GGU"), norm(S / "C~1.GGU"),
            norm(S / "d.gguf"), norm(S / "e.gguf"), norm(S / "work" / "rel" / "f.gguf"), norm(S / "h.gguf")}
    check("-m, --model, --mmproj, the = form, other .gguf arguments, a relative path with its cwd",
          got == want, f"missing {sorted(want - got)} extra {sorted(got - want)}")
    check("a frozenset of normcase(realpath()) strings", isinstance(got, frozenset)
          and all(x == os.path.normcase(x) for x in got))
    check("a relative path whose cwd cannot be read is skipped, not resolved against ours",
          norm("g.gguf") not in got and norm(S / "g.gguf") not in got)
    check("AccessDenied on cmdline, a vanished process and another program's .gguf are skipped",
          not {norm(S / "denied.gguf"), norm(S / "vanished.gguf"), norm(S / "other.gguf"), norm(S / "x.gguf")} & got)
    check("no processes: empty", mf.serving_paths(lambda: []) == frozenset())

    def broken():
        raise psutil.AccessDenied(1)
    check("process_iter itself fails: empty, no exception", mf.serving_paths(broken) == frozenset())

    def half():
        yield FakeProc("llama-server.exe", [exe, "-m", S / "a.gguf"])
        raise psutil.NoSuchProcess(2)
    check("a scan that fails half-way never raises", mf.serving_paths(half) == frozenset({norm(S / "a.gguf")}))
    real = mf.serving_paths()
    check("the real read-only scan returns a frozenset", isinstance(real, frozenset), repr(real))


def test_source() -> None:
    section("source check")
    src = (ROOT / "model_files.py").read_text(encoding="utf-8")
    check("exactly one call that deletes: one 'remove('", src.count("remove(") == 1 and "remove(path)" in src,
          str(src.count("remove(")))
    bad = [w for w in ("rmtree", "subprocess", "os.system", "send2trash", "os.unlink(", "os.rmdir", "glob",
                       "os.rename", "os.replace", "shutil", "open(", ".write", "os.walk", "Popen")
           if w in src]
    check("no rmtree, subprocess, os.system, send2trash, os.unlink, os.rmdir, glob, rename, write, walk", not bad,
          repr(bad))
    check("the module imports no Tk and nothing of the network", not any(
        w in src for w in ("tkinter", "customtkinter", "urllib", "socket", "http", "requests")))


def main() -> int:
    print(f"temp folder: {TMP}")
    try:
        setup_models()
        for fn in (test_list_folder, test_not_gguf, test_outside, test_missing, test_link, test_hard_link,
                   test_changed, test_serving, test_in_use_and_failed, test_success, test_serving_paths,
                   test_source):
            try:
                fn()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
        check("every refusal kind was seen", KINDS_SEEN == set(mf.KINDS), repr(sorted(set(mf.KINDS) - KINDS_SEEN)))
        check("the kept files were never touched",
              all((M / n).read_bytes() == b for n, b in KEEP.items()))
    finally:
        mf._lstat = os.lstat
        for j in reversed(JUNCTIONS):
            try:
                os.rmdir(j)          # removes the junction itself, never its target
            except OSError:
                pass
        shutil.rmtree(TMP, ignore_errors=True)
        check("temp dir removed", not TMP.exists(), str(TMP))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed, {len(SKIPPED)} skipped")
    for n in SKIPPED:
        print(f"  SKIPPED: {n}")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
