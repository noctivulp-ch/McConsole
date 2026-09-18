"""Lector NBT mínimo (level.dat): gzip + árbol de tags a dict/list de Python. Solo lectura, sin dependencias."""
from __future__ import annotations

import gzip
import struct
from pathlib import Path

_END, _BYTE, _SHORT, _INT, _LONG, _FLOAT, _DOUBLE, _BYTE_ARRAY, _STRING, _LIST, _COMPOUND, _INT_ARRAY, _LONG_ARRAY = range(13)


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, fmt: str):
        size = struct.calcsize(fmt)
        v = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return v[0] if len(v) == 1 else v

    def string(self) -> str:
        n = self.take(">H")
        s = self.data[self.pos:self.pos + n]
        self.pos += n
        return s.decode("utf-8", "replace")

    def payload(self, tag: int):
        if tag == _BYTE: return self.take(">b")
        if tag == _SHORT: return self.take(">h")
        if tag == _INT: return self.take(">i")
        if tag == _LONG: return self.take(">q")
        if tag == _FLOAT: return self.take(">f")
        if tag == _DOUBLE: return self.take(">d")
        if tag == _BYTE_ARRAY:
            n = self.take(">i"); v = self.data[self.pos:self.pos + n]; self.pos += n; return bytes(v)
        if tag == _STRING: return self.string()
        if tag == _LIST:
            sub = self.take(">b"); n = self.take(">i")
            return [self.payload(sub) for _ in range(n)]
        if tag == _COMPOUND:
            out = {}
            while True:
                t = self.take(">b")
                if t == _END:
                    return out
                name = self.string()
                out[name] = self.payload(t)
        if tag == _INT_ARRAY:
            n = self.take(">i"); v = list(struct.unpack_from(f">{n}i", self.data, self.pos)); self.pos += 4 * n; return v
        if tag == _LONG_ARRAY:
            n = self.take(">i"); v = list(struct.unpack_from(f">{n}q", self.data, self.pos)); self.pos += 8 * n; return v
        raise ValueError(f"tag NBT desconocido {tag}")


def read_nbt(path: Path) -> dict:
    """Devuelve el compound raíz (level.dat → {'Data': {...}})."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    r = _Reader(raw)
    tag = r.take(">b")
    if tag != _COMPOUND:
        raise ValueError("el archivo no empieza por un compound")
    name = r.string()
    return {name: r.payload(_COMPOUND)} if name else r.payload(_COMPOUND)


def level_data(path: Path) -> dict:
    root = read_nbt(path)
    return root.get("Data", root.get("", root))
