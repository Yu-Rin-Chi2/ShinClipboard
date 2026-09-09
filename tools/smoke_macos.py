"""Verify a built app reaches its UI loop, using disposable application data.

Run from the repository root: .venv/bin/python tools/smoke_macos.py
"""
import hashlib
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import Quartz


def main():
    executable = Path("dist/ShinClipboard.app/Contents/MacOS/ShinClipboard").resolve()
    with tempfile.TemporaryDirectory(prefix="shinclipboard-smoke-") as directory:
        # A window can appear only after the queued IPC request reaches Tk.
        Path(directory, "config.json").write_text(json.dumps({
            "schema_version": 1, "settings": {"start_minimized": True},
            "groups": [], "transforms": [],
        }), encoding="utf-8")
        digest = hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()
        port = 49152 + int(digest[:8], 16) % 16383
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen([str(executable), "--data-dir", directory], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 25
                signalled = False
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"App exited during startup: {process.returncode}")
                    if not signalled:
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.2) as connection:
                                connection.sendall(b"show\n")
                            signalled = True
                        except OSError:
                            pass
                    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
                    if signalled and any(w.get("kCGWindowOwnerPID") == process.pid and w.get("kCGWindowLayer") == 0 for w in windows):
                        print("PASS: app initialized, accepted show request, and displayed a window")
                        return
                    time.sleep(0.2)
                raise RuntimeError("App did not initialize and display a window within 25 seconds")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                (Path.home() / f".shinclipboard-{digest[:24]}.lock").unlink(missing_ok=True)
                log.seek(0)
                output = log.read().decode(errors="replace")
                if output:
                    print(output)


if __name__ == "__main__":
    main()
