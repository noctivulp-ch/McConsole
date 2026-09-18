"""Gestión de mundos del servidor (carpeta local): listar, info, tipos disponibles, crear, activar, borrar, backup.

Todo se apoya en lo que el servidor ya entiende, sin inventar formatos:
- `server.properties`: level-name (mundo activo), level-type (preset), level-seed, generator-settings,
  generate-structures, hardcore, difficulty, gamemode, … (cualquier clave con --set k=v).
- `bukkit.yml` (Paper/Youer): `worlds.<nombre>.generator` para generadores de plugin (Terra:PACK).
- `level.dat` (NBT) para semilla, versión, generador real y tiempo de juego.
Los tipos de mundo se descubren **dinámicamente**: presets `data/*/worldgen/world_preset/*.json` en el jar del
servidor (incluido el jar anidado del bundler), mods/*.jar, datapacks del mundo, y packs de Terra en plugins/Terra/packs.
Solo carpetas locales (perfil con process.type=local o server_dir); SSH pendiente.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from .nbt import level_data, read_nbt

DIM_FOLDERS = ("region", "DIM-1", "DIM1", "dimensions", "entities", "poi", "playerdata", "data", "datapacks")
TRASH_DIR = "worlds-trash"
BACKUP_DIR = "backups"


# ---------------------------------------------------------------- server.properties / bukkit.yml
class Properties:
    """server.properties conservando comentarios y orden."""

    def __init__(self, path: Path):
        self.path = path
        self.lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def get(self, key: str, default: str = "") -> str:
        for l in self.lines:
            if l.startswith(key + "="):
                return l[len(key) + 1:].replace("\\:", ":").replace("\\=", "=")
        return default

    def set(self, key: str, value: str) -> None:
        enc = value.replace(":", "\\:")
        for i, l in enumerate(self.lines):
            if l.startswith(key + "="):
                self.lines[i] = f"{key}={enc}"
                return
        self.lines.append(f"{key}={enc}")

    def save(self) -> None:
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def bukkit_generator(server_dir: Path, world: str) -> str | None:
    """worlds.<world>.generator de bukkit.yml (sin PyYAML: parser línea a línea del bloque `worlds:`)."""
    p = server_dir / "bukkit.yml"
    if not p.exists():
        return None
    in_worlds, cur = False, None
    for l in p.read_text(encoding="utf-8").splitlines():
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        indent = len(l) - len(l.lstrip())
        if indent == 0:
            in_worlds = l.strip() == "worlds:"
            continue
        if not in_worlds:
            continue
        if indent == 2 and l.rstrip().endswith(":"):
            cur = l.strip()[:-1].strip("'\"")
        elif indent >= 4 and cur == world and l.strip().startswith("generator:"):
            return l.split(":", 1)[1].strip().strip("'\"")
    return None


def set_bukkit_generator(server_dir: Path, world: str, generator: str | None) -> None:
    """Añade/actualiza/quita worlds.<world>.generator en bukkit.yml (crea el bloque si falta)."""
    p = server_dir / "bukkit.yml"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    out, in_worlds, done, cur, skipping = [], False, False, None, False
    has_worlds = any(l.strip() == "worlds:" for l in lines)
    for l in lines:
        indent = len(l) - len(l.lstrip())
        if indent == 0 and l.strip():
            if in_worlds and not done and generator:
                out += [f"  {world}:", f"    generator: {generator}"]; done = True
            in_worlds = l.strip() == "worlds:"; skipping = False
            out.append(l); continue
        if in_worlds and indent == 2 and l.rstrip().endswith(":"):
            cur = l.strip()[:-1].strip("'\"")
            skipping = cur == world
            if skipping:
                if generator:
                    out += [f"  {world}:", f"    generator: {generator}"]
                done = True
                continue
        if in_worlds and skipping and indent >= 4:
            continue
        out.append(l)
    if generator and not done:
        if not has_worlds:
            out.append("worlds:")
        out += [f"  {world}:", f"    generator: {generator}"]
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- mundos
@dataclass
class WorldInfo:
    name: str
    path: Path
    active: bool = False
    size_bytes: int = 0
    modified: float = 0.0
    seed: int | None = None
    version: str = ""
    level_name: str = ""
    generator: str = ""            # tipo del generador del overworld según level.dat
    preset: str = ""               # level-type con el que se creó si se conoce
    plugin_generator: str | None = None  # bukkit.yml
    game_time_ticks: int = 0
    gamemode: str = ""
    hardcore: bool = False
    difficulty: str = ""
    dims: list[str] = field(default_factory=list)
    datapacks: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def size_h(self) -> str:
        return human(self.size_bytes)

    @property
    def played_h(self) -> str:
        h = self.game_time_ticks / 20 / 3600
        return f"{h:.1f} h" if h >= 1 else f"{self.game_time_ticks / 20 / 60:.0f} min"


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


GAMEMODES = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
DIFFICULTIES = {0: "peaceful", 1: "easy", 2: "normal", 3: "hard"}


def read_world(server_dir: Path, name: str, active: str, with_size: bool = True) -> WorldInfo:
    p = server_dir / name
    w = WorldInfo(name=name, path=p, active=name == active, plugin_generator=bukkit_generator(server_dir, name))
    try:
        w.modified = p.stat().st_mtime
    except OSError:
        pass
    if with_size:
        w.size_bytes = dir_size(p)
    w.dims = [d for d in DIM_FOLDERS[:4] if (p / d).exists()] + sorted(
        f"dimensions/{x.parent.name}/{x.name}" for x in (p / "dimensions").glob("*/*") if x.is_dir())
    w.datapacks = sorted(x.name for x in (p / "datapacks").iterdir()) if (p / "datapacks").exists() else []
    try:
        d = level_data(p / "level.dat")
    except Exception as e:  # noqa: BLE001
        w.error = f"level.dat ilegible: {e}"
        return w
    w.level_name = str(d.get("LevelName", ""))
    w.version = str((d.get("Version") or {}).get("Name", ""))
    w.game_time_ticks = int(d.get("Time", 0))
    w.gamemode = GAMEMODES.get(int(d.get("GameType", 0)), str(d.get("GameType")))
    ds = d.get("difficulty_settings") or {}          # 26.x
    w.hardcore = bool(d.get("hardcore", ds.get("hardcore", 0)))
    w.difficulty = str(ds.get("difficulty") or DIFFICULTIES.get(int(d.get("Difficulty", 2)), ""))
    wgs = d.get("WorldGenSettings") or {}
    if not wgs:  # 26.x: los ajustes de generación salieron de level.dat a world_gen_settings.dat
        for cand in (p / "data/minecraft/world_gen_settings.dat",
                     p / "dimensions/minecraft/overworld/data/minecraft/world_gen_settings.dat"):
            if cand.exists():
                try:
                    wgs = read_nbt(cand).get("data") or {}
                    break
                except Exception:  # noqa: BLE001
                    pass
    if "seed" in wgs:
        w.seed = int(wgs["seed"])
    elif "RandomSeed" in d:
        w.seed = int(d["RandomSeed"])
    ow = ((wgs.get("dimensions") or {}).get("minecraft:overworld") or {}).get("generator") or {}
    gtype = str(ow.get("type", ""))
    settings = ow.get("settings")
    bs = (ow.get("biome_source") or {})
    if gtype == "minecraft:noise":
        w.generator = f"noise/{settings if isinstance(settings, str) else 'custom'} · biomas {str(bs.get('type', '')).split(':')[-1]}"
        if isinstance(settings, str):
            w.preset = {"minecraft:overworld": "normal", "minecraft:amplified": "amplified", "minecraft:large_biomes": "large_biomes"}.get(settings, settings)
            if bs.get("type") == "minecraft:fixed":
                w.preset = f"single_biome ({bs.get('biome')})"
            elif settings == "minecraft:overworld" and bs.get("type") == "minecraft:multi_noise" and bs.get("preset") == "minecraft:overworld":
                w.preset = "normal"
    elif gtype:
        w.generator = gtype.split(":")[-1]
        w.preset = w.generator
    if isinstance(settings, dict) and gtype == "minecraft:flat":
        layers = settings.get("layers") or []
        w.generator = "flat: " + ", ".join(f"{l.get('height')}×{str(l.get('block', '')).split(':')[-1]}" for l in layers)
    return w


def list_worlds(server_dir: Path, with_size: bool = True) -> list[WorldInfo]:
    active = Properties(server_dir / "server.properties").get("level-name", "world")
    names = sorted(p.name for p in server_dir.iterdir() if p.is_dir() and (p / "level.dat").exists())
    return [read_world(server_dir, n, active, with_size) for n in names]


# ---------------------------------------------------------------- tipos de mundo (presets) — descubrimiento dinámico
@dataclass
class WorldType:
    id: str                 # minecraft:flat, terra:ORIGEN_SURVIVAL, …
    source: str             # jar/plugin/datapack de donde sale
    kind: str               # vanilla | mod | datapack | terra
    summary: str = ""       # generador del overworld
    how: str = ""           # cómo se aplica: level-type=… | bukkit.yml generator=…


def _preset_summary(obj: dict) -> str:
    ow = ((obj.get("dimensions") or {}).get("minecraft:overworld") or {}).get("generator") or {}
    t = str(ow.get("type", "?")).split(":")[-1]
    s = ow.get("settings")
    if isinstance(s, str):
        t += f" ({s.split(':')[-1]})"
    elif isinstance(s, dict) and t == "flat":
        t += " " + "/".join(str(l.get("block", "")).split(":")[-1] for l in s.get("layers", []))
    bs = ow.get("biome_source") or {}
    if bs.get("type") == "minecraft:fixed":
        t += f" biome={str(bs.get('biome', '')).split(':')[-1]}"
    return t


def _scan_zip_presets(zf: zipfile.ZipFile, source: str, kind: str, out: dict[str, WorldType]) -> None:
    rx = re.compile(r"^data/([^/]+)/worldgen/world_preset/([^/]+)\.json$")
    for name in zf.namelist():
        m = rx.match(name)
        if m:
            pid = f"{m.group(1)}:{m.group(2)}"
            try:
                summary = _preset_summary(json.loads(zf.read(name)))
            except Exception:  # noqa: BLE001
                summary = "?"
            out.setdefault(pid, WorldType(pid, source, kind, summary, f"level-type={pid}"))
        elif name.startswith("META-INF/versions/") and name.endswith(".jar"):  # bundler de Mojang: jar dentro del jar
            try:
                with zipfile.ZipFile(BytesIO(zf.read(name))) as inner:
                    _scan_zip_presets(inner, f"{source}!{Path(name).name}", kind, out)
            except zipfile.BadZipFile:
                pass


def discover_world_types(server_dir: Path, world: str | None = None) -> list[WorldType]:
    out: dict[str, WorldType] = {}
    jars: list[tuple[Path, str]] = []
    for p in sorted(server_dir.glob("*.jar")):
        jars.append((p, "vanilla"))
    for p in sorted(server_dir.glob("libraries/net/minecraft/server/*/server-*.jar")):
        jars.append((p, "vanilla"))
    for p in sorted(server_dir.glob("versions/*/*.jar")):
        jars.append((p, "vanilla"))
    for p in sorted(server_dir.glob("mods/*.jar")):
        jars.append((p, "mod"))
    for p in sorted(server_dir.glob("plugins/*.jar")):
        jars.append((p, "plugin"))
    dps = []
    for w in ([world] if world else [x.name for x in list_worlds(server_dir, with_size=False)]):
        dps += sorted((server_dir / w / "datapacks").glob("*")) if (server_dir / w / "datapacks").exists() else []
    for p, kind in jars:
        try:
            with zipfile.ZipFile(p) as zf:
                _scan_zip_presets(zf, p.name, kind, out)
        except (zipfile.BadZipFile, OSError):
            continue
    for dp in dps:
        try:
            if dp.is_dir():
                for f in dp.glob("data/*/worldgen/world_preset/*.json"):
                    pid = f"{f.parts[-4]}:{f.stem}"
                    out.setdefault(pid, WorldType(pid, dp.name, "datapack", _preset_summary(json.loads(f.read_text())), f"level-type={pid}"))
            else:
                with zipfile.ZipFile(dp) as zf:
                    _scan_zip_presets(zf, dp.name, "datapack", out)
        except Exception:  # noqa: BLE001
            continue
    # Terra (plugin Bukkit): packs = generadores "Terra:ID" vía bukkit.yml
    for packs in (server_dir / "plugins/Terra/packs", server_dir / "config/Terra/packs"):
        if not packs.exists():
            continue
        for p in sorted(packs.iterdir()):
            pack_yml = None
            try:
                if p.is_dir() and (p / "pack.yml").exists():
                    pack_yml = (p / "pack.yml").read_text(encoding="utf-8", errors="replace")
                elif p.suffix == ".zip":
                    with zipfile.ZipFile(p) as zf:
                        pack_yml = zf.read("pack.yml").decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pack_yml = None
            if not pack_yml:
                continue
            m = re.search(r"^id:\s*(\S+)", pack_yml, re.M)
            gen = re.search(r"^generator:\s*(\S+)", pack_yml, re.M)
            if m:
                tid = m.group(1).strip("'\"")
                out.setdefault(f"terra:{tid}", WorldType(f"terra:{tid}", p.name, "terra", f"Terra {gen.group(1) if gen else ''}".strip(), f"bukkit.yml worlds.<mundo>.generator=Terra:{tid}"))
    order = {"vanilla": 0, "mod": 1, "plugin": 1, "datapack": 2, "terra": 3}
    return sorted(out.values(), key=lambda t: (order.get(t.kind, 9), t.id))


# ---------------------------------------------------------------- acciones
class WorldError(Exception):
    pass


VALID_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _check_name(name: str) -> None:
    if not VALID_NAME.match(name) or name in (TRASH_DIR, BACKUP_DIR):
        raise WorldError(f"nombre de mundo no válido: {name!r} (letras, dígitos, . _ -)")


def create_world(server_dir: Path, name: str, *, world_type: str | None = None, seed: str | None = None,
                 generator_settings: str | None = None, structures: bool | None = None, hardcore: bool | None = None,
                 difficulty: str | None = None, gamemode: str | None = None, extra: dict[str, str] | None = None,
                 activate: bool = True) -> list[str]:
    """Prepara server.properties (+ bukkit.yml si el tipo es de Terra) para que el próximo arranque genere `name`.
    Devuelve las líneas de lo que se cambió. El servidor debe estar parado (lo comprueba quien llama)."""
    _check_name(name)
    if (server_dir / name).exists():
        raise WorldError(f"ya existe la carpeta {name}")
    props = Properties(server_dir / "server.properties")
    changed = []
    if activate:
        props.set("level-name", name); changed.append(f"level-name={name}")
    if world_type:
        if world_type.lower().startswith("terra:"):
            pack = world_type.split(":", 1)[1]
            set_bukkit_generator(server_dir, name, f"Terra:{pack}")
            changed.append(f"bukkit.yml worlds.{name}.generator=Terra:{pack}")
            props.set("level-type", "minecraft:normal"); changed.append("level-type=minecraft:normal (Terra sustituye el generador)")
        else:
            wt = world_type if ":" in world_type else f"minecraft:{world_type}"
            props.set("level-type", wt); changed.append(f"level-type={wt}")
    if seed is not None:
        props.set("level-seed", seed); changed.append(f"level-seed={seed or '(aleatoria)'}")
    if generator_settings is not None:
        props.set("generator-settings", generator_settings); changed.append(f"generator-settings={generator_settings}")
    if structures is not None:
        props.set("generate-structures", str(structures).lower()); changed.append(f"generate-structures={str(structures).lower()}")
    if hardcore is not None:
        props.set("hardcore", str(hardcore).lower()); changed.append(f"hardcore={str(hardcore).lower()}")
    if difficulty:
        props.set("difficulty", difficulty); changed.append(f"difficulty={difficulty}")
    if gamemode:
        props.set("gamemode", gamemode); changed.append(f"gamemode={gamemode}")
    for k, v in (extra or {}).items():
        props.set(k, v); changed.append(f"{k}={v}")
    props.save()
    return changed


def use_world(server_dir: Path, name: str) -> None:
    _check_name(name)
    if not (server_dir / name / "level.dat").exists():
        raise WorldError(f"no existe el mundo {name} (sin level.dat)")
    props = Properties(server_dir / "server.properties")
    props.set("level-name", name)
    props.save()


def delete_world(server_dir: Path, name: str, purge: bool = False) -> Path | None:
    """Mueve la carpeta a worlds-trash/<nombre>-<fecha> (o la borra con purge). No toca el mundo activo."""
    _check_name(name)
    p = server_dir / name
    if not p.exists():
        raise WorldError(f"no existe {name}")
    active = Properties(server_dir / "server.properties").get("level-name", "world")
    if name == active:
        raise WorldError(f"{name} es el mundo activo (level-name); activa otro con :world use antes de borrarlo")
    set_bukkit_generator(server_dir, name, None)
    if purge:
        shutil.rmtree(p)
        return None
    trash = server_dir / TRASH_DIR
    trash.mkdir(exist_ok=True)
    dst = trash / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(p), dst)
    return dst


def rename_world(server_dir: Path, old: str, new: str) -> None:
    _check_name(old); _check_name(new)
    if not (server_dir / old).exists():
        raise WorldError(f"no existe {old}")
    if (server_dir / new).exists():
        raise WorldError(f"ya existe {new}")
    (server_dir / old).rename(server_dir / new)
    props = Properties(server_dir / "server.properties")
    if props.get("level-name") == old:
        props.set("level-name", new); props.save()
    gen = bukkit_generator(server_dir, old)
    if gen:
        set_bukkit_generator(server_dir, old, None)
        set_bukkit_generator(server_dir, new, gen)


def backup_world(server_dir: Path, name: str) -> Path:
    _check_name(name)
    p = server_dir / name
    if not p.exists():
        raise WorldError(f"no existe {name}")
    out = server_dir / BACKUP_DIR
    out.mkdir(exist_ok=True)
    dst = out / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in p.rglob("*"):
            if f.is_file() and f.name != "session.lock":
                zf.write(f, f.relative_to(server_dir))
    return dst


# ---------------------------------------------------------------- texto (compartido por CLI y TUI)
def fmt_list(worlds: list[WorldInfo]) -> list[str]:
    if not worlds:
        return ["sin mundos (carpetas con level.dat)"]
    lines = []
    for w in worlds:
        mark = "*" if w.active else " "
        gen = w.plugin_generator or w.preset or w.generator or "?"
        lines.append(f"{mark} {w.name:22s} {w.size_h:>9s}  {w.version:8s} {gen:28s} seed={w.seed if w.seed is not None else '?'}  {w.played_h:>7s}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(w.modified))}"
                     + (f"  [{w.error}]" if w.error else ""))
    lines.append("(* = level-name activo)")
    return lines


def fmt_info(w: WorldInfo) -> list[str]:
    return [
        f"{w.name}{' (activo)' if w.active else ''} — {w.path}",
        f"  versión {w.version or '?'} · nombre interno {w.level_name!r} · {w.size_h} · modificado {time.strftime('%Y-%m-%d %H:%M', time.localtime(w.modified))}",
        f"  semilla {w.seed} · generador {w.generator or '?'} · preset {w.preset or '?'}" + (f" · plugin {w.plugin_generator}" if w.plugin_generator else ""),
        f"  jugado {w.played_h} · {w.gamemode} · dificultad {w.difficulty}{' · hardcore' if w.hardcore else ''}",
        f"  dimensiones: {', '.join(w.dims) or '-'}",
        f"  datapacks: {', '.join(w.datapacks) or '-'}",
    ] + ([f"  [{w.error}]"] if w.error else [])


def fmt_types(types: list[WorldType]) -> list[str]:
    if not types:
        return ["no se encontraron presets (¿server_dir correcto?)"]
    lines = []
    for t in types:
        lines.append(f"{t.kind:8s} {t.id:34s} {t.summary:38s} ← {t.source}")
    lines.append("uso: :world create <nombre> --type <id> [--seed N] [--flat …]  (terra:* se aplica vía bukkit.yml)")
    return lines


HELP = """:world list                         mundos del servidor (carpetas con level.dat); * = activo
:world info <nombre>                detalles de level.dat, dimensiones, datapacks
:world types                        tipos/presets disponibles (jar del servidor, mods, datapacks, packs de Terra)
:world create <nombre> [--type ID] [--seed N] [--gen-settings JSON] [--no-structures] [--hardcore]
                       [--difficulty peaceful|easy|normal|hard] [--gamemode survival|…] [--set clave=valor]…
                       [--no-activate] [--start]      prepara server.properties/bukkit.yml; el mundo se genera al arrancar
:world use <nombre> [--start]       cambiar level-name (servidor parado)
:world rename <viejo> <nuevo>
:world delete <nombre> [--purge]    mueve a worlds-trash/ (o borra) — nunca el activo
:world backup <nombre>              zip en backups/ (si el servidor corre, hace save-all antes)"""
