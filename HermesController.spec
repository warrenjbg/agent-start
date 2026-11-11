# PyInstaller spec — Hermes Controller (one-folder, windowed). Build via build.bat.
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("customtkinter") + [
    ("assets/app.ico", "assets"),
    ("assets/app.png", "assets"),
    ("build/config.default.json", "."),
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    datas=datas,
    # detector / server_manager are imported lazily in main._build_real_deps; pynvml lazily in vram_sampler.
    # The rest are plain imports PyInstaller finds itself - listed so a refactor to lazy imports cannot drop them.
    hiddenimports=["detector", "server_manager", "win32timezone", "pynvml", "version", "launcher_bat",
                   "settings_dialog", "metrics_store", "vram_sampler", "graphs",
                   "engine_log", "ram_plan"],
    excludes=["PIL", "pytest", "unittest", "pydoc", "test", "lib2to3"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HermesController",
    icon="assets/app.ico",
    console=False,
    version="installer/version_info.txt",
    upx=False,                      # UPX-packed binaries trip antivirus heuristics
)
coll = COLLECT(exe, a.binaries, a.datas, name="HermesController", upx=False)
