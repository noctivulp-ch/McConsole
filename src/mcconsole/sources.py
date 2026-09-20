"""Fuentes de log: fichero local (tail -F tolerante a truncado/rotación), remoto por SSH (`tail -F` en el host) o
remoto por SFTP (sondeo; paneles sin shell como Pterodactyl).

Interfaz común: `LogSource.follow(emit, stop, last_lines)` corre en un hilo y llama a `emit(line)` por línea.
`last_lines=None` = todo el archivo actual (con logs rotados por arranque, eso es "el arranque completo").
Si el archivo cambia de inode (rotación por reinicio: `console.log` es un symlink al del arranque actual), se
reabre y se avisa; si se trunca, también.
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

    def follow(self, emit: Emit, stop: threading.Event, last_lines: int | None) -> None:
        pos, inode, f, printed = 0, None, None, False
        while not stop.is_set():
            try:
                st = self.path.stat()  # sigue el symlink: al rotar cambia el inode
            except FileNotFoundError:
                if f:
                    f.close(); f = None
                stop.wait(0.5)
                continue
            rotated = f is not None and st.st_ino != inode
            truncated = f is not None and not rotated and st.st_size < pos
            if f is None or rotated or truncated:
                if f:
                    f.close()
                f = self.path.open("r", encoding="utf-8", errors="replace")
                inode = st.st_ino
                if not printed:
                    lines = f.readlines()
                    for line in (lines if last_lines is None else lines[-last_lines:]):
                        emit(line.rstrip("\n"))
                    printed = True
                elif rotated:
                    emit(f"\x1b[96m— nuevo archivo de log ({self.path.resolve().name}): arranque nuevo —\x1b[0m")
                elif truncated:
                    emit("\x1b[90m— log truncado —\x1b[0m")
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

    def follow(self, emit: Emit, stop: threading.Event, last_lines: int | None) -> None:
        backoff = 1.0
        n = 20000 if last_lines is None else last_lines  # remoto: tope razonable en vez de "todo"
        while not stop.is_set():
            try:
                proc = subprocess.Popen(self._ssh("tail", "-n", str(n), "-F", self.path),
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
            n = 0  # al reconectar no repetir el histórico


class SftpPollSource:
    """Log remoto por SFTP (paneles tipo Pterodactyl: hay SFTP pero no shell). Sondea el tamaño del archivo cada
    `interval` s y lee solo lo nuevo; si el archivo encoge (rotación/reinicio) vuelve a empezar. Requiere `paramiko`
    (extra `sftp`). Credenciales: `sftp_user` + `sftp_password_file` (o `sftp_key`) del [log] del perfil."""

    def __init__(self, host: str, port: int, user: str, path: str, password: str | None = None, key: str | None = None,
                 interval: float = 2.0):
        self.host, self.port, self.user, self.path = host, port, user, path
        self.password, self.key, self.interval = password, key, interval
        self.label = f"sftp://{host}:{Path(path).name}"

    def _client(self):
        import paramiko  # import perezoso: solo perfiles sftp lo necesitan
        t = paramiko.Transport((self.host, self.port))
        if self.key:
            t.connect(username=self.user, pkey=paramiko.Ed25519Key.from_private_key_file(str(Path(self.key).expanduser())))
        else:
            t.connect(username=self.user, password=self.password)
        return t, paramiko.SFTPClient.from_transport(t)

    def tail(self, n: int) -> list[str]:
        try:
            t, sftp = self._client()
            try:
                with sftp.open(self.path) as f:
                    f.seek(max(0, f.stat().st_size - 512 * 1024))
                    return f.read().decode("utf-8", "replace").splitlines()[-n:]
            finally:
                sftp.close(); t.close()
        except Exception:  # noqa: BLE001
            return []

    def follow(self, emit: Emit, stop: threading.Event, last_lines: int | None) -> None:
        backoff, pos, printed, rest = 1.0, 0, False, ""
        while not stop.is_set():
            try:
                t, sftp = self._client()
            except Exception as e:  # noqa: BLE001
                emit(f"\x1b[91msftp: {e}\x1b[0m")
                stop.wait(backoff); backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            try:
                while not stop.is_set():
                    try:
                        size = sftp.stat(self.path).st_size
                    except IOError:
                        stop.wait(self.interval); continue
                    if size < pos:
                        emit("\x1b[90m— log reiniciado —\x1b[0m"); pos, rest = 0, ""
                    if not printed:
                        with sftp.open(self.path) as f:
                            f.seek(max(0, size - 2 * 1024 * 1024) if last_lines is not None else 0)
                            lines = f.read().decode("utf-8", "replace").splitlines()
                        for line in (lines if last_lines is None else lines[-last_lines:]):
                            emit(line)
                        pos, printed = size, True
                    elif size > pos:
                        with sftp.open(self.path) as f:
                            f.seek(pos); chunk = f.read(size - pos).decode("utf-8", "replace")
                        pos = size
                        data = rest + chunk; parts = data.split("\n"); rest = parts.pop()
                        for line in parts:
                            emit(line)
                    stop.wait(self.interval)
            except Exception as e:  # noqa: BLE001 — conexión caída: reconectar
                emit(f"\x1b[91msftp: {e}; reconectando…\x1b[0m")
            finally:
                try: sftp.close(); t.close()
                except Exception: pass
            stop.wait(backoff)


def make_source(spec: LogSpec) -> LogSource | None:
    if not spec.path:
        return None
    if spec.type == "ssh":
        return SshTailSource(spec.ssh_host, spec.path)
    if spec.type == "sftp":
        pw = None
        if spec.sftp_password_file:
            pw = Path(spec.sftp_password_file).expanduser().read_text().strip()
        return SftpPollSource(spec.sftp_host, spec.sftp_port, spec.sftp_user, spec.path, password=pw, key=spec.sftp_key or None)
    return LocalFileSource(spec.path)
