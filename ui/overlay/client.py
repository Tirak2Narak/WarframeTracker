"""The tracker's side of the overlay helper.

`OverlayClient` starts `ui.overlay.helper` as a child process on the `xcb`
platform, listens on a local socket the helper connects to, and passes JSON
lines both ways (the protocol is in `helper`). Nothing starts until a palette or
a reward overlay is asked for: not at program start, not in the self-test, not in a release
build. The process is ended when the tracker ends, and the helper ends by
itself when the connection drops.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import (
    QObject, QProcess, QProcessEnvironment, QTimer, Signal,
)
from PySide6.QtNetwork import QLocalServer

#: The folder `ui` lies in; the helper needs it on its import path, the way
#: `bin/wf-gui` puts it on the tracker's.
ROOT = Path(__file__).resolve().parent.parent.parent

#: The commands that start the helper when none runs. Any other command with no
#: helper running is dropped: there is nothing to tell.
STARTING_COMMANDS = ("palette_show", "reward_show")

#: How long the helper may take to connect before it counts as failed.
CONNECT_TIMEOUT_MS = 10_000

#: Longest the quit of the helper is waited for before it is asked to terminate.
QUIT_WAIT_MS = 1500


def helper_command(socket_name: str) -> tuple[str, list[str]]:
    """Program and arguments of the helper. A function so a test can replace it."""
    return sys.executable, ["-m", "ui.overlay.helper", "--socket", socket_name]


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


class OverlayClient(QObject):
    #: A `state` message of the helper, as a dict.
    state = Signal(dict)
    #: A `reward` message of the helper (the reward overlay's state), as a dict.
    reward_state = Signal(dict)
    #: A message the user can read: the helper did not start, or died.
    failed = Signal(str)
    #: The helper process is gone (ended, stopped or failed).
    stopped = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._server: QLocalServer | None = None
        self._socket = None
        self._queue: list[dict] = []
        self._buffer = b""
        self._stderr = ""
        self._stopping = False
        self._start_after_stop: dict | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(CONNECT_TIMEOUT_MS)
        self._timer.timeout.connect(self._connect_timed_out)

    # -- public ------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._process is not None and not self._stopping

    def send(self, command: dict) -> None:
        """Send a command. Only `STARTING_COMMANDS` start the helper; any other
        command with no helper running is dropped (there is nothing to tell)."""
        if self._stopping:
            if command.get("cmd") in STARTING_COMMANDS:
                self._start_after_stop = command
            return
        if self._process is None:
            if command.get("cmd") not in STARTING_COMMANDS:
                return
            if not self._spawn():
                return
        if self._socket is None:
            self._queue.append(command)
            return
        self._write(command)

    def stop(self) -> None:
        """End the helper (and with it every overlay)."""
        self._start_after_stop = None
        if self._process is None or self._stopping:
            return
        self._stopping = True
        self._timer.stop()
        if self._socket is not None:
            self._write({"cmd": "quit"})
        else:
            self._process.terminate()
        QTimer.singleShot(QUIT_WAIT_MS, self._terminate_if_still_running)

    def shutdown(self) -> None:
        """At the end of the tracker: wait a moment for the helper, then ask it
        to terminate."""
        self._start_after_stop = None
        process = self._process
        if process is None:
            return
        self._stopping = True
        self._timer.stop()
        if self._socket is not None:
            self._write({"cmd": "quit"})
        if not process.waitForFinished(QUIT_WAIT_MS):
            process.terminate()
            process.waitForFinished(QUIT_WAIT_MS)

    # -- starting ------------------------------------------------------------
    def _spawn(self) -> bool:
        name = f"wft-overlay-{os.getuid()}-{os.getpid()}"
        QLocalServer.removeServer(name)
        server = QLocalServer(self)
        if not server.listen(name):
            self.failed.emit(f"The overlay could not open its socket: {server.errorString()}")
            server.deleteLater()
            return False
        server.newConnection.connect(self._connected)
        program, arguments = helper_command(name)
        process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("QT_QPA_PLATFORM", "xcb")
        previous = environment.value("PYTHONPATH")
        environment.insert("PYTHONPATH", f"{ROOT}{os.pathsep}{previous}" if previous else str(ROOT))
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(ROOT))
        process.readyReadStandardError.connect(self._collect_stderr)
        process.errorOccurred.connect(self._process_error)
        process.finished.connect(self._finished)
        self._server, self._process = server, process
        self._socket, self._queue, self._buffer, self._stderr = None, [], b"", ""
        self._stopping = False
        process.start(program, arguments)
        self._timer.start()
        return True

    def _connected(self) -> None:
        if self._server is None:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        self._timer.stop()
        self._socket = socket
        socket.readyRead.connect(self._read)
        # Only one helper belongs to this server.
        self._server.close()
        for command in self._queue:
            self._write(command)
        self._queue = []

    # -- talking ----------------------------------------------------------------
    def _write(self, command: dict) -> None:
        if self._socket is not None:
            self._socket.write((json.dumps(command) + "\n").encode())
            self._socket.flush()

    def _read(self) -> None:
        self._buffer += bytes(self._socket.readAll())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            try:
                message = json.loads(line.decode())
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "state":
                self.state.emit(message)
            elif message.get("type") == "reward":
                self.reward_state.emit(message)
            elif message.get("type") == "error":
                self.failed.emit(f"The overlay reported: {message.get('message', '')}")

    # -- ending -------------------------------------------------------------------
    def _collect_stderr(self) -> None:
        if self._process is not None and len(self._stderr) < 8000:
            self._stderr += bytes(self._process.readAllStandardError()).decode(errors="replace")

    def _process_error(self, error) -> None:
        if error == QProcess.FailedToStart and self._process is not None:
            program = self._process.program()
            self._fail(f"The overlay helper could not be started ({program}): "
                       f"{self._process.errorString()}")

    def _connect_timed_out(self) -> None:
        self._fail("The overlay helper did not answer within "
                   f"{CONNECT_TIMEOUT_MS // 1000} seconds.")

    def _terminate_if_still_running(self) -> None:
        if self._process is not None and self._stopping:
            self._process.terminate()

    def _fail(self, message: str) -> None:
        """Report, then take the process down; `_finished` cleans up."""
        process = self._process
        if process is None:
            return
        self.failed.emit(message)
        self._stopping = True
        if process.state() == QProcess.NotRunning:
            self._cleanup()
        else:
            process.terminate()

    def _finished(self, code, status) -> None:
        process = self._process
        if process is None:
            return
        self._collect_stderr()
        expected = self._stopping
        connected = self._socket is not None
        line = first_line(self._stderr)
        self._cleanup()
        if not expected:
            if line:
                self.failed.emit(f"The overlay helper stopped: {line}")
            elif code != 0 or status != QProcess.NormalExit:
                self.failed.emit(f"The overlay helper stopped (exit code {code}).")
            elif not connected:
                self.failed.emit("The overlay helper ended before it connected.")

    def _cleanup(self) -> None:
        process, server = self._process, self._server
        self._process = self._server = self._socket = None
        self._queue, self._buffer, self._stopping = [], b"", False
        self._timer.stop()
        if server is not None:
            server.close()
            server.deleteLater()
        if process is not None:
            process.deleteLater()
        self.stopped.emit()
        pending, self._start_after_stop = self._start_after_stop, None
        if pending is not None:
            self.send(pending)
