"""PySide6 interface for warframe-tracker.

Everything runs in a single Qt process: the main window with its five
views, the always-open relic panel, and every long-running job as a QThread
worker under ui/workers/. Nothing here talks to a subprocess or another
runtime.
"""
