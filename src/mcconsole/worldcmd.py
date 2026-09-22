"""`:world …` — misma lógica para el CLI (mc-rcon) y la TUI. Devuelve (código, líneas); las acciones destructivas
las confirma quien llama (`needs_confirm`)."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable

from . import worlds
from .profiles import Profile
from .rcon import Rcon

DESTRUCTIVE = {"delete", "rename", "use", "create"}


def server_dir(prof: Profile) -> Path | None:
    if prof.process is None or prof.process.type != "local":
        return None
    return prof.process.server_path()


def address_info(prof: Profile) -> list[str]:
    """Dirección de conexión del servidor, tal y como se escribe en Minecraft (Multijugador → Añadir servidor).

    Un servidor local se conecta con `localhost:<puerto>` (Minecraft acepta ese formato; con el puerto por defecto,
    25565, basta `localhost`). Los datos salen de `server.properties` cuando el perfil sabe dónde está el servidor.
    """
    import socket

    out: list[str] = []
    sd = server_dir(prof)
    props = worlds.Properties(sd / "server.properties") if sd else None
    port = (props.get("server-port") if props else "") or (str(prof.game_port) if prof.game_port else "") or "25565"
    bind = (props.get("server-ip") if props else "") or ""
    remoto = prof.host not in ("127.0.0.1", "localhost", "::1", "")
    sufijo = "" if port == "25565" else f":{port}"

    if remoto:
        out.append(f"remoto:     {prof.address or prof.host}{sufijo}")
        if prof.address and prof.address != prof.host:
            out.append(f"            (IP {prof.host}{sufijo})")
        out.append(f"RCON:       {prof.host}:{prof.rcon_port}")
    else:
        out.append(f"este PC:    localhost{sufijo}    (igual que 127.0.0.1{sufijo})")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("10.255.255.255", 1))
            out.append(f"en la LAN:  {sock.getsockname()[0]}{sufijo}")
            sock.close()
        except Exception:
            pass
        out.append(f"RCON:       127.0.0.1:{prof.rcon_port}")
    if props:
        extras = [f"{k}={props.get(k)}" for k in ("online-mode", "level-name", "max-players", "difficulty") if props.get(k)]
        if bind:
            extras.append(f"server-ip={bind}")
        if extras:
            out.append("  " + " · ".join(extras))
        if props.get("motd"):
            out.append(f"  motd: {props.get('motd')}")
    elif not remoto:
        out.append("  (sin server.properties: define process.server_dir o process.start en el perfil)")
    return out


def parse(text: str) -> tuple[str, list[str], dict[str, str | bool | list[str]]]:
    """':world create foo --type flat --seed 3 --set x=y' → ('create', ['foo'], {'type': 'flat', 'seed': '3', 'set': ['x=y']})."""
    toks = shlex.split(text)
    sub = toks[0] if toks else "help"
    pos: list[str] = []
    opts: dict[str, str | bool | list[str]] = {}
    i = 1
    while i < len(toks):
        t = toks[i]
        if t.startswith("--"):
            k = t[2:]
            if k in ("no-structures", "hardcore", "no-activate", "start", "purge", "no-size"):
                opts[k] = True; i += 1; continue
            if i + 1 >= len(toks):
                raise worlds.WorldError(f"--{k} necesita valor")
            v = toks[i + 1]
            if k == "set":
                opts.setdefault("set", []).append(v)  # type: ignore[union-attr]
            else:
                opts[k] = v
            i += 2
        else:
            pos.append(t); i += 1
    return sub, pos, opts


def needs_confirm(text: str) -> str | None:
    """Mensaje de confirmación para subcomandos que cambian cosas, o None."""
    try:
        sub, pos, opts = parse(text)
    except (worlds.WorldError, ValueError):
        return None
    if sub == "delete" and pos:
        return f"¿{'BORRAR definitivamente' if opts.get('purge') else 'Mover a worlds-trash'} el mundo {pos[0]}?"
    if sub == "create" and pos:
        return f"¿Crear el mundo {pos[0]} (server.properties{' + arrancar' if opts.get('start') else ''})?"
    if sub == "use" and pos:
        return f"¿Activar el mundo {pos[0]} (level-name)?"
    if sub == "rename" and len(pos) == 2:
        return f"¿Renombrar {pos[0]} → {pos[1]}?"
    return None


def run(text: str, prof: Profile, running: bool, rcon_factory: Callable[[], Rcon] | None = None,
        start: Callable[[], list[str]] | None = None) -> tuple[int, list[str]]:
    """`running`: si el proceso del servidor está vivo (create/use/rename/delete-activo exigen parado).
    `start`: callback que arranca el servidor y devuelve líneas de resultado (para --start)."""
    try:
        sub, pos, opts = parse(text)
    except (worlds.WorldError, ValueError) as e:
        return 4, [f"error: {e}"]
    sd = server_dir(prof)
    if sub in ("help", "", "?"):
        return 0, worlds.HELP.splitlines()
    if sd is None or not sd.exists():
        return 4, [f"el perfil {prof.name} no tiene carpeta de servidor local ([process] type=local con start o server_dir)"]
    try:
        if sub in ("list", "ls"):
            return 0, worlds.fmt_list(worlds.list_worlds(sd, with_size=not opts.get("no-size")))
        if sub == "info":
            if not pos:
                return 4, ["uso: :world info <nombre>"]
            active = worlds.Properties(sd / "server.properties").get("level-name", "world")
            if not (sd / pos[0] / "level.dat").exists():
                return 3, [f"no existe {pos[0]}"]
            return 0, worlds.fmt_info(worlds.read_world(sd, pos[0], active))
        if sub == "types":
            return 0, worlds.fmt_types(worlds.discover_world_types(sd))
        if sub == "create":
            if not pos:
                return 4, ["uso: :world create <nombre> [--type ID] [--seed N] …"]
            if running:
                return 3, ["el servidor está corriendo: páralo (:stop) antes de crear un mundo"]
            extra = {}
            for kv in opts.get("set", []):  # type: ignore[union-attr]
                k, _, v = str(kv).partition("=")
                extra[k] = v
            changed = worlds.create_world(
                sd, pos[0], world_type=opts.get("type"), seed=opts.get("seed"),  # type: ignore[arg-type]
                generator_settings=opts.get("gen-settings"),  # type: ignore[arg-type]
                structures=False if opts.get("no-structures") else None,
                hardcore=True if opts.get("hardcore") else None,
                difficulty=opts.get("difficulty"), gamemode=opts.get("gamemode"),  # type: ignore[arg-type]
                extra=extra, activate=not opts.get("no-activate"))
            lines = [f"mundo {pos[0]} preparado:"] + [f"  {c}" for c in changed]
            if opts.get("no-activate"):
                lines.append("  (no activado: :world use " + pos[0] + " cuando quieras)")
            if opts.get("start") and start:
                lines.append("arrancando…"); lines += start()
            else:
                lines.append("se generará en el próximo arranque (:start)")
            return 0, lines
        if sub == "use":
            if not pos:
                return 4, ["uso: :world use <nombre>"]
            if running:
                return 3, ["el servidor está corriendo: páralo (:stop) antes de cambiar de mundo"]
            worlds.use_world(sd, pos[0])
            lines = [f"level-name={pos[0]}"]
            if opts.get("start") and start:
                lines += start()
            return 0, lines
        if sub == "rename":
            if len(pos) != 2:
                return 4, ["uso: :world rename <viejo> <nuevo>"]
            if running:
                return 3, ["el servidor está corriendo: páralo antes de renombrar"]
            worlds.rename_world(sd, pos[0], pos[1])
            return 0, [f"{pos[0]} → {pos[1]}"]
        if sub in ("delete", "rm"):
            if not pos:
                return 4, ["uso: :world delete <nombre> [--purge]"]
            dst = worlds.delete_world(sd, pos[0], purge=bool(opts.get("purge")))
            return 0, [f"{pos[0]} borrado" if dst is None else f"{pos[0]} movido a {dst.relative_to(sd)}"]
        if sub == "backup":
            if not pos:
                return 4, ["uso: :world backup <nombre>"]
            notes = []
            if running and rcon_factory:
                try:
                    r = rcon_factory(); r.connect()
                    r.command("save-off"); r.command("save-all flush"); notes.append("save-all hecho (guardado desactivado durante el zip)")
                except Exception as e:  # noqa: BLE001
                    notes.append(f"aviso: no se pudo forzar save-all por RCON ({e}); el zip puede ir desfasado")
                    r = None
                try:
                    dst = worlds.backup_world(sd, pos[0])
                finally:
                    if r is not None:
                        try: r.command("save-on")
                        finally: r.close()
            else:
                dst = worlds.backup_world(sd, pos[0])
            return 0, notes + [f"backup: {dst.relative_to(sd)} ({worlds.human(dst.stat().st_size)})"]
        return 4, [f"subcomando desconocido: {sub}"] + worlds.HELP.splitlines()
    except worlds.WorldError as e:
        return 3, [f"error: {e}"]
    except OSError as e:
        return 3, [f"error de E/S: {e}"]
