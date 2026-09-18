"""Texto: códigos § de Minecraft, ANSI y tinte por nivel de log. Sin dependencias de UI salvo rich.Text."""
from __future__ import annotations

import re

from rich.text import Text

MC_COLORS = {
    "0": "30", "1": "34", "2": "32", "3": "36", "4": "31", "5": "35", "6": "33", "7": "37",
    "8": "90", "9": "94", "a": "92", "b": "96", "c": "91", "d": "95", "e": "93", "f": "97",
    "l": "1", "n": "4", "o": "3", "m": "9", "k": "5", "r": "0",
}
MC_CODE = re.compile("§([0-9a-fk-or])", re.IGNORECASE)
ANSI_RX = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LEVEL_RX = re.compile(r"\b(ERROR|FATAL|SEVERE|WARN(?:ING)?)\b")


def mc_to_ansi(text: str) -> str:
    return MC_CODE.sub(lambda m: f"\x1b[{MC_COLORS.get(m.group(1).lower(), '0')}m", text) + "\x1b[0m"


def strip_ansi(text: str) -> str:
    return ANSI_RX.sub("", text)


def strip_mc(text: str) -> str:
    return MC_CODE.sub("", text)


def level_of(line: str) -> str | None:
    """'error' | 'warn' | None según el texto (sin colores)."""
    m = LEVEL_RX.search(strip_ansi(line))
    if not m:
        return None
    return "warn" if m.group(1).upper().startswith("WARN") else "error"


def log_line_text(line: str) -> Text:
    """Convierte una línea de log (posiblemente con ANSI del servidor) en rich.Text, tintando WARN/ERROR."""
    if "\x1b[" in line:
        return Text.from_ansi(line)
    lvl = level_of(line)
    if lvl == "error":
        return Text(line, style="bold red")
    if lvl == "warn":
        return Text(line, style="yellow")
    return Text(line)


def response_text(resp: str) -> Text:
    """Respuesta RCON (con § de Minecraft) → rich.Text."""
    return Text.from_ansi(mc_to_ansi(resp))
