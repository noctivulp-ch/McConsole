# mc-console

Consola para servidores Minecraft que corren desacoplados de la terminal (local o remoto): **log en vivo + comandos
RCON + control del proceso**. Dos frontends sobre el mismo núcleo:

- `mc-console` — TUI (Textual) para humanos: log arriba, prompt abajo, estado, pestañas por servidor, filtro, historial.
- `mc-rcon` — CLI para scripts y agentes: salida limpia, varios comandos por conexión, `--json`, `--log-wait`, `:start/:stop/:status`.

Humano y bot conviven: RCON admite varios clientes y el log es la fuente de verdad para ambos (incluida la salida
diferida de plugins, que RCON nunca devuelve).

## Instalación
```bash
uv tool install --editable .        # desde AppCreator/McConsole (o pipx install .)
mc-console --init                                  # escribe ~/.config/mc-console/servers.toml de ejemplo
```

## Perfiles (`~/.config/mc-console/servers.toml`)
```toml
[servers.survival]
host = "127.0.0.1"
rcon_port = 25575
password_file = "~/.config/mc-console/survival.pw"     # 0600; o password_env = "MC_RCON_PW"
[servers.survival.log]
type = "local"                                          # "local" | "ssh"
path = "/srv/mc/survival/console.log"
[servers.survival.process]                                  # opcional
type = "local"                                          # "local" | "ssh"
start = "/srv/mc/survival/start.sh"                         # script desacoplado que escribe el pidfile
pidfile = "/srv/mc/survival/server.pid"

[servers.vps]
host = "vps.example.net"
rcon_port = 25575
password_file = "~/.config/mc-console/vps.pw"
[servers.vps.log]
type = "ssh"
ssh_host = "mc@vps.example.net"                          # acceso por clave (BatchMode)
path = "/opt/mc/logs/latest.log"
[servers.vps.process]
type = "ssh"
ssh_host = "mc@vps.example.net"
start = "/opt/mc/start.sh"
pidfile = "/opt/mc/server.pid"
```
Para remoto, RCON debe ser alcanzable (túnel SSH `-L 25575:127.0.0.1:25575` si no está expuesto).

## Uso
```bash
mc-console survival               # TUI de un servidor
mc-console survival vps           # dos pestañas (Ctrl-N / Ctrl-B)
mc-rcon -p survival "list" "tps"  # bot: una conexión, cabecera '# cmd' por respuesta
mc-rcon -p survival --log-wait 2 "lp user X info"
mc-rcon -p survival --json "list"
mc-rcon -p survival :status | :start | :stop | :restart
echo -e "say a\nsay b" | mc-rcon -p survival -
```
Dentro de la TUI: `Tab` / `Ctrl-Espacio` **autocompleta** comandos locales (`:`), del juego y sus subcomandos
(árbol pedido al servidor con `minecraft:help`, incluye mods y plugins; repetir = rotar candidatos) · `→` acepta la
sugerencia del historial · `F1` ayuda · `F2` filtro · `↑↓` historial · `Ctrl-R` buscar · `Ctrl-L` limpiar ·
`F5` reconectar · `F7/F8` pestañas · `:status :start :stop :restart :tail N :filter :tab :q`. Salir **no** para el servidor.

## Modo máquina y códigos de salida
`mc-rcon` no usa colores ni cursor (a menos que `--color`); `0` ok · `2` contraseña rechazada · `3` sin conexión /
proceso · `4` uso. `mc-console` sin TTY sale con 4 y te manda a `mc-rcon`.

## Script de arranque esperado
```bash
java ... -jar server.jar nogui <&3 >> console.log 2>&1 &
echo $! > server.pid; wait $!; rm -f server.pid
```

## Por qué Textual
Ver `docs/decisiones/0001-stack.md`. Resumen: Python ya estaba en el flujo; hacen falta paneles, pestañas, filtro,
ratón opcional y tests de pantalla; `textual-serve` permite servir la misma consola en un navegador más adelante.
