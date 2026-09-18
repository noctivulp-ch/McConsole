"""Servidor RCON falso en un hilo + log temporal, para probar la TUI y la CLI sin Minecraft.

Por qué: la consola tiene que arrancar y ser útil con el servidor caído, reconectar cuando vuelve, y no
reventar sin TTY. Eso solo se prueba con un servidor controlable desde el test.
"""
import socket
import struct
import threading
from pathlib import Path

import pytest

from mcconsole.profiles import LogSpec, Profile


class FakeRcon:
    def __init__(self, password="secret"):
        self.password = password
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.received: list[str] = []
        self.responses: dict[str, str] = {"list": "There are 0 of a max of 5 players online:", "tps": "§aTPS: 20.0"}
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(5)
        authed = False
        try:
            while True:
                raw = conn.recv(4)
                if not raw:
                    return
                (n,) = struct.unpack("<i", raw)
                data = b""
                while len(data) < n:
                    data += conn.recv(n - len(data))
                pid, kind = struct.unpack("<ii", data[:8])
                body = data[8:-2].decode()
                if kind == 3:
                    ok = body == self.password
                    authed = ok
                    self._send(conn, pid if ok else -1, 2, "")
                elif kind == 2:
                    if not authed:
                        return
                    self.received.append(body)
                    self._send(conn, pid, 0, self.responses.get(body, ""))
                else:
                    self._send(conn, pid, 0, "")
        except OSError:
            return

    @staticmethod
    def _send(conn, pid, kind, body):
        payload = struct.pack("<ii", pid, kind) + body.encode() + b"\x00\x00"
        conn.sendall(struct.pack("<i", len(payload)) + payload)

    def close(self):
        self._stop.set()
        self.sock.close()


@pytest.fixture
def fake_server():
    s = FakeRcon()
    yield s
    s.close()


@pytest.fixture
def profile(tmp_path: Path, fake_server: FakeRcon) -> Profile:
    pw = tmp_path / "pw"
    pw.write_text("secret\n")
    log = tmp_path / "console.log"
    log.write_text("[00:00:00 INFO]: Starting minecraft server\n[00:00:01 WARN]: something odd\n")
    return Profile(name="test", host="127.0.0.1", rcon_port=fake_server.port, password_file=str(pw), log=LogSpec(type="local", path=str(log)))
