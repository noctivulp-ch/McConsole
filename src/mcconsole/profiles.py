"""Perfiles de servidor: ~/.config/mc-console/servers.toml (XDG). Secretos por referencia a fichero, nunca inline.

Formato:

    [servers.survival]
    host = "127.0.0.1"          # host RCON (local o remoto)
    rcon_port = 25575
    password_file = "~/.config/mc-console/survival.pw"   # o password_env = "MC_RCON_PW"
    [servers.survival.log]
    type = "local"              # "local" | "ssh"
    path = "/ruta/console.log"
    # ssh_host = "user@vps"     # solo type = "ssh" (tail -F remoto)
    [servers.survival.process]      # opcional: control del proceso (:start/:stop)
    type = "local"              # "local" | "ssh"
    start = "/ruta/start.sh"    # script desacoplado que escribe el pidfile
    pidfile = "/ruta/server.pid"
    # server_dir = "/ruta"       # carpeta con server.properties y los mundos (:world); por defecto la del start
    # ssh_host = "user@vps"
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "mc-console"


def config_path() -> Path:
    return config_dir() / "servers.toml"


@dataclass
class LogSpec:
    type: str = "local"           # local | ssh | sftp
    path: str = ""
    ssh_host: str = ""
    sftp_host: str = ""           # solo type=sftp (paneles Pterodactyl: SFTP sin shell)
    sftp_port: int = 2022
    sftp_user: str = ""
    sftp_password_file: str = ""  # o sftp_key (ruta a clave privada)
    sftp_key: str = ""


@dataclass
class ProcessSpec:
    type: str = "local"           # local | ssh
    start: str = ""
    pidfile: str = ""
    ssh_host: str = ""
    server_dir: str = ""          # carpeta del servidor (server.properties, mundos); por defecto la del script start

    def server_path(self):
        from pathlib import Path as _P
        if self.server_dir:
            return _P(self.server_dir).expanduser()
        if self.start:
            return _P(self.start).expanduser().resolve().parent
        return None


@dataclass
class Profile:
    name: str
    host: str = "127.0.0.1"
    rcon_port: int = 25575
    password_file: str = ""
    password_env: str = ""
    log: LogSpec = field(default_factory=LogSpec)
    process: ProcessSpec | None = None

    def password(self) -> str:
        if self.password_env and os.environ.get(self.password_env):
            return os.environ[self.password_env]
        if self.password_file:
            return Path(self.password_file).expanduser().read_text().strip()
        raise ValueError(f"perfil '{self.name}': falta password_file o password_env")

    @property
    def label(self) -> str:
        return f"{self.host}:{self.rcon_port}"


class ProfileError(Exception):
    pass


def _expand(s: str) -> str:
    return os.path.expandvars(os.path.expanduser(s)) if s else s


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    path = path or config_path()
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Profile] = {}
    for name, d in (data.get("servers") or {}).items():
        log = d.get("log") or {}
        proc = d.get("process")
        out[name] = Profile(
            name=name,
            host=d.get("host", "127.0.0.1"),
            rcon_port=int(d.get("rcon_port", 25575)),
            password_file=_expand(d.get("password_file", "")),
            password_env=d.get("password_env", ""),
            log=LogSpec(type=log.get("type", "local"), path=_expand(log.get("path", "")), ssh_host=log.get("ssh_host", ""),
                        sftp_host=log.get("sftp_host", ""), sftp_port=int(log.get("sftp_port", 2022)), sftp_user=log.get("sftp_user", ""),
                        sftp_password_file=_expand(log.get("sftp_password_file", "")), sftp_key=_expand(log.get("sftp_key", ""))),
            process=None if not proc else ProcessSpec(
                type=proc.get("type", "local"), start=_expand(proc.get("start", "")),
                pidfile=_expand(proc.get("pidfile", "")), ssh_host=proc.get("ssh_host", ""),
                server_dir=_expand(proc.get("server_dir", ""))),
        )
    return out


def profile_from_env(name: str = "server") -> Profile | None:
    """Compatibilidad con los wrappers antiguos (RCON_HOST/RCON_PORT/RCON_PW_FILE/RCON_LOG/SERVER_START/SERVER_PID)."""
    pw = os.environ.get("RCON_PW_FILE")
    if not pw:
        return None
    proc = None
    if os.environ.get("SERVER_START") or os.environ.get("SERVER_PID"):
        proc = ProcessSpec(type="local", start=_expand(os.environ.get("SERVER_START", "")), pidfile=_expand(os.environ.get("SERVER_PID", "")))
    return Profile(
        name=os.environ.get("RCON_NAME", name),
        host=os.environ.get("RCON_HOST", "127.0.0.1"),
        rcon_port=int(os.environ.get("RCON_PORT", "25575")),
        password_file=_expand(pw),
        log=LogSpec(type="local", path=_expand(os.environ.get("RCON_LOG", ""))),
        process=proc,
    )


def resolve(name: str | None) -> Profile:
    """Perfil por nombre; si no hay nombre: variables de entorno; si solo hay un perfil, ese."""
    profiles = load_profiles()
    if name:
        if name in profiles:
            return profiles[name]
        raise ProfileError(f"perfil '{name}' no existe en {config_path()} (disponibles: {', '.join(profiles) or 'ninguno'})")
    env = profile_from_env()
    if env:
        return env
    if len(profiles) == 1:
        return next(iter(profiles.values()))
    raise ProfileError(f"indica un perfil ({', '.join(profiles) or 'ninguno definido'}) — ver `mc-console --help` y {config_path()}")


def _toml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def append_profile(p: Profile, path: Path | None = None) -> Path:
    """Añade un perfil al servers.toml (escritura simple: la estructura es plana y conocida)."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if p.name in load_profiles(path):
        raise ProfileError(f"ya existe un perfil '{p.name}'")
    out = [f"\n[servers.{p.name}]", f"host = {_toml_str(p.host)}", f"rcon_port = {p.rcon_port}"]
    if p.password_file:
        out.append(f"password_file = {_toml_str(p.password_file)}")
    if p.password_env:
        out.append(f"password_env = {_toml_str(p.password_env)}")
    if p.log.path:
        out += [f"[servers.{p.name}.log]", f"type = {_toml_str(p.log.type)}", f"path = {_toml_str(p.log.path)}"]
        if p.log.type == "ssh":
            out.append(f"ssh_host = {_toml_str(p.log.ssh_host)}")
    if p.process and (p.process.start or p.process.pidfile):
        out += [f"[servers.{p.name}.process]", f"type = {_toml_str(p.process.type)}"]
        if p.process.start:
            out.append(f"start = {_toml_str(p.process.start)}")
        if p.process.pidfile:
            out.append(f"pidfile = {_toml_str(p.process.pidfile)}")
        if p.process.type == "ssh":
            out.append(f"ssh_host = {_toml_str(p.process.ssh_host)}")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing.rstrip("\n") + "\n" + "\n".join(out) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def write_example(path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(__doc__.split("Formato:", 1)[1].replace("\n    ", "\n").strip() + "\n", encoding="utf-8")
    return path
