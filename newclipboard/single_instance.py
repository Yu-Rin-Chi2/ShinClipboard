from __future__ import annotations

import ctypes
import hashlib
import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable, IO


ERROR_ALREADY_EXISTS = 183
PORT_BASE = 49152
PORT_RANGE = 16383


class SingleInstance:
    """Prevents duplicate instances and signals the running one to show itself."""

    def __init__(self, data_dir: Path):
        resolved = str(Path(data_dir).expanduser().resolve())
        identity = resolved.casefold() if os.name == "nt" else resolved
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.mutex_name = f"Local\\NewClipboard-{digest[:24]}"
        self.port = PORT_BASE + int(digest[:8], 16) % PORT_RANGE
        self._kernel32 = None
        self._mutex_handle = None
        self._lock_stream: IO[bytes] | None = None
        self._server: socket.socket | None = None
        self._listener: threading.Thread | None = None
        self._stopping = threading.Event()

    def acquire(self) -> bool:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
            create_mutex.restype = ctypes.c_void_p
            ctypes.set_last_error(0)
            handle = create_mutex(None, False, self.mutex_name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._kernel32 = kernel32
            self._mutex_handle = handle
            return True

        import fcntl

        lock_path = Path.home() / f".newclipboard-{self.mutex_name.rsplit('-', 1)[-1]}.lock"
        stream = lock_path.open("a+b")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return False
        self._lock_stream = stream
        return True

    def start_listener(self, on_show: Callable[[], None]) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", self.port))
            server.listen(4)
            server.settimeout(0.25)
        except OSError:
            server.close()
            return
        self._server = server

        def listen() -> None:
            while not self._stopping.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with connection:
                    try:
                        request = connection.recv(32)
                    except OSError:
                        continue
                if request.strip() == b"show":
                    on_show()

        self._listener = threading.Thread(target=listen, name="NewClipboardInstanceListener", daemon=True)
        self._listener.start()

    def notify_existing(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2) as connection:
                    connection.sendall(b"show\n")
                return True
            except OSError:
                time.sleep(0.05)
        return False

    def close(self) -> None:
        self._stopping.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._listener and self._listener.is_alive():
            self._listener.join(timeout=0.5)
        self._listener = None

        if self._mutex_handle and self._kernel32:
            self._kernel32.CloseHandle(self._mutex_handle)
            self._mutex_handle = None
        if self._lock_stream:
            self._lock_stream.close()
            self._lock_stream = None
