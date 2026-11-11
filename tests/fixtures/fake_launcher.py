"""Launcher layer for tests: `python fake_launcher.py <serve\\server.py> args...` spawns the server as a child and
waits, like the Strata venv redirector (its own cmdline also contains serve\\server.py)."""
import subprocess
import sys

child = subprocess.Popen([sys.executable] + sys.argv[1:])
sys.exit(child.wait())
