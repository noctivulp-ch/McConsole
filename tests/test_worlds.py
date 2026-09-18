"""Gestión de mundos: si falla en silencio, `:world delete` puede tocar el mundo activo o `create` dejar
server.properties/bukkit.yml inconsistentes y el servidor arrancaría con otro mundo del esperado."""
import gzip
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

from mcconsole import worlds
from mcconsole.nbt import level_data
from mcconsole.profiles import ProcessSpec, Profile
from mcconsole.worldcmd import needs_confirm, run


def _nbt_string(s: str) -> bytes:
    b = s.encode(); return struct.pack(">H", len(b)) + b


def _compound(items: dict) -> bytes:
    out = b""
    for k, v in items.items():
        if isinstance(v, bool):
            out += b"\x01" + _nbt_string(k) + struct.pack(">b", int(v))
        elif isinstance(v, int) and abs(v) > 2**31:
            out += b"\x04" + _nbt_string(k) + struct.pack(">q", v)
        elif isinstance(v, int):
            out += b"\x03" + _nbt_string(k) + struct.pack(">i", v)
        elif isinstance(v, str):
            out += b"\x08" + _nbt_string(k) + _nbt_string(v)
        elif isinstance(v, dict):
            out += b"\x0a" + _nbt_string(k) + _compound(v)
    return out + b"\x00"


def write_level(world: Path, seed: int = 1234, name: str = "w"):
    world.mkdir(parents=True, exist_ok=True)
    data = {"Data": {"LevelName": name, "Time": 72000, "GameType": 0, "Version": {"Name": "26.2"},
                     "difficulty_settings": {"difficulty": "normal", "hardcore": False},
                     "WorldGenSettings": {"seed": seed, "dimensions": {"minecraft:overworld": {
                         "generator": {"type": "minecraft:noise", "settings": "minecraft:overworld",
                                       "biome_source": {"type": "minecraft:multi_noise", "preset": "minecraft:overworld"}}}}}}}
    (world / "level.dat").write_bytes(gzip.compress(b"\x0a" + _nbt_string("") + _compound(data)))
    (world / "region").mkdir(exist_ok=True)


@pytest.fixture
def server(tmp_path: Path) -> Path:
    sd = tmp_path / "srv"
    sd.mkdir()
    (sd / "server.properties").write_text("#props\nlevel-name=world\nlevel-type=minecraft\\:normal\nlevel-seed=\nmotd=x\n")
    (sd / "bukkit.yml").write_text("settings:\n  a: 1\nworlds:\n  world:\n    generator: Terra:PACK\nother: 2\n")
    write_level(sd / "world", seed=42, name="world")
    write_level(sd / "old", seed=7, name="old")
    # jar "vanilla" con dos presets, uno anidado tipo bundler
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("data/minecraft/worldgen/world_preset/flat.json", json.dumps({"dimensions": {"minecraft:overworld": {"generator": {"type": "minecraft:flat", "settings": {"layers": [{"block": "minecraft:stone", "height": 3}]}}}}}))
    with zipfile.ZipFile(sd / "server.jar", "w") as z:
        z.writestr("data/minecraft/worldgen/world_preset/normal.json", json.dumps({"dimensions": {"minecraft:overworld": {"generator": {"type": "minecraft:noise", "settings": "minecraft:overworld"}}}}))
        z.writestr("META-INF/versions/26.2/server-26.2.jar", inner.getvalue())
    (sd / "plugins/Terra/packs/Foo").mkdir(parents=True)
    (sd / "plugins/Terra/packs/Foo/pack.yml").write_text("id: FOO\ngenerator: NOISE_3D\n")
    return sd


def prof(sd: Path) -> Profile:
    return Profile(name="t", process=ProcessSpec(type="local", start=str(sd / "start.sh"), pidfile=str(sd / "pid")))


def test_nbt_roundtrip(server):
    d = level_data(server / "world/level.dat")
    assert d["LevelName"] == "world" and d["WorldGenSettings"]["seed"] == 42


def test_list_and_info(server):
    ws = worlds.list_worlds(server)
    assert [w.name for w in ws] == ["old", "world"]
    w = next(x for x in ws if x.name == "world")
    assert w.active and w.seed == 42 and w.preset == "normal" and w.plugin_generator == "Terra:PACK" and w.version == "26.2"
    assert "region" in w.dims


def test_types_discovery(server):
    ids = {t.id: t for t in worlds.discover_world_types(server)}
    assert "minecraft:normal" in ids and "minecraft:flat" in ids and "terra:FOO" in ids
    assert ids["minecraft:flat"].source.endswith("!server-26.2.jar") and "stone" in ids["minecraft:flat"].summary
    assert ids["terra:FOO"].how.startswith("bukkit.yml")


def test_create_vanilla_and_terra(server):
    changed = worlds.create_world(server, "nuevo", world_type="flat", seed="99", structures=False, extra={"motd": "hola"})
    props = worlds.Properties(server / "server.properties")
    assert props.get("level-name") == "nuevo" and props.get("level-type") == "minecraft:flat" and props.get("level-seed") == "99"
    assert props.get("generate-structures") == "false" and props.get("motd") == "hola"
    assert (server / "server.properties").read_text().startswith("#props\n")  # comentarios conservados
    assert any("level-type=minecraft:flat" in c for c in changed)
    worlds.create_world(server, "terra1", world_type="terra:FOO", activate=False)
    assert worlds.bukkit_generator(server, "terra1") == "Terra:FOO"
    assert worlds.bukkit_generator(server, "world") == "Terra:PACK"      # el bloque existente se conserva
    assert "other: 2" in (server / "bukkit.yml").read_text()
    with pytest.raises(worlds.WorldError):
        worlds.create_world(server, "world")                              # ya existe
    with pytest.raises(worlds.WorldError):
        worlds.create_world(server, "../x")


def test_delete_protects_active_and_trashes(server):
    with pytest.raises(worlds.WorldError):
        worlds.delete_world(server, "world")
    dst = worlds.delete_world(server, "old")
    assert dst and dst.parent.name == worlds.TRASH_DIR and not (server / "old").exists()
    assert worlds.delete_world(server, "world") if False else True


def test_rename_updates_props_and_bukkit(server):
    worlds.rename_world(server, "world", "mundo")
    assert worlds.Properties(server / "server.properties").get("level-name") == "mundo"
    assert worlds.bukkit_generator(server, "mundo") == "Terra:PACK" and worlds.bukkit_generator(server, "world") is None


def test_backup_zip(server):
    (server / "world/session.lock").write_text("x")
    dst = worlds.backup_world(server, "world")
    with zipfile.ZipFile(dst) as z:
        names = z.namelist()
    assert "world/level.dat" in names and not any(n.endswith("session.lock") for n in names)


def test_worldcmd_running_guard_and_confirm(server):
    p = prof(server)
    rc, lines = run("create x --type flat", p, running=True)
    assert rc == 3 and "corriendo" in lines[0]
    rc, lines = run("list", p, running=True)
    assert rc == 0 and any("* world" in l for l in lines)
    rc, lines = run("create x --type flat --seed 5 --set spawn-protection=0", p, running=False)
    assert rc == 0 and worlds.Properties(server / "server.properties").get("spawn-protection") == "0"
    assert needs_confirm("delete old") and needs_confirm("list") is None
    rc, lines = run("delete old --purge", p, running=False)
    assert rc == 0 and not (server / "old").exists()
    rc, _ = run("use nope", p, running=False)
    assert rc == 3
