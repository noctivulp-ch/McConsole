"""Autocompletado: comandos locales (':'), comandos del juego ('/' u omitido) y sus subcomandos.

El árbol del juego se obtiene del propio servidor con `minecraft:help [ruta]` (salida Brigadier:
`/execute (run|if|unless) …`, `/girls spawnCustom <id> [<pos>]`), así incluye vanilla, mods y plugins.
Los niveles profundos se piden bajo demanda y se cachean. Núcleo sin UI: la TUI solo llama a `complete()`.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Callable

LITERAL_RX = re.compile(r"^[A-Za-z0-9_:.\-]+$")
GROUP_RX = re.compile(r"^\((.+)\)$")

LOCAL_COMMANDS: dict[str, str] = {
    "help": "ayuda", "status": "estado del proceso y RCON", "start": "arrancar servidor", "stop": "parar servidor",
    "restart": "reiniciar servidor", "tail": "N — reimprimir últimas N líneas", "clear": "limpiar panel",
    "reconnect": "reconectar RCON", "filter": "TEXTO — filtrar log (vacío quita)", "tab": "NOMBRE — cambiar pestaña",
    "profile": "new | list | close — perfiles (pestañas)",
    "world": "list | info | types | create | use | rename | delete | backup — mundos del servidor",
    "q": "salir (no para el servidor)",
}


def _split_tokens(token: str) -> tuple[list[str], list[str]]:
    """'(grant|revoke)' → literales; '<targets>' / '[<pos>]' → argumentos (se muestran como pista, no se completan)."""
    m = GROUP_RX.match(token)
    parts = m.group(1).split("|") if m else [token]
    literals = [p for p in parts if LITERAL_RX.match(p)]
    args = [p for p in parts if not LITERAL_RX.match(p)]
    return literals, args


@dataclass
class Node:
    literals: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)      # pistas tipo '<targets>'
    loaded: bool = False


class GameCatalog:
    """Árbol de comandos del servidor, poblado con `minecraft:help` vía una función `run(cmd) -> str`."""

    def __init__(self, run: Callable[[str], str]):
        self.run = run
        self.nodes: dict[tuple[str, ...], Node] = {(): Node()}
        self.lock = threading.Lock()

    def _parse(self, path: tuple[str, ...], output: str) -> None:
        """Rellena el nodo `path` (y los que cuelgan) con las líneas '/a b (c|d) <e>' de la salida."""
        depth = len(path)
        for raw in output.splitlines():
            line = raw.strip()
            if not line.startswith("/"):
                continue
            tokens = line[1:].split(" -> ")[0].split()
            if tokens[:depth] != list(path) or len(tokens) <= depth:
                continue
            literals, args = _split_tokens(tokens[depth])
            node = self.nodes.setdefault(path, Node())
            for lit in literals:
                if lit not in node.literals:
                    node.literals.append(lit)
                # si la línea sigue, el siguiente token es hijo directo de (path + lit)
                if len(tokens) > depth + 1 and len(literals) == 1:
                    child = self.nodes.setdefault(path + (lit,), Node())
                    cl, ca = _split_tokens(tokens[depth + 1])
                    for c in cl:
                        if c not in child.literals:
                            child.literals.append(c)
                    for a in ca:
                        if a not in child.args:
                            child.args.append(a)
            for a in args:
                if a not in node.args:
                    node.args.append(a)
        self.nodes.setdefault(path, Node()).loaded = True

    def load_root(self) -> None:
        out = self.run("minecraft:help")
        if not any(l.startswith("/") for l in out.splitlines()):
            out = self.run("help")  # servidores sin namespace minecraft: (o sin Brigadier expuesto)
        with self.lock:
            self._parse((), out)

    def node(self, path: tuple[str, ...]) -> Node:
        """Nodo para una ruta de literales; carga bajo demanda (`minecraft:help a b`) si no se conoce."""
        with self.lock:
            n = self.nodes.get(path)
            if n is not None and n.loaded:
                return n
        if not path:
            return self.nodes[()]
        try:
            out = self.run("minecraft:help " + " ".join(path))
        except Exception:  # noqa: BLE001
            return self.nodes.get(path, Node())
        with self.lock:
            self._parse(path, out)
            return self.nodes.get(path, Node())

    @property
    def ready(self) -> bool:
        return self.nodes[()].loaded


@dataclass
class Completion:
    text: str                      # nuevo valor del input
    candidates: list[str]          # alternativas (para mostrar/ciclar), vacío si única o ninguna
    hint: str = ""                 # pista de argumentos, p. ej. '<targets> (only|from)'


class Completer:
    def __init__(self, catalog: GameCatalog | None, tab_names: Callable[[], list[str]]):
        self.catalog = catalog
        self.tab_names = tab_names
        self._cycle_prefix = ""
        self._cycle: list[str] = []
        self._cycle_i = -1
        self._cycle_last = None  # último texto devuelto por el ciclo; si el input sigue igual, avanzar

    def reset_cycle(self) -> None:
        self._cycle_prefix, self._cycle, self._cycle_i, self._cycle_last = "", [], -1, None

    def complete(self, text: str) -> Completion | None:
        if not text.strip() and not text.startswith(":"):
            return Completion(text, [], "escribe : para comandos locales, / (opcional) para el juego")
        # ciclo: si el input es exactamente lo que devolvimos la última vez, rotar al siguiente candidato
        if self._cycle and text == self._cycle_last:
            self._cycle_i = (self._cycle_i + 1) % len(self._cycle)
            self._cycle_last = self._cycle_prefix + self._cycle[self._cycle_i] + " "
            return Completion(self._cycle_last, self._cycle, f"{self._cycle_i + 1}/{len(self._cycle)}")

        ends_space = text.endswith(" ")
        tokens = text.split()
        if text.startswith(":"):
            return self._complete_local(text, tokens, ends_space)
        return self._complete_game(text, tokens, ends_space)

    # --- locales
    def _complete_local(self, text: str, tokens: list[str], ends_space: bool) -> Completion | None:
        head = tokens[0][1:] if tokens else ""
        if len(tokens) <= 1 and not ends_space:
            names = sorted(n for n in LOCAL_COMMANDS if n.startswith(head))
            return self._finish(":", head, names, lambda n: f"{n} — {LOCAL_COMMANDS[n]}")
        if head == "profile":
            partial = "" if ends_space else (tokens[1] if len(tokens) > 1 else "")
            names = [n for n in ("new", "list", "close") if n.startswith(partial)]
            return self._finish(":profile ", partial, names)
        if head == "tab":
            partial = "" if ends_space else (tokens[1] if len(tokens) > 1 else "")
            names = sorted(n for n in self.tab_names() if n.startswith(partial))
            return self._finish(":tab ", partial, names)
        return Completion(text, [], LOCAL_COMMANDS.get(head, ""))

    # --- juego
    def _complete_game(self, text: str, tokens: list[str], ends_space: bool) -> Completion | None:
        if self.catalog is None or not self.catalog.ready:
            return Completion(text, [], "catálogo de comandos aún no cargado (RCON desconectado?)")
        slash = text.startswith("/")
        words = [tokens[0][1:] if slash else tokens[0]] + tokens[1:] if tokens else []
        if ends_space:
            path, partial = tuple(words), ""
        else:
            path, partial = tuple(words[:-1]), (words[-1] if words else "")
        node = self.catalog.node(path)
        names = sorted(n for n in node.literals if n.startswith(partial))
        prefix = ("/" if slash else "") + " ".join(path) + (" " if path else "")
        hint = " ".join(node.args)
        if not names:
            return Completion(text, [], hint or ("sin subcomandos conocidos" if path else "sin coincidencias"))
        return self._finish(prefix, partial, names, hint=hint)

    # --- común
    def _finish(self, prefix: str, partial: str, names: list[str], label=None, hint: str = "") -> Completion:
        if not names:
            return Completion(prefix + partial, [], hint or "sin coincidencias")
        if len(names) == 1:
            self.reset_cycle()
            return Completion(prefix + names[0] + " ", [], hint)
        common = _common_prefix(names)
        if len(common) > len(partial):
            self.reset_cycle()
            return Completion(prefix + common, [label(n) if label else n for n in names], hint)
        # sin prefijo común que avanzar: empezar a ciclar
        self._cycle_prefix, self._cycle, self._cycle_i = prefix, names, 0
        self._cycle_last = prefix + names[0] + " "
        return Completion(self._cycle_last, [label(n) if label else n for n in names], f"1/{len(names)}" + (f" · {hint}" if hint else ""))


def _common_prefix(names: list[str]) -> str:
    if not names:
        return ""
    a, b = min(names), max(names)
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return a[:i]
