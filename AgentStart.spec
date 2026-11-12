# PyInstaller spec — Agent Start (one-folder, windowed). Build via build.bat.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

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
                   "engine_log", "ram_plan", "reopen", "tray", "updater", "update_controller", "strata_finder", "game_watch", "mem_watch", "watch_controller", "fit_check", "fit_watch", "resume_watch", "usage_history", "history_dialog", "engines", "engines.base", "engines.strata", *collect_submodules("engines"), "autostart", "toasts", "win32gui", "win32api", "win32con"],
    excludes=["pytest", "unittest", "pydoc", "test", "lib2to3"],
    # PIL is needed at run time (tray.py draws the status icon). build.bat runs `AgentStart.exe --selftest` after
    # the build so a missing module in the frozen app fails the build instead of silently losing the tray.
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="AgentStart",
    icon="assets/app.ico",
    console=False,
    version="installer/version_info.txt",
    upx=False,                      # UPX-packed binaries trip antivirus heuristics
)
coll = COLLECT(exe, a.binaries, a.datas, name="AgentStart", upx=False)
