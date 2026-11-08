#!/usr/bin/env python3
"""Regenera las capturas del README en docs/capturas/ (como mucho tres).

    tools/capturas/capturas.sh

Monta un servidor de ejemplo sin Minecraft: un RCON de mentira en 127.0.0.1, un log sintético y un
pidfile con el PID de este proceso, para que la consola muestre «servidor ON». Ningún dato real: los
jugadores, el mundo y las respuestas son inventados. Textual exporta SVG; Chromium (sin interfaz) lo
pasa a PNG y corte_diagonal.py une dos estados en una sola imagen.
"""
import asyncio
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

from mcconsole.app import ConsoleApp, ServerPane
from mcconsole.profiles import LogSpec, ProcessSpec, Profile

AQUI = Path(__file__).resolve().parent
SALIDA = AQUI.parents[1] / "docs" / "capturas"
TAMANO = (110, 40)
FUENTE = "JetBrains Mono"

LOG = """\
[18:02:11 INFO]: Starting minecraft server version 1.21.4
[18:02:11 INFO]: Loading properties
[18:02:12 INFO]: Default game type: SURVIVAL
[18:02:12 INFO]: Preparing level "mundo"
[18:02:14 INFO]: Preparing start region for dimension minecraft:overworld
[18:02:17 INFO]: Done (5.912s)! For help, type "help"
[18:02:17 INFO]: Starting remote control listener
[18:02:17 INFO]: RCON running on 0.0.0.0:25575
[18:05:40 INFO]: zorro_gris joined the game
[18:05:52 INFO]: <zorro_gris> buenas, ¿alguien para la expedición al Nether?
[18:06:03 INFO]: nube42 joined the game
[18:06:10 INFO]: <nube42> voy, dame un minuto
[18:07:31 WARN]: Can't keep up! Is the server overloaded? Running 2140ms or 42 ticks behind
[18:09:02 INFO]: nube42 has made the advancement [We Need to Go Deeper]
[18:10:15 INFO]: [Server] Copia de seguridad a las 19:00
[18:12:47 INFO]: zorro_gris has made the advancement [Hot Stuff]
[18:14:05 INFO]: <nube42> ya estoy en el portal
[18:15:20 INFO]: Saving the game (this may take a moment!)
[18:15:21 INFO]: Saved the game
[18:16:02 INFO]: nube42 lost connection: Disconnected
[18:16:02 INFO]: nube42 left the game
"""

RESPUESTAS = {
    "list": "There are 2 of a max of 20 players online: zorro_gris, nube42",
    "tps": "§6TPS from last 1m, 5m, 15m: §a20.0, §a19.98, §a20.0",
    "time query daytime": "The time is 6020",
    "minecraft:help": "/advancement (grant|revoke)\n/gamemode <mode> [<target>]\n/give <targets> <item> [<count>]\n"
                      "/list\n/say <message>\n/time (add|query|set)\n/tp <destination>\n/weather (clear|rain|thunder)\n",
}


class RconDeEjemplo:
    """Servidor RCON mínimo (protocolo Source): acepta la contraseña y responde desde RESPUESTAS."""

    def __init__(self, password: str):
        self.password = password
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("127.0.0.1", 25575))
        except OSError:
            self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        # Retiene la respuesta hasta que el log esté cargado: así el orden de las líneas es siempre el mismo.
        self.listo = threading.Event()
        threading.Thread(target=self._aceptar, daemon=True).start()

    def _aceptar(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._atender, args=(conn,), daemon=True).start()

    def _atender(self, conn):
        self.listo.wait()
        try:
            while raw := conn.recv(4):
                (n,) = struct.unpack("<i", raw)
                data = b""
                while len(data) < n:
                    data += conn.recv(n - len(data))
                pid, tipo = struct.unpack("<ii", data[:8])
                cuerpo = data[8:-2].decode()
                if tipo == 3:
                    self._enviar(conn, pid if cuerpo == self.password else -1, 2, "")
                else:
                    self._enviar(conn, pid, 0, RESPUESTAS.get(cuerpo, ""))
        except OSError:
            pass

    @staticmethod
    def _enviar(conn, pid, tipo, cuerpo):
        carga = struct.pack("<ii", pid, tipo) + cuerpo.encode() + b"\x00\x00"
        conn.sendall(struct.pack("<i", len(carga)) + carga)

    def cerrar(self):
        self.sock.close()


