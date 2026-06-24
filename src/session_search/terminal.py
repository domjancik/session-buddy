from __future__ import annotations

import os
import pty
import signal
import subprocess
import threading
from collections.abc import Callable

from .resume import PreparedResumeCommand


OutputCallback = Callable[[str], None]
ExitCallback = Callable[[int | None], None]


class EmbeddedTerminalProcess:
    def __init__(
        self,
        command: PreparedResumeCommand,
        on_output: OutputCallback,
        on_exit: ExitCallback,
    ) -> None:
        self.command = command
        self.on_output = on_output
        self.on_exit = on_exit
        self.master_fd: int | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.reader: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        try:
            self.process = subprocess.Popen(
                self.command.argv,
                cwd=self.command.cwd or None,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid,
            )
        finally:
            os.close(slave_fd)

        self.reader = threading.Thread(target=self._read_loop, name="session-search-terminal", daemon=True)
        self.reader.start()

    def send_line(self, text: str) -> None:
        if self.master_fd is None or not self.running:
            return
        os.write(self.master_fd, (text + "\n").encode("utf-8", errors="replace"))

    def send_bytes(self, data: bytes) -> None:
        if self.master_fd is None or not self.running:
            return
        os.write(self.master_fd, data)

    def terminate(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def _read_loop(self) -> None:
        assert self.master_fd is not None
        returncode: int | None = None
        try:
            while True:
                try:
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self.on_output(data.decode("utf-8", errors="replace"))
                if self.process is not None and self.process.poll() is not None:
                    break
        finally:
            if self.process is not None:
                returncode = self.process.poll()
                if returncode is None:
                    returncode = self.process.wait()
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                self.master_fd = None
            self.on_exit(returncode)
