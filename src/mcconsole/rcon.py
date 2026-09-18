"""Cliente RCON (protocolo Source, el que usa Minecraft). Sin dependencias; síncrono y con lock.

Núcleo sin UI: lo usan tanto la TUI (desde un worker) como la CLI.
"""
from __future__ import annotations

import socket
import struct
import threading

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconAuthError(PermissionError):
    pass


class Rcon:
    """Conexión RCON persistente. `command()` es thread-safe; reconecta perezosamente si se cayó."""

    def __init__(self, host: str, port: int, password: str, timeout: float = 4.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock: socket.socket | None = None
        self._id = 0
        self.lock = threading.Lock()

    # --- protocolo
    def _send(self, kind: int, body: str) -> int:
        self._id = (self._id % 1_000_000) + 1
        payload = struct.pack("<ii", self._id, kind) + body.encode("utf-8") + b"\x00\x00"
        assert self.sock is not None
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)
        return self._id

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("conexión cerrada por el servidor")
            buf += chunk
        return buf

    def _recv(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._recv_exact(4))
        data = self._recv_exact(length)
        pid, kind = struct.unpack("<ii", data[:8])
        return pid, kind, data[8:-2].decode("utf-8", errors="replace")

    # --- API
    def connect(self) -> None:
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self.sock = s
        self._send(SERVERDATA_AUTH, self.password)
        for _ in range(2):  # algunos servidores mandan un RESPONSE_VALUE vacío antes del AUTH_RESPONSE
            pid, kind, _ = self._recv()
            if kind == SERVERDATA_AUTH_RESPONSE:
                if pid == -1:
                    self.close()
                    raise RconAuthError("contraseña RCON rechazada")
                return
        self.close()
        raise ConnectionError("respuesta de autenticación inesperada")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def command(self, cmd: str) -> str:
        """Ejecuta un comando y devuelve la respuesta completa (junta multipaquete con un centinela)."""
        with self.lock:
            if self.sock is None:
                self.connect()
            cmd_id = self._send(SERVERDATA_EXECCOMMAND, cmd)
            sentinel = self._send(SERVERDATA_RESPONSE_VALUE, "")
            parts: list[str] = []
            while True:
                pid, _, body = self._recv()
                if pid == sentinel:
                    return "".join(parts)
                if pid == cmd_id:
                    parts.append(body)

    def command_retry(self, cmd: str) -> str:
        """Un reintento con reconexión si la conexión estaba muerta."""
        try:
            return self.command(cmd)
        except OSError:
            self.close()
            self.connect()
            return self.command(cmd)