def perfiles(dir_: Path, port: int) -> list[Profile]:
    (dir_ / "pw").write_text("ejemplo\n")
    (dir_ / "console.log").write_text(LOG)
    (dir_ / "server.pid").write_text(str(os.getpid()))
    survival = Profile(
        name="survival", rcon_port=port, password_file=str(dir_ / "pw"),
        log=LogSpec(type="local", path=str(dir_ / "console.log")),
        process=ProcessSpec(type="local", pidfile=str(dir_ / "server.pid")),
    )
    # Pestañas de adorno: no se abren, así que nunca intentan conectar.
    otros = [Profile(name=n, rcon_port=port, password_file=str(dir_ / "pw")) for n in ("creativo", "vps")]
    return [survival, *otros]


async def esperar(cond, segundos=5.0):
    for _ in range(int(segundos / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("la consola no llegó al estado esperado")


def texto_log(app: ConsoleApp) -> str:
    return "\n".join(s.text for s in app.query_one(ServerPane).log.lines)


async def capturar(dir_: Path, rcon: RconDeEjemplo, tema: str, svgs: dict[str, Path], con_formulario: bool = False):
    rcon.listo.clear()
    app = ConsoleApp(perfiles(dir_, rcon.port))
    async with app.run_test(size=TAMANO) as pilot:
        app.theme = tema
        await esperar(lambda: "left the game" in texto_log(app))
        rcon.listo.set()
        for cmd in ("list", "tps"):
            await pilot.press(*cmd, "enter")
            await esperar(lambda c=cmd: re.sub("§.", "", RESPUESTAS[c])[:20] in texto_log(app))
        await esperar(lambda: "servidor ON" in str(app.query_one("#status").render()))
        await pilot.press(*"time q")
        await pilot.pause(0.3)
        svgs[f"{tema}"] = Path(app.save_screenshot(f"{tema}.svg", path=str(dir_)))
        if con_formulario:
            app.query_one("#prompt").value = ""
            await pilot.press(*":profile new", "enter")
            await pilot.pause(0.3)
            for campo, valor in (("name", "lobby"), ("log_path", "/srv/mc/lobby/console.log"),
                                 ("start", "/srv/mc/lobby/start.sh"), ("pidfile", "/srv/mc/lobby/server.pid")):
                app.screen.query_one(f"#f-{campo}").value = valor
            app.screen.query_one("#f-name").focus()
            app.screen.query_one("#profile-form").scroll_home(animate=False)
            await pilot.pause(0.5)
            svgs[f"{tema}-form"] = Path(app.save_screenshot(f"{tema}-form.svg", path=str(dir_)))


def a_png(svg: Path, png: Path) -> None:
    """Chromium sin interfaz, a escala 2: la fuente de la exportación de Textual se cambia por una local."""
    texto = re.sub(r'font-family: *"Fira Code"', f'font-family: "{FUENTE}"', svg.read_text())
    texto = re.sub(r"@font-face \{.*?\}", "", texto, flags=re.S)
    ancho, alto = (float(v) for v in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', texto).groups())
    html = svg.with_suffix(".html")
    html.write_text(f"<html><body style='margin:0'>{texto}</body></html>")
    navegador = shutil.which("chromium") or shutil.which("google-chrome-stable") or "chromium"
    subprocess.run([
        navegador, "--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
        f"--window-size={int(ancho)},{int(alto)}", f"--screenshot={png}", html.as_uri(),
    ], check=True, capture_output=True)


async def principal() -> None:
    SALIDA.mkdir(parents=True, exist_ok=True)
    for viejo in SALIDA.glob("*.png"):
        viejo.unlink()
    with tempfile.TemporaryDirectory() as tmp:
        dir_ = Path(tmp)
        rcon = RconDeEjemplo("ejemplo")
        svgs: dict[str, Path] = {}
        try:
            await capturar(dir_, rcon, "tokyo-night", svgs)
            await capturar(dir_, rcon, "catppuccin-latte", svgs, con_formulario=True)
        finally:
            rcon.cerrar()
        pngs = {k: dir_ / f"{k}.png" for k in svgs}
        for k, svg in svgs.items():
            a_png(svg, pngs[k])
        shutil.copyfile(pngs["tokyo-night"], SALIDA / "consola-oscuro.png")
        shutil.copyfile(pngs["catppuccin-latte-form"], SALIDA / "perfil-nuevo-claro.png")
        subprocess.run([
            "python", str(AQUI / "corte_diagonal.py"), str(pngs["catppuccin-latte"]),
            str(pngs["tokyo-night"]), str(SALIDA / "consola-claro-oscuro.png"),
        ], check=True)
    for f in sorted(SALIDA.glob("*.png")):
        print(f.relative_to(AQUI.parents[1]))


if __name__ == "__main__":
    asyncio.run(principal())
