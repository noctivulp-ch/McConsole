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


async def test_tab_completes_local_and_game(profile, fake_server):
    fake_server.responses["minecraft:help"] = "/advancement (grant|revoke)\n/girls (locateAll|spawnCustom)\n/list\n/gamemode <mode> [<targets>]\n"
    fake_server.responses["minecraft:help girls spawnCustom"] = "/girls spawnCustom <id> [<pos>]\n"
    app = ConsoleApp([profile])
    async with app.run_test(size=(100, 30)) as pilot:
        assert await wait_for(lambda: "catálogo de comandos" in log_text(app))
        prompt = app.query_one("#prompt")
        await pilot.press(*":st", "tab")
        assert await wait_for(lambda: prompt.value in (":start ", ":status ", ":st"))
        # ':st' → start|status: ciclo
        v1 = prompt.value
        await pilot.press("tab")
        assert await wait_for(lambda: prompt.value != v1 and prompt.value.startswith(":st"))
        await pilot.press("escape")
        prompt.value = ""
        await pilot.press(*"gir", "tab")
        assert await wait_for(lambda: prompt.value == "girls ")
        await pilot.press(*"sp", "tab")
        assert await wait_for(lambda: prompt.value == "girls spawnCustom ")
        await pilot.press("tab")  # sin literales: pista de argumentos
        assert await wait_for(lambda: "<id>" in log_text(app))


async def test_sticky_bottom(profile, fake_server):
    """Si el usuario sube, las líneas nuevas no lo bajan y se cuentan; al volver abajo se reengancha."""
    from pathlib import Path
    log_path = Path(profile.log.path)
    log_path.write_text("".join(f"[00:00:{i:02d} INFO]: linea {i}\n" for i in range(200)))
    app = ConsoleApp([profile], tail=200)
    async with app.run_test(size=(80, 20)) as pilot:
        assert await wait_for(lambda: "linea 199" in log_text(app))
        pane = app.query_one(ServerPane)
        await pilot.pause()
        assert pane.log.is_vertical_scroll_end
        await pilot.press("pageup", "pageup")
        await pilot.pause()
        assert not pane.log.is_vertical_scroll_end
        y_before = pane.log.scroll_y
        with log_path.open("a") as f:
            f.write("[00:01:00 INFO]: nueva A\n[00:01:01 INFO]: nueva B\n")
        assert await wait_for(lambda: pane.session.unseen == 2, timeout=4)
        await pilot.pause()
        assert pane.log.scroll_y == y_before          # no me movió
        assert "2 nuevas" in str(pane.query_one("#status").render())
        await pilot.press("ctrl+end")
        assert await wait_for(lambda: pane.session.unseen == 0)
        assert pane.log.is_vertical_scroll_end
        with log_path.open("a") as f:
            f.write("[00:01:02 INFO]: nueva C\n")
        assert await wait_for(lambda: "nueva C" in log_text(app), timeout=4)
        await pilot.pause(); await pilot.pause()
        assert pane.log.following and pane.log.is_vertical_scroll_end   # reenganchado: sigue bajando


async def test_loads_whole_file_and_follows_rotation(profile, fake_server, tmp_path):
    """Por defecto se carga el archivo entero; si console.log (symlink) pasa a apuntar a otro archivo, se reengancha."""
    import os
    from pathlib import Path
    real1 = tmp_path / "console-1.log"; real1.write_text("".join(f"[00:00:{i:02d} INFO]: boot1 {i}\n" for i in range(150)))
    link = tmp_path / "console.log"
    if link.exists():
        link.unlink()
    os.symlink(real1.name, link)
    profile.log.path = str(link)
    app = ConsoleApp([profile])
    async with app.run_test(size=(80, 20)) as pilot:
        assert await wait_for(lambda: "boot1 0" in log_text(app) and "boot1 149" in log_text(app))
        real2 = tmp_path / "console-2.log"; real2.write_text("[00:01:00 INFO]: boot2 start\n")
        tmp_link = tmp_path / "console.log.tmp"; os.symlink(real2.name, tmp_link); os.replace(tmp_link, link)
        assert await wait_for(lambda: "arranque nuevo" in log_text(app) and "boot2 start" in log_text(app), timeout=5)


async def test_drag_selection_autocopies(profile, fake_server, monkeypatch):
    """Arrastrar sobre el log selecciona texto y al soltar se copia solo (OSC 52)."""
    copied = []
    app = ConsoleApp([profile])
    monkeypatch.setattr(ConsoleApp, "copy_to_clipboard", lambda self, text: copied.append(text))
    async with app.run_test(size=(80, 20)) as pilot:
        assert await wait_for(lambda: "something odd" in log_text(app))
        pane = app.query_one(ServerPane)
        await pilot.mouse_down(pane.log, offset=(1, 0))
        await pilot.hover(pane.log, offset=(30, 1))
        await pilot.mouse_up(pane.log, offset=(30, 1))
        assert await wait_for(lambda: bool(copied))
        # selección parcial de las filas 0-1 (el log tiene padding 0 1: widget x=1 → contenido col 0; x=30 → col 29):
        # exactamente eso, no todo el log (regresión: sin Strip.apply_offsets el compositor caía en SELECT_ALL)
        row0, row1 = pane.log.lines[0].text, pane.log.lines[1].text
        assert copied[0] == row0.rstrip() + "\n" + row1[:30].rstrip()  # fin inclusivo
        assert "something odd" not in copied[0] and "Starting minecraft" not in copied[0]


async def test_profile_form_creates_tab(profile, fake_server, tmp_path, monkeypatch):
    import mcconsole.app as appmod
    import mcconsole.profiles as prof
    toml = tmp_path / "servers.toml"
    monkeypatch.setattr(prof, "config_path", lambda: toml)
    monkeypatch.setattr(appmod, "config_path", lambda: toml)
    app = ConsoleApp([profile])
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press(*":profile new", "enter")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, appmod.ProfileForm)
        form.query_one("#f-name").value = "vps"
        form.query_one("#f-host").value = "vps.example.net"
        form.query_one("#f-password_file").value = str(tmp_path / "vps.pw")
        form.query_one("#f-log_path").value = "/opt/mc/console.log"
        form.query_one("#f-log_ssh").value = "mc@vps.example.net"
        await pilot.click("#save")
        await pilot.pause()
        assert "[servers.vps]" in toml.read_text() and 'ssh_host = "mc@vps.example.net"' in toml.read_text()
        assert [s.profile.name for s in app.sessions] == ["test", "vps"]
        assert app.query_one("#tabs").active == "pane-vps"
        vps = app.sessions[1]
        assert vps.started  # la pestaña activa arranca; la remota solo cuando se mira


async def test_lazy_start_and_initial_active(profile, fake_server, tmp_path):
    pw = tmp_path / "pw2"; pw.write_text("secret")
    second = Profile(name="second", host="127.0.0.1", rcon_port=1, password_file=str(pw), log=LogSpec(type="local", path=str(tmp_path / "none.log")))
    app = ConsoleApp([profile, second], active="second")
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        assert app.query_one("#tabs").active == "pane-second"
        assert app.sessions[1].started and not app.sessions[0].started
        await pilot.press("f7")
        await pilot.pause()
        assert app.sessions[0].started
