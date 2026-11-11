"""Integrator check: real detector + endpoint, IDLE only (no clicks, no kills), screenshot, exit."""
import sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import main
app = main.HermesControllerApp()
shot = pathlib.Path(__file__).with_name("ui_real_idle.png")
def snap():
    from PIL import ImageGrab
    app.attributes("-topmost", True); app.update(); time.sleep(0.3)
    x, y = app.winfo_rootx(), app.winfo_rooty()
    ImageGrab.grab(bbox=(x, y, x + app.winfo_width(), y + app.winfo_height())).save(shot)
    print("state:", getattr(app, "_state", "?"))
    for attr in ("_preview", "_preview_label", "preview_label"):
        w = getattr(app, attr, None)
        if w is not None:
            print("preview:", w.cget("text").replace("\n", " | "))
    app._shutdown() if hasattr(app, "_shutdown") else app.destroy()
app.after(12000, snap)
app.mainloop()
print("exited cleanly")
