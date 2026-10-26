"""QThread workers.

Everything that can take more than a UI frame's worth of time — a data update,
a video scan, waiting on hotkey events — runs here, never on the GUI thread and
never as a subprocess (one process, no IPC).
"""
