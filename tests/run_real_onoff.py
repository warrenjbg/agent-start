"""Integrator check (user-approved): REAL Strata ON -> ACTIVE -> OFF -> IDLE through the real app.
Sweep dialog is answered 'start_only' (nothing is closed). Screenshots tests/ui_real_*.png."""
import sys, time, json, socket, pathlib, logging, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import main
from tkinter import messagebox
from sweep_dialog import SweepDialog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s", stream=sys.stdout)
messagebox.askyesno = lambda *a, **k: (print("ASKYESNO", a), False)[1]   # never force anything
HERE = pathlib.Path(__file__).resolve().parent
app = main.AgentStartApp()
T0 = time.monotonic()
log = []

def shot(name):
    from PIL import ImageGrab
    app.attributes("-topmost", True); app.update(); time.sleep(0.3)
    x, y = app.winfo_rootx(), app.winfo_rooty()
    ImageGrab.grab(bbox=(x, y, x + app.winfo_width(), y + app.winfo_height())).save(HERE / f"ui_real_{name}.png")

def mark(msg):
    line = f"[{time.monotonic() - T0:6.1f}s] {msg}"
    print(line, flush=True); log.append(line)

def http(path):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open("http://127.0.0.1:8080" + path, timeout=3) as r:
        return json.loads(r.read())

def port_open():
    s = socket.socket(); s.settimeout(0.3)
    try: return s.connect_ex(("127.0.0.1", 8080)) == 0
    finally: s.close()

phase = {"p": "start", "t": time.monotonic(), "ready_at": None}
def step():
    st = app.state_name
    if phase["p"] == "start":
        mark(f"initial state {st}; port open={port_open()}; pressing ON")
        app.on_toggle(); phase.update(p="wait_dialog_or_active", t=time.monotonic())
    elif phase["p"] == "wait_dialog_or_active":
        if isinstance(app.dialog, SweepDialog):
            mark(f"sweep dialog shown with {len(app.dialog._rows)} candidates; choosing 'Start without closing'")
            shot("sweepdialog")
            app.dialog._decide("start_only")
        elif st == "ACTIVE":
            phase["ready_at"] = time.monotonic() - T0
            mark(f"ACTIVE (model_id={app.model_id}, loaded={app.loaded})")
            try:
                h = http("/health"); mdl = http("/v1/models")
                mark(f"/health service={h.get('service')} loaded={h.get('loaded')} model={h.get('model')}; /v1/models ids={[d.get('id') for d in mdl.get('data', [])]}")
            except Exception as e:
                mark(f"HTTP check failed: {e}")
            shot("active"); phase.update(p="hold", t=time.monotonic())
        elif st == "ERROR":
            mark(f"ERROR: {app.error_text}"); shot("error"); phase["p"] = "done"
        elif time.monotonic() - phase["t"] > 240:
            mark(f"timeout waiting for ACTIVE (state {st}, preview={app.preview.cget('text')!r})"); phase["p"] = "done"
    elif phase["p"] == "hold":
        if time.monotonic() - phase["t"] > 6:   # let the monitor reconcile a couple of ticks
            mark(f"state after 6 s hold: {app.state_name} ({app.preview.cget('text')!r}); pressing OFF")
            app.on_toggle(); phase.update(p="wait_idle", t=time.monotonic())
    elif phase["p"] == "wait_idle":
        if st == "IDLE":
            ep = app.endpoint
            mark(f"IDLE: preview={app.preview.cget('text')!r}; telemetry={app.telemetry.cget('text')!r}; port open={port_open()}; toast={getattr(ep, 'last_toast', None)!r}")
            shot("after_off"); phase["p"] = "done"
        elif st == "ERROR":
            mark(f"ERROR on stop: {app.error_text}"); shot("stop_error"); phase["p"] = "done"
        elif time.monotonic() - phase["t"] > 60:
            mark(f"timeout waiting for IDLE (state {st})"); phase["p"] = "done"
    if phase["p"] == "done":
        (HERE / "real_onoff_result.txt").write_text("\n".join(log), encoding="utf-8")
        app._finish_exit(); return
    app.after(500, step)

app.after(1500, step)
app.mainloop()
print("exited; port open:", port_open())
