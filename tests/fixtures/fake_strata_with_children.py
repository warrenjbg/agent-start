"""Fake Strata that first spawns child processes, then runs fake_strata_server.py in-process.

Copied to <dir>\\serve\\server.py with fake_strata_server.py next to it as fake_impl.py. Extra args (the rest go to
the fake server, which ignores unknown ones):
  --children <file.json>   JSON list of argv lists; each is started as a child of THIS process (stand-ins for
                           the browser that `--open` / webbrowser.open() starts, engine\\strata.exe, ...)
  --pids <file.json>       written once all children are started: {"<argv0>": pid, ...} in spawn order
"""
import json
import os
import runpy
import subprocess
import sys

children_file = pids_file = None
rest = []
argv = sys.argv[1:]
i = 0
while i < len(argv):
    if argv[i] == "--children" and i + 1 < len(argv):
        children_file = argv[i + 1]
        i += 2
    elif argv[i] == "--pids" and i + 1 < len(argv):
        pids_file = argv[i + 1]
        i += 2
    else:
        rest.append(argv[i])
        i += 1

pids = []
if children_file:
    with open(children_file, encoding="utf-8") as f:
        for child in json.load(f):
            p = subprocess.Popen(child, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            pids.append([child[0], p.pid])
if pids_file:
    tmp = pids_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pids, f)
    os.replace(tmp, pids_file)

sys.argv = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_impl.py")] + rest
runpy.run_path(sys.argv[0], run_name="__main__")
