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
    print("state:", app.state_name)
    print("preview:", app.preview.cget("text").replace("\n", " | "))
    app._finish_exit()
app.after(12000, snap)
app.mainloop()
print("exited cleanly")
