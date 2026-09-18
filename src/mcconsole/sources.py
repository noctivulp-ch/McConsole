"""Fuentes de log: fichero local (tail -F tolerante a truncado/rotación) o remoto por SSH (`tail -F` en el host).

Interfaz común: `LogSource.follow(emit, stop, last_lines)` corre en un hilo y llama a `emit(line)` por línea.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from .profiles import LogSpec

Emit = Callable[[str], None]


class LogSource(Protocol):
    label: str

    def follow(self, emit: Emit, stop: threading.Event, last_lines: int) -> None: ...
    def tail(self, n: int) -> list[str]: ...


class LocalFileSource:
    def __init__(self, path: str):
        self.path = Path(path)
        self.label = self.path.name

    def tail(self, n: int) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            return []

    def follow(self, emit: Emit, stop: threading.Event, last_lines: int) -> None:
        pos, inode, f, printed = 0, None, None, False
        while not stop.is_set():
            try:
                st = self.path.stat()
            except FileNotFoundError:
                if f:
                    f.close(); f = None
                stop.wait(0.5)
                continue
            if f is None or st.st_ino != inode or st.st_size < pos:
                if f:
                    f.close()
                f = self.path.open("r", encoding="utf-8", errors="replace")
                inode = st.st_ino
                if not printed:
                    for line in f.readlines()[-last_lines:]:
                        emit(line.rstrip("\n"))
                    printed = True
                elif st.st_size < pos:
                    emit("\x1b[90m— log truncado (¿servidor reiniciado?) —\x1b[0m")
                pos = f.tell()
            line = f.readline()
            if line:
                if line.endswith("\n"):
                    pos = f.tell()
                    emit(line.rstrip("\n"))
                else:  # línea a medio escribir
                    f.seek(pos)
                    stop.wait(0.1)
            else:
                stop.wait(0.2)
        if f:
            f.close()


class SshTailSource:
    """`ssh host tail -n N -F path`. Requiere acceso por clave (sin prompt de contraseña)."""

    def __init__(self, ssh_host: str, path: str):
        self.ssh_host, self.path = ssh_host, path
        self.label = f"{ssh_host}:{Path(path).name}"

    def _ssh(self, *args: str) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", self.ssh_host, *args]

    def tail(self, n: int) -> list[str]:
        try:
            r = subprocess.run(self._ssh("tail", "-n", str(n), self.path), capture_output=True, text=True, timeout=15)
            return r.stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return []

    def follow(self, emit: Emit, stop: threading.Event, last_lines: int) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                proc = subprocess.Popen(self._ssh("tail", "-n", str(last_lines), "-F", self.path),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            except OSError as e:
                emit(f"\x1b[91mssh: {e}\x1b[0m")
                stop.wait(backoff); backoff = min(backoff * 2, 30)
                continue
            assert proc.stdout is not None
            backoff = 1.0
            for line in proc.stdout:
                if stop.is_set():
                    break
                emit(line.rstrip("\n"))
            proc.kill()
            if stop.is_set():
                break
            emit(f"\x1b[93m— conexión ssh a {self.ssh_host} perdida; reintento en {backoff:.0f}s —\x1b[0m")
            stop.wait(backoff); backoff = min(backoff * 2, 30)
            last_lines = 0  # al reconectar no repetir el histórico


def make_source(spec: LogSpec) -> LogSource | None:
    if not spec.path:
        return None
    if spec.type == "ssh":
        return SshTailSource(spec.ssh_host, spec.path)
    return LocalFileSource(spec.path)
