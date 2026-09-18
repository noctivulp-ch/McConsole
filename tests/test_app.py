"""Pruebas de la TUI con Pilot. Escenarios: arranca y muestra el log; envía un comando y pinta la respuesta;
el filtro oculta líneas; comando local desconocido no rompe; el servidor caído no impide arrancar."""
import asyncio
from pathlib import Path

from mcconsole.app import ConsoleApp, ServerPane
from mcconsole.profiles import LogSpec, Profile


async def wait_for(pred, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def log_text(app: ConsoleApp) -> str:
    pane = app.query_one(ServerPane)
    return "\n".join(strip.text for strip in pane.log.lines)


async def test_shows_log_and_sends_command(profile, fake_server):
    app = ConsoleApp([profile])
    async with app.run_test(size=(100, 30)) as pilot:
        assert await wait_for(lambda: "Starting minecraft server" in log_text(app))
        await pilot.press(*"list", "enter")
        assert await wait_for(lambda: "list" in fake_server.received)
        assert await wait_for(lambda: "max of 5 players" in log_text(app))
        assert app.query_one(ServerPane).session.history[-1] == "list"


async def test_filter_hides_lines(profile, fake_server):
    app = ConsoleApp([profile])
    async with app.run_test(size=(100, 30)) as pilot:
        assert await wait_for(lambda: "something odd" in log_text(app))
        await pilot.press(*":filter WARN", "enter")
        await pilot.pause()
        text = log_text(app)
        assert "something odd" in text and "Starting minecraft" not in text
        await pilot.press("escape")
        await pilot.pause()
        assert "Starting minecraft" in log_text(app)


async def test_unknown_local_command(profile, fake_server):
    app = ConsoleApp([profile])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*":nada", "enter")
        assert await wait_for(lambda: "comando local desconocido" in log_text(app))


async def test_starts_with_server_down(tmp_path: Path):
    pw = tmp_path / "pw"; pw.write_text("x")
    prof = Profile(name="down", host="127.0.0.1", rcon_port=1, password_file=str(pw), log=LogSpec(type="local", path=str(tmp_path / "missing.log")))
    app = ConsoleApp([prof])
    async with app.run_test(size=(80, 24)) as pilot:
        assert await wait_for(lambda: "RCON no disponible" in log_text(app))
        await pilot.press(*"list", "enter")
        assert await wait_for(lambda: "¿Está el servidor arriba?" in log_text(app))
