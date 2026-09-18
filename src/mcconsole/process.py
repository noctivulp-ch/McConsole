"""Control del proceso del servidor (RCON no puede arrancarlo): pidfile + script de arranque, local o por SSH.

El script de arranque debe ser desacoplado (escribe el pidfile del java, redirige la salida al log). Ejemplo local:

    java ... -jar server.jar nogui <&3 >> console.log 2>&1 &
    echo $! > server.pid; wait $!; rm -f server.pid
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .profiles import ProcessSpec
from .rcon import Rcon


class ProcessControl:
    def __init__(self, spec: ProcessSpec):
        self.spec = spec

    # --- helpers
    def _ssh(self, *args: str) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", self.spec.ssh_host, *args]

    def pid(self) -> int | None:
        if not self.spec.pidfile:
            return None
        if self.spec.type == "ssh":
            try:
                r = subprocess.run(self._ssh(f"pid=$(cat {self.spec.pidfile} 2>/dev/null) && kill -0 $pid 2>/dev/null && echo $pid"),
                                   capture_output=True, text=True, timeout=15)
                return int(r.stdout.strip()) if r.stdout.strip().isdigit() else None
            except (OSError, subprocess.TimeoutExpired):
                return None
        try:
            pid = int(Path(self.spec.pidfile).read_text().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    @property
    def can_start(self) -> bool:
        return bool(self.spec.start)

    # --- acciones
    def start(self) -> None:
        if not self.spec.start:
            raise RuntimeError("el perfil no define process.start")
        if self.spec.type == "ssh":
            subprocess.run(self._ssh(f"nohup setsid {self.spec.start} >/dev/null 2>&1 &"), timeout=20, check=False)
            return
        subprocess.Popen([self.spec.start], cwd=os.path.dirname(os.path.abspath(self.spec.start)) or None,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    def stop(self, rcon: Rcon | None) -> str:
        """Intenta `stop` por RCON; si no, SIGTERM al pid. Devuelve cómo se hizo."""
        if rcon is not None:
            try:
                rcon.command_retry("stop")
                rcon.close()
                return "rcon"
            except Exception:  # noqa: BLE001
                rcon.close()
        pid = self.pid()
        if pid is None:
            return "none"
        if self.spec.type == "ssh":
            subprocess.run(self._ssh(f"kill -TERM {pid}"), timeout=15, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
        return "sigterm"

    def wait_stopped(self, timeout: float = 90.0) -> bool:
        deadline = time.monotonic() + timeout
        interval = 0.2 if self.spec.type == "local" else 2.0
        while time.monotonic() < deadline:
            if self.pid() is None:
                return True
            time.sleep(interval)
        return False


def make_process(spec: ProcessSpec | None) -> ProcessControl | None:
    return ProcessControl(spec) if spec else None
