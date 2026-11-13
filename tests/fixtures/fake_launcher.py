"""Launcher layer for tests: spawns the server (argv[1], with the remaining arguments) as a child and waits, like
the Strata venv redirector. The tests copy this file to <tmp>\\redirector\\serve\\server.py, so that - as with the
real redirector - the script its own command line runs is a serve\\server.py."""
import subprocess
import sys

child = subprocess.Popen([sys.executable] + sys.argv[1:])
sys.exit(child.wait())
