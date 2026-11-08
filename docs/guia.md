# Guía de uso de mc-console

Consola para servidores Minecraft que corren desacoplados de la terminal (local o remoto): **log en vivo + comandos
RCON + control del proceso**. Dos frontends sobre el mismo núcleo:

- `mc-console` — TUI (Textual) para humanos: log arriba, prompt abajo, estado, pestañas por servidor, filtro, historial.
- `mc-rcon` — CLI para scripts y agentes: salida limpia, varios comandos por conexión, `--json`, `--log-wait`, `:start/:stop/:status`, `:world …` (mundos).

Humano y bot conviven: RCON admite varios clientes y el log es la fuente de verdad para ambos (incluida la salida
diferida de plugins, que RCON nunca devuelve).

## Instalación
```bash
uv tool install --editable .        # desde la raíz del repositorio (o pipx install .)
mc-console --init                                  # escribe ~/.config/mc-console/servers.toml de ejemplo
```

## Perfiles (`~/.config/mc-console/servers.toml`)

Tipos de log: `local` (fichero), `ssh` (`tail -F` remoto) y **`sftp`** (sondeo de `logs/latest.log` por SFTP, para
paneles tipo Pterodactyl que dan SFTP pero no shell; instalar con el extra `.[sftp]` = paramiko; claves
`sftp_host/sftp_port/sftp_user/sftp_password_file|sftp_key`).
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
mc-console                    # TUI con TODOS los perfiles como pestañas (F7/F8, :tab NOMBRE)
mc-console survival               # igual, pero entra directo en la pestaña survival
mc-console survival vps --only    # solo esas dos
```
Las pestañas de arriba son los perfiles. Cada sesión (seguir log, RCON, proceso) arranca la primera vez que miras su
pestaña, así un perfil remoto no lanza `ssh` hasta que lo abres. `:profile new` abre un formulario que escribe el
perfil en `servers.toml` y añade la pestaña; `:profile close` cierra la pestaña (no borra el perfil).

**Selección y copia**: arrastra sobre el log para seleccionar (se resalta) y **al soltar se copia solo** al
portapapeles del sistema (OSC 52; también Ctrl-C). En foot debe estar `osc52=enabled` (es el valor por defecto);
en tmux, `set -s set-clipboard on`. Si prefieres la selección nativa de la terminal, usa Shift+arrastrar.

```bash
mc-rcon -p survival "list" "tps"  # bot: una conexión, cabecera '# cmd' por respuesta
mc-rcon -p survival --log-wait 2 "lp user X info"
mc-rcon -p survival --json "list"
mc-rcon -p survival :status | :start | :stop | :restart  ·  `:ip` (dirección para conectarse desde Minecraft)
mc-rcon -p survival :world list | info W | types | create W --type minecraft:flat --seed 3 | use W | rename A B | delete W | backup W
echo -e "say a\nsay b" | mc-rcon -p survival -
```

### Mundos (`:world`, CLI y TUI)
Trabaja sobre la carpeta del servidor (perfil `[process]` local: la del `start` o `server_dir = "…"`): lista las
carpetas con `level.dat` (activo = `level-name`; tamaño, versión, semilla, generador real leído de `level.dat` /
`world_gen_settings.dat`, horas jugadas), `info`, `types` (**presets descubiertos dinámicamente**: `data/*/worldgen/
world_preset/*.json` del jar del servidor —incluido el jar anidado del bundler—, `mods/*.jar`, `plugins/*.jar`,
datapacks de los mundos, y packs de Terra en `plugins/Terra/packs` como `terra:ID`), `create` (escribe
`server.properties`: `level-name/level-type/level-seed/generator-settings/generate-structures/hardcore/difficulty/
gamemode` + `--set clave=valor` para cualquier otra; con `terra:*` añade `worlds.<mundo>.generator: Terra:ID` a
`bukkit.yml`; el mundo se genera al arrancar, `--start` lo arranca), `use` (cambia `level-name`), `rename`, `delete`
(mueve a `worlds-trash/`, `--purge` borra; nunca el activo) y `backup` (zip en `backups/`; si el servidor corre,
`save-all` por RCON antes). Crear/usar/renombrar exigen servidor parado; la TUI pide confirmación.
Dentro de la TUI: `Tab` / `Ctrl-Espacio` **autocompleta** comandos locales (`:`), del juego y sus subcomandos
(árbol pedido al servidor con `minecraft:help`, incluye mods y plugins; repetir = rotar candidatos) · `→` acepta la
sugerencia fantasma (historial y, si no, el completador: `:wo` → `:world`) · `Ctrl/Alt+⌫` borra palabra, `Alt+D` la siguiente · `F1` ayuda · `F2` filtro · `↑↓` historial · `Ctrl-R` buscar · `Ctrl-L` limpiar ·
`F5` reconectar · `F7/F8` pestañas · `PgUp/PgDn`, `Ctrl-Home/End` mover el log (si subes a leer, las líneas nuevas
**no** te bajan: el estado muestra `▼ N nuevas`; al volver abajo se reengancha solo) · `:status :start :stop :restart :world … :tail N :filter :tab :q`. Salir **no** para el servidor.

## Modo máquina y códigos de salida
`mc-rcon` no usa colores ni cursor (a menos que `--color`); `0` ok · `2` contraseña rechazada · `3` sin conexión /
proceso · `4` uso. `mc-console` sin TTY sale con 4 y te manda a `mc-rcon`.

## Script de arranque esperado (log por arranque + pidfile)
```bash
mkdir -p console-logs
find console-logs -name 'console-*.log' -mtime +60 -delete                       # retención por días
ls -1t console-logs/console-*.log | tail -n +30 | xargs -r rm -f --              # retención por número
LOG="console-logs/console-$(date +%F_%H-%M-%S).log"; : > "$LOG"; ln -sfn "$LOG" console.log
java ... -jar server.jar nogui <&3 >> "$LOG" 2>&1 &
echo $! > server.pid; wait $!; echo "SERVER_EXIT=$?" >> "$LOG"; rm -f server.pid
```
`mc-console` sigue `console.log` (el symlink): al abrir carga el archivo actual entero — es decir, **el arranque
completo** — y cuando el servidor reinicia detecta el archivo nuevo y reengancha. `--tail N` limita la carga inicial.

## Por qué Textual
Ver [`decisiones/0001-stack.md`](decisiones/0001-stack.md). Resumen: Python ya estaba en el flujo; hacen falta paneles, pestañas, filtro,
ratón opcional y tests de pantalla; `textual-serve` permite servir la misma consola en un navegador más adelante.
