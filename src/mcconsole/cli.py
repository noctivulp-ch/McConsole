"""mc-rcon — modo máquina: comandos RCON limpios para scripts y agentes, más control del proceso.

  mc-rcon [-p PERFIL] "cmd1" ["cmd2" …]      varios comandos por una conexión; salida sin colores
  echo "cmd" | mc-rcon [-p PERFIL] -          comandos por stdin (uno por línea, # comenta)
  mc-rcon [-p PERFIL] --log-wait 2 "cmd"      + líneas NUEVAS del log durante 2 s (salida diferida de plugins)
  mc-rcon [-p PERFIL] --json "cmd"            {"cmd","response","log"} por línea
  mc-rcon [-p PERFIL] :status|:start|:stop|:restart
  mc-rcon [-p PERFIL] :world list|info|types|create|use|rename|delete|backup …   mundos (":world help")
  mc-rcon [-p PERFIL] -t                      abre la TUI (mc-console)
  mc-rcon --profiles                          lista perfiles

Códigos de salida: 0 ok · 2 contraseña rechazada · 3 sin conexión / proceso · 4 uso.
Sin perfil: variables RCON_HOST/RCON_PORT/RCON_PW_FILE/RCON_LOG/SERVER_START/SERVER_PID, o el único perfil definido.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .process import make_process
from .profiles import Profile, ProfileError, config_path, load_profiles, resolve
from .rcon import Rcon, RconAuthError
from .sources import make_source
from .text import mc_to_ansi, strip_ansi, strip_mc


def _new_log_lines(source, since: int) -> tuple[list[str], int]:
    """Solo para fuentes locales: líneas nuevas desde un tamaño de fichero."""
    p = getattr(source, "path", None)
    if not isinstance(p, Path):  # solo ficheros locales (las fuentes ssh/sftp tienen path remoto en str)
        return [], since
    try:
        size = p.stat().st_size
    except OSError:
        return [], since
    if size < since:
        since = 0
    if size == since:
        return [], size
    with p.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(since)
        return f.read().splitlines(), size


def process_command(op: str, prof: Profile, timeout: float) -> int:
    proc = make_process(prof.process)
    if proc is None:
        print("el perfil no define [process] (pidfile/start)", file=sys.stderr)
        return 4
    if op == ":status":
        pid = proc.pid()
        print(f"{prof.name}: {'corriendo, pid ' + str(pid) if pid else 'parado'}")
        return 0 if pid else 3
    if op in (":stop", ":restart"):
        if proc.pid() is None:
            print("ya estaba parado")
        else:
            rcon = Rcon(prof.host, prof.rcon_port, prof.password(), timeout=timeout)
            how = proc.stop(rcon)
            print({"rcon": "stop enviado por RCON", "sigterm": "RCON no respondió; SIGTERM", "none": "sin proceso"}[how])
            if not proc.wait_stopped():
                print("el proceso sigue vivo tras 90 s", file=sys.stderr)
                return 3
            print("servidor parado")
        if op == ":stop":
            return 0
    if op in (":start", ":restart"):
        if not proc.can_start:
            print("el perfil no define process.start", file=sys.stderr)
            return 4
        if proc.pid() is not None:
            print("ya corre")
            return 0
        proc.start()
        source = make_source(prof.log)
        pos = getattr(getattr(source, "path", None), "stat", lambda: None)()
        pos = pos.st_size if pos else 0
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            time.sleep(0.5)
            lines, pos = _new_log_lines(source, pos) if source else ([], pos)
            for l in lines:
                if "Done (" in l:
                    print("servidor listo:", strip_ansi(l).strip())
                    return 0
                if "SERVER_EXIT=" in l or "Mod loading has failed" in l:
                    print("el servidor murió al arrancar:", strip_ansi(l).strip(), file=sys.stderr)
                    return 3
            if source is None or not isinstance(getattr(source, "path", None), Path):
                # sin log local: comprobar solo el pid unos segundos
                if time.monotonic() > deadline - 290:
                    print("arranque lanzado" if proc.pid() else "no se ve el proceso", file=sys.stderr)
                    return 0 if proc.pid() else 3
        print("timeout esperando el arranque (mira el log)", file=sys.stderr)
        return 3
    return 4


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mc-rcon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("commands", nargs="*", help='comandos; "-" = stdin; ":status/:start/:stop/:restart" = proceso')
    ap.add_argument("-p", "--profile", help="perfil de ~/.config/mc-console/servers.toml")
    ap.add_argument("-t", "--tty", action="store_true", help="abrir la TUI (mc-console)")
    ap.add_argument("--profiles", action="store_true", help="listar perfiles y salir")
    ap.add_argument("--log-wait", type=float, default=0.0, metavar="S", help="tras cada comando, líneas nuevas del log durante S s (solo log local)")
    ap.add_argument("--color", action="store_true", help="conservar colores")
    ap.add_argument("--header", action="store_true", help="prefijar cada respuesta con '# cmd'")
    ap.add_argument("--json", action="store_true", help="salida JSON por comando")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--version", action="version", version=f"mc-rcon {__version__}")
    args = ap.parse_args(argv)

    if args.profiles:
        profiles = load_profiles()
        if not profiles:
            print(f"sin perfiles; crea {config_path()} (mc-console --init escribe un ejemplo)")
        for p in profiles.values():
            print(f"{p.name:16s} {p.label:22s} log={p.log.type}:{p.log.path or '-'} process={'sí' if p.process else 'no'}")
        return 0

    try:
        prof = resolve(args.profile)
    except ProfileError as e:
        print(f"mc-rcon: {e}", file=sys.stderr)
        return 4

    if args.tty:
        from .app import run_app
        others = [p for p in load_profiles().values() if p.name != prof.name]
        return run_app(prof, *others, active=prof.name)

    if args.commands and args.commands[0] == ":world":
        from .worldcmd import run as world_run
        proc = make_process(prof.process)
        running = bool(proc and proc.pid())

        def _start() -> list[str]:
            import contextlib, io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                process_command(":start", prof, args.timeout)
            return buf.getvalue().splitlines()

        rc, lines = world_run(" ".join(args.commands[1:]), prof, running,
                              rcon_factory=lambda: Rcon(prof.host, prof.rcon_port, prof.password(), timeout=args.timeout),
                              start=_start)
        print("\n".join(lines))
        return rc
    if args.commands and args.commands[0].startswith(":"):
        return process_command(args.commands[0], prof, args.timeout)

    commands = args.commands
    if commands == ["-"] or (not commands and not sys.stdin.isatty()):
        commands = [l.strip() for l in sys.stdin if l.strip() and not l.lstrip().startswith("#")]
    if not commands:
        print("mc-rcon: nada que enviar (pasa comandos o '-' para stdin)", file=sys.stderr)
        return 4
    commands = [c[1:] if c.startswith("/") else c for c in commands]

    source = make_source(prof.log) if args.log_wait else None
    if args.log_wait and (source is None or not isinstance(getattr(source, "path", None), Path)):
        print("mc-rcon: --log-wait requiere un log local en el perfil", file=sys.stderr)
        return 4

    try:
        rcon = Rcon(prof.host, prof.rcon_port, prof.password(), timeout=args.timeout)
        rcon.connect()
    except RconAuthError as e:
        print(f"mc-rcon: {e}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"mc-rcon: sin conexión a {prof.label} ({e}). ¿Servidor arriba?", file=sys.stderr)
        return 3

    rc = 0
    try:
        for cmd in commands:
            pos = source.path.stat().st_size if (source and source.path.exists()) else 0  # type: ignore[attr-defined]
            try:
                resp = rcon.command(cmd)
            except OSError as e:
                print(f"mc-rcon: conexión perdida en '{cmd}' ({e})", file=sys.stderr)
                rc = 3
                break
            resp_lines = [mc_to_ansi(l) if args.color else strip_mc(l) for l in resp.strip().splitlines()]
            log_lines: list[str] = []
            if source is not None:
                deadline = time.monotonic() + args.log_wait
                while True:
                    lines, pos = _new_log_lines(source, pos)
                    log_lines += lines
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.15)
                log_lines = [l for l in log_lines if "Thread RCON Client" not in l]
                if not args.color:
                    log_lines = [strip_ansi(l) for l in log_lines]
            if args.json:
                print(json.dumps({"cmd": cmd, "response": strip_ansi("\n".join(resp_lines)), "log": [strip_ansi(l) for l in log_lines]}, ensure_ascii=False))
                continue
            if args.header or len(commands) > 1:
                print(f"# {cmd}")
            for l in resp_lines:
                print(l)
            if source is not None:
                if log_lines:
                    print("-- log --")
                    for l in log_lines:
                        print(l)
                elif not resp_lines:
                    print("(sin respuesta ni líneas nuevas en el log)")
            sys.stdout.flush()
    finally:
        rcon.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
