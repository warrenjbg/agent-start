"""Single source of the app version.

Everything else consumes it: the UI/log line import it, `tools/sync_version.py` (run by build.bat) regenerates
`installer/version_info.txt` and `installer/version.iss` from it, and HermesController.iss includes the latter.
"""
__version__ = "1.4.0"
