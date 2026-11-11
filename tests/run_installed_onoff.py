"""Integrator check (user-approved): drive the INSTALLED HermesController.exe like a user (mouse clicks):
Turn on -> 'Start without closing' -> wait Serving -> Turn off -> Idle -> close. Screenshots tests/inst_*.png."""
import os, time, socket, subprocess, sys
import win32api, win32con, win32gui, win32process
from PIL import ImageGrab

EXE = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Hermes Controller", "HermesController.exe")
OUT = os.path.dirname(os.path.abspath(__file__))
T0 = time.time()
def mark(m): print(f"[{time.time()-T0:6.1f}s] {m}", flush=True)

def find(title, pid):
    hs = []
    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == title and \
           win32process.GetWindowThreadProcessId(h)[1] == pid:
            hs.append(h)
    win32gui.EnumWindows(cb, None)
    return hs[0] if hs else None

def client_shot(h, name=None):
    l, t = win32gui.ClientToScreen(h, (0, 0)); _, _, w, hh = win32gui.GetClientRect(h)
    win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.25)
    im = ImageGrab.grab(bbox=(l, t, l + w, t + hh)).convert("RGB")
    if name: im.save(os.path.join(OUT, f"inst_{name}.png"))
    return im, (l, t)

def bbox_of(im, rgb, region=None, tol=6):
    x0, y0, x1, y1 = region or (0, 0, im.width, im.height)
    px = im.load(); xs = []; ys = []
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            p = px[x, y]
            if all(abs(p[i] - rgb[i]) <= tol for i in range(3)):
                xs.append(x); ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if len(xs) > 30 else None

def click(origin, box):
    x = origin[0] + (box[0] + box[2]) // 2; y = origin[1] + (box[1] + box[3]) // 2
    win32api.SetCursorPos((x, y)); time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0); time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)

def led(im):   # LED dot at ~ (20, 22) client px
    return im.getpixel((20, 22)) if im.width > 30 else None

def port_open():
    s = socket.socket(); s.settimeout(0.3)
    try: return s.connect_ex(("127.0.0.1", 8080)) == 0
    finally: s.close()

OFFWHITE, RAISED, GREEN = (237, 237, 235), (38, 38, 42), (60, 199, 126)
p = subprocess.Popen([EXE])
h = None
while h is None and time.time() - T0 < 20:
    h = find("Hermes Controller", p.pid); time.sleep(0.2)
mark(f"installed exe window up (pid {p.pid}); port open={port_open()}")
time.sleep(6)
im, org = client_shot(h, "idle")
btn = bbox_of(im, OFFWHITE, (60, 90, 300, 200))
mark(f"'Turn on' button bbox {btn}; clicking")
click(org, btn)

dlg = None
t1 = time.time()
while time.time() - t1 < 15:
    dlg = find("Close apps before start", p.pid)
    if dlg: break
    time.sleep(0.3)
if dlg:
    time.sleep(0.8)
    dim, dorg = client_shot(dlg, "sweepdialog")
    region = (0, dim.height - 60, dim.width // 2, dim.height)
    b2 = bbox_of(dim, RAISED, region, tol=5)
    mark(f"sweep dialog shown; 'Start without closing' bbox {b2}; clicking")
    click(dorg, b2)
else:
    mark("no sweep dialog (nothing to close)")

shots = {20: False, 40: False}
t_on = time.time(); served = False
while time.time() - t_on < 240:
    im, org = client_shot(h)
    el = time.time() - t_on
    for k in shots:
        if not shots[k] and el >= k:
            im.save(os.path.join(OUT, f"inst_loading_{k}s.png")); shots[k] = True; mark(f"loading screenshot at {k}s")
    c = led(im)
    if c and all(abs(c[i] - GREEN[i]) <= 12 for i in range(3)) and bbox_of(im, RAISED, (60, 90, 300, 200)):
        served = True; break
    time.sleep(1)
mark(f"Serving={served} after {time.time() - t_on:.1f}s; port open={port_open()}")
time.sleep(5)
im, org = client_shot(h, "serving")
btn = bbox_of(im, RAISED, (60, 90, 300, 200), tol=5)
mark(f"'Turn off' bbox {btn}; clicking")
t_off = time.time()
click(org, btn)
idle = False
while time.time() - t_off < 60:
    im, org = client_shot(h)
    if bbox_of(im, OFFWHITE, (60, 90, 300, 200)) and not port_open():
        idle = True; break
    time.sleep(0.5)
mark(f"back to Idle={idle} after {time.time() - t_off:.1f}s; port open={port_open()}")
time.sleep(2.5)
client_shot(h, "after_off")
win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
try:
    p.wait(10); mark(f"exe closed, code {p.returncode}")
except subprocess.TimeoutExpired:
    mark("exe did NOT close on WM_CLOSE")
log = os.path.join(os.environ["APPDATA"], "HermesController", "logs", "controller.log")
lines = open(log, encoding="utf-8").read().splitlines()
start = max(i for i, l in enumerate(lines) if "starting (frozen=True" in l)
for l in lines[start:]:
    if any(k in l for k in ("state ->", "launched Strata", "Strata ready", "stop result", "toast", "ERROR", "WARNING")):
        print("   log:", l[:230])
