"""mc-console — TUI (Textual): log en vivo + prompt RCON + estado del proceso, uno o varios servidores en pestañas.

Reglas que sigue (skill tui-architect): núcleo sin UI en rcon/sources/process; un solo bucle (los hilos solo
encolan líneas y el refresco se hace a ≤ 15 fps desde la app); teclado primero; 80×24 mínimo; colores por rol
sobre la paleta del usuario; estados de desconexión visibles; nunca imprime a stdout mientras vive.
"""
from __future__ import annotations

import argparse
import sys
import threading
from collections import deque
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.suggester import Suggester
from textual import events
from textual.widgets import Button, Footer, Input, Label, RichLog, Select, Static, TabbedContent, TabPane

from . import __version__
from .completion import Completer, GameCatalog
from .process import ProcessControl, make_process
from .profiles import LogSpec, ProcessSpec, Profile, ProfileError, append_profile, config_dir, config_path, load_profiles, resolve, write_example
from .rcon import Rcon, RconAuthError
from .sources import LogSource, make_source
from .text import log_line_text, response_text, strip_ansi

REFRESH_HZ = 12          # tope de refresco del log (SSH/tmux lo agradecen)
MAX_LINES = 20000        # líneas retenidas por servidor (un arranque entero cabe de sobra)
HISTORY_MAX = 500

HELP = """\
[b]Atajos[/b]   Enter enviar · Tab / Ctrl-Espacio autocompletar (repetir = rotar) · → aceptar sugerencia del historial
          ↑/↓ historial · Ctrl-R buscar en historial · F2 filtrar log · Esc quitar filtro
          PgUp/PgDn · Ctrl-Home/Ctrl-End mover el log (si subes, deja de seguir; vuelve a seguir al llegar abajo)
          Ctrl-L limpiar · F5 reconectar RCON · F7/F8 pestaña anterior/siguiente · F1 ayuda · Ctrl-Q salir
[b]Locales[/b]  :status  :start  :stop  :restart  :tail N  :clear  :reconnect  :filter TEXTO  :tab NOMBRE  :help  :q
          :profile new | list | close   (las pestañas de arriba son los perfiles de ~/.config/mc-console/servers.toml)
[b]Ratón[/b]    arrastrar sobre el log selecciona y al soltar se copia al portapapeles (también Ctrl-C)
          Todo lo demás se envía al servidor por RCON (con o sin '/'). 'stop' pide confirmación.
[b]Ojo[/b]      Salir NO para el servidor. La salida diferida de plugins no llega por RCON: se ve en el log."""


class Confirm(ModalScreen[bool]):
    """Diálogo mínimo sí/no (s/y = sí, n/Esc = no)."""

    BINDINGS = [Binding("s,y", "yes", "Sí"), Binding("n,escape", "no", "No")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.question)
            with Horizontal():
                yield Button("Sí [s]", id="yes", variant="error")
                yield Button("No [n]", id="no")

    @on(Button.Pressed, "#yes")
    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


class ProfileForm(ModalScreen["Profile | None"]):
    """Alta de perfil desde la TUI: escribe en servers.toml y abre la pestaña."""

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    FIELDS = [
        ("name", "nombre corto (sin espacios)", ""),
        ("host", "host RCON", "127.0.0.1"),
        ("rcon_port", "puerto RCON", "25575"),
        ("password_file", "fichero con la contraseña RCON (0600)", "~/.config/mc-console/<nombre>.pw"),
        ("log_path", "ruta del console.log (vacío = sin log)", ""),
        ("log_ssh", "ssh host para el log (vacío = local)", ""),
        ("start", "script de arranque (opcional)", ""),
        ("pidfile", "pidfile del servidor (opcional)", ""),
        ("proc_ssh", "ssh host para el proceso (vacío = local)", ""),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="profile-form"):
            yield Label("[b]Nuevo perfil[/b]  (Tab entre campos · Enter guarda · Esc cancela)")
            for key, label, default in self.FIELDS:
                yield Label(label, classes="field-label")
                yield Input(value=default if key != "password_file" else "", placeholder=default, id=f"f-{key}")
            yield Label("", id="form-error")
            with Horizontal():
                yield Button("Guardar", id="save", variant="primary")
                yield Button("Cancelar", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#f-name", Input).focus()

    def _val(self, key: str) -> str:
        return self.query_one(f"#f-{key}", Input).value.strip()

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        name = self._val("name")
        err = None
        if not name or " " in name or "." in name:
            err = "nombre: sin espacios ni puntos"
        elif not self._val("rcon_port").isdigit():
            err = "puerto: número"
        elif not self._val("password_file"):
            err = "hace falta el fichero de contraseña"
        if err:
            self.query_one("#form-error", Label).update(f"[red]{err}[/]")
            return
        proc = None
        if self._val("start") or self._val("pidfile"):
            proc = ProcessSpec(type="ssh" if self._val("proc_ssh") else "local", start=self._val("start"),
                               pidfile=self._val("pidfile"), ssh_host=self._val("proc_ssh"))
        prof = Profile(
            name=name, host=self._val("host") or "127.0.0.1", rcon_port=int(self._val("rcon_port")),
            password_file=self._val("password_file"),
            log=LogSpec(type="ssh" if self._val("log_ssh") else "local", path=self._val("log_path"), ssh_host=self._val("log_ssh")),
            process=proc,
        )
        self.dismiss(prof)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ServerSession:
    """Estado de un servidor dentro de la app: RCON, fuente de log, proceso, historial, cola de líneas."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.rcon: Rcon | None = None
        self.rcon_error = ""
        self.source: LogSource | None = make_source(profile.log)
        self.process: ProcessControl | None = make_process(profile.process)
        self.lines: deque[str] = deque(maxlen=MAX_LINES)
        self.pending: deque[str] = deque()
        self.pending_lock = threading.Lock()
        self.stop = threading.Event()
        self.filter = ""
        self.history: list[str] = []
        self.history_idx = -1
        self.proc_pid: int | None = None
        self.unseen = 0                      # líneas nuevas mientras el usuario lee arriba
        self.started = False                 # el seguimiento/RCON arrancan al entrar en la pestaña
        self.history_file = config_dir() / f"history-{profile.name}"
        self._load_history()
        self.catalog = GameCatalog(lambda cmd: self.ensure_rcon().command_retry(cmd))
        self.completer: Completer | None = None  # se crea en la app (necesita los nombres de pestaña)

    # --- historial
    def _load_history(self) -> None:
        try:
            self.history = self.history_file.read_text(encoding="utf-8").splitlines()[-HISTORY_MAX:]
        except OSError:
            self.history = []

    def remember(self, cmd: str) -> None:
        if self.history and self.history[-1] == cmd:
            return
        self.history.append(cmd)
        self.history = self.history[-HISTORY_MAX:]
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            self.history_file.write_text("\n".join(self.history) + "\n", encoding="utf-8")
        except OSError:
            pass

    # --- rcon
    def ensure_rcon(self) -> Rcon:
        if self.rcon is None:
            self.rcon = Rcon(self.profile.host, self.profile.rcon_port, self.profile.password())
        if not self.rcon.connected:
            self.rcon.connect()
        return self.rcon

    def push(self, line: str) -> None:
        with self.pending_lock:
            self.pending.append(line)


class HistorySuggester(Suggester):
    """Sugerencia fantasma (se acepta con →) a partir del historial de la sesión activa."""

    def __init__(self, app: "ConsoleApp"):
        super().__init__(use_cache=False, case_sensitive=True)
        self.app_ref = app

    async def get_suggestion(self, value: str) -> str | None:
        if len(value) < 2:
            return None
        for cmd in reversed(self.app_ref.session.history):
            if cmd.startswith(value) and cmd != value:
                return cmd
        return None


class StickyLog(RichLog):
    """RichLog que sigue el final solo mientras el usuario no haya subido a leer.

    `following` cambia únicamente con el scroll: si baja de posición (el usuario subió) se suelta; cuando
    vuelve a estar al final (PgDn, rueda, Ctrl-End) se reengancha. No se deriva por escritura porque el scroll
    se aplica en el siguiente layout y durante una ráfaga de líneas la vista parece no estar al final.
    """

    following = True
    on_follow_change = None  # callback(bool)

    # RichLog no implementa selección de texto (Log sí); la añadimos sobre sus Strips ya envueltos.
    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self._line_cache.clear()
        self.refresh()

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        selection = self.text_selection
        if selection is None:
            return strip
        scroll_x, scroll_y = self.scroll_offset
        span = selection.get_span(scroll_y + y)
        if span is None:
            return strip
        start, end = span
        text = Text.assemble(*[(seg.text, seg.style or "") for seg in strip])
        if end == -1:
            end = len(text)
        text.stylize(self.screen.get_component_rich_style("screen--selection"), max(0, start - scroll_x), max(0, end - scroll_x))
        return Strip(text.render(self.app.console), strip.cell_length)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value < old_value:
            self._set_following(False)
        elif not self.following:
            self.call_after_refresh(self._recheck)

    def _recheck(self) -> None:
        if self.is_vertical_scroll_end:
            self._set_following(True)

    def _set_following(self, value: bool) -> None:
        if value != self.following:
            self.following = value
            if self.on_follow_change:
                self.on_follow_change(value)


class ServerPane(TabPane):
    """Una pestaña = un servidor: log arriba, línea de estado abajo. El prompt es común a la app."""

    def __init__(self, session: ServerSession):
        super().__init__(session.profile.name, id=f"pane-{session.profile.name}")
        self.session = session

    def compose(self) -> ComposeResult:
        log = StickyLog(id="log", highlight=False, markup=False, wrap=True, max_lines=MAX_LINES, auto_scroll=False)
        log.on_follow_change = self._on_follow_change
        yield log
        yield Static(id="status", classes="status")

    @property
    def log(self) -> StickyLog:
        return self.query_one("#log", StickyLog)

    def append(self, text: Text) -> None:
        """Escribe respetando la lectura del usuario (sticky bottom): sigue al final solo si `following`;
        si está leyendo arriba, no lo mueve y cuenta las líneas nuevas hasta que vuelva abajo."""
        log = self.log
        log.write(text, scroll_end=log.following)
        if not log.following:
            self.session.unseen += 1

    def _on_follow_change(self, following: bool) -> None:
        if following:
            self.session.unseen = 0
        self.refresh_status()

    def refresh_status(self) -> None:
        s = self.session
        parts = [f"[b]{s.profile.name}[/b]", s.profile.label]
        if s.process is not None:
            parts.append("[green]servidor ON[/]" if s.proc_pid else "[red]servidor OFF[/]")
        if s.rcon is not None and s.rcon.connected:
            parts.append("[green]RCON ✓[/]")
        else:
            parts.append(f"[red]RCON ✗[/]{(' ' + s.rcon_error) if s.rcon_error else ''}")
        parts.append(f"log: {s.source.label if s.source else '—'}")
        if s.filter:
            parts.append(f"[yellow]filtro: {s.filter}[/]")
        if s.unseen:
            parts.append(f"[yellow]▼ {s.unseen} nuevas (Ctrl-End)[/]")
        self.query_one("#status", Static).update(" · ".join(parts))

    def rerender(self) -> None:
        """Vuelve a pintar el búfer aplicando el filtro (tras cambiar filtro o limpiar)."""
        log = self.log
        log.clear()
        f = self.session.filter.lower()
        for line in self.session.lines:
            if not f or f in strip_ansi(line).lower():
                log.write(log_line_text(line), scroll_end=False)
        log.scroll_end(animate=False)
        log._set_following(True)
        self.session.unseen = 0


class ConsoleApp(App[int]):
    TITLE = "mc-console"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    # Solo teclas que el Input con foco no consume (F-keys, Ctrl-*, flechas con priority).
    BINDINGS = [
        Binding("f1", "help", "Ayuda"),
        Binding("f2", "filter", "Filtrar"),
        Binding("f5", "reconnect", "Reconectar"),
        Binding("ctrl+l", "clear_log", "Limpiar"),
        Binding("f7", "prev_tab", "◀ pestaña", show=False),
        Binding("f8", "next_tab", "pestaña ▶", show=False),
        Binding("ctrl+q", "quit", "Salir"),
        Binding("escape", "clear_filter", show=False),
        Binding("up", "history_prev", show=False, priority=True),
        Binding("down", "history_next", show=False, priority=True),
        Binding("ctrl+r", "history_search", show=False, priority=True),
        Binding("tab,ctrl+at", "complete", "Completar", key_display="Tab", priority=True),
        Binding("pageup", "log_page(-1)", "Log ▲", show=False, priority=True),
        Binding("pagedown", "log_page(1)", "Log ▼", show=False, priority=True),
        Binding("ctrl+home", "log_home", show=False, priority=True),
        Binding("ctrl+end", "log_end", "Al final", show=False, priority=True),
    ]

    def __init__(self, profiles: list[Profile], tail: int | None = None, active: str | None = None):
        super().__init__()
        self.sessions = [ServerSession(p) for p in profiles]
        self.tail = tail
        self.initial_active = active
        self._last_copied: str | None = None

    # --- composición
    def compose(self) -> ComposeResult:
        with TabbedContent(id="tabs"):
            for s in self.sessions:
                yield ServerPane(s)
        yield Input(placeholder="comando RCON…  (:help para ayuda local, Tab completa)", id="prompt", suggester=HistorySuggester(self))
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        names = lambda: [x.profile.name for x in self.sessions]  # noqa: E731
        for s in self.sessions:
            s.completer = Completer(s.catalog, names)
        tabs = self.query_one("#tabs", TabbedContent)
        if self.initial_active and self.query(f"#pane-{self.initial_active}"):
            tabs.active = f"pane-{self.initial_active}"
        self.set_interval(1 / REFRESH_HZ, self.flush_pending)
        self.set_interval(5, self.poll_all)
        for pane in self.query(ServerPane):
            pane.refresh_status()
        # la sesión arranca en on_tab_activated (la pestaña por defecto también dispara el evento)

    def start_session(self, s: ServerSession) -> None:
        """Arranque perezoso: seguir el log, conectar RCON y sondear el proceso la primera vez que se mira la pestaña."""
        if s.started:
            return
        s.started = True
        self.start_follow(s)
        self.connect_rcon(s, verbose=True)
        self.poll_process(s)

    @on(TabbedContent.TabActivated, "#tabs")
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = event.pane
        if not isinstance(pane, ServerPane):
            return
        # TabbedContent activa la primera pestaña por su cuenta antes de que apliquemos la pedida: ignorarla
        if self.initial_active:
            if pane.session.profile.name != self.initial_active:
                return
            self.initial_active = None
        self.start_session(pane.session)
        pane.refresh_status()
        self.query_one("#prompt", Input).focus()

    # --- sesión activa
    @property
    def pane(self) -> ServerPane:
        tabs = self.query_one("#tabs", TabbedContent)
        return self.query_one(f"#{tabs.active}", ServerPane)

    @property
    def session(self) -> ServerSession:
        return self.pane.session

    def pane_of(self, s: ServerSession) -> ServerPane:
        return self.query_one(f"#pane-{s.profile.name}", ServerPane)

    def say(self, s: ServerSession, text: Text | str) -> None:
        """Mensaje de la propia consola (no del log) en el panel de un servidor."""
        self.pane_of(s).append(text if isinstance(text, Text) else Text.from_markup(text))

    # --- workers (hilos): solo encolan; la UI se toca desde el bucle principal
    @work(thread=True, exclusive=False)
    def start_follow(self, s: ServerSession) -> None:
        if s.source is None:
            s.push("\x1b[90m(este perfil no define log)\x1b[0m")
            return
        s.source.follow(s.push, s.stop, self.tail)

    @work(thread=True)
    def connect_rcon(self, s: ServerSession, verbose: bool = False) -> None:
        try:
            s.ensure_rcon()
            s.rcon_error = ""
            if verbose:
                self.call_from_thread(self.say, s, f"[green]✓ RCON conectado a {s.profile.label}[/]")
            if not s.catalog.ready:
                try:
                    s.catalog.load_root()
                    n = len(s.catalog.nodes[()].literals)
                    self.call_from_thread(self.say, s, f"[dim]catálogo de comandos: {n} raíces (Tab para completar)[/]")
                except Exception as e:  # noqa: BLE001
                    self.call_from_thread(self.say, s, f"[dim]sin catálogo de comandos ({e})[/]")
        except RconAuthError as e:
            s.rcon_error = str(e)
            self.call_from_thread(self.say, s, f"[red]✗ {e}[/]")
        except Exception as e:  # noqa: BLE001
            s.rcon_error = "sin conexión"
            if verbose:
                self.call_from_thread(self.say, s, f"[red]✗ RCON no disponible ({e}); el log sigue, reintento al enviar[/]")
        self.call_from_thread(self.pane_of(s).refresh_status)

    @work(thread=True)
    def poll_process(self, s: ServerSession) -> None:
        if s.process is None:
            return
        s.proc_pid = s.process.pid()
        self.call_from_thread(self.pane_of(s).refresh_status)

    def poll_all(self) -> None:
        for s in self.sessions:
            if s.started:
                self.poll_process(s)

    @work(thread=True)
    def send_command(self, s: ServerSession, cmd: str) -> None:
        try:
            resp = s.ensure_rcon().command_retry(cmd)
        except Exception as e:  # noqa: BLE001
            if s.rcon:
                s.rcon.close()
            s.rcon_error = "sin conexión"
            self.call_from_thread(self.say, s, f"[red]✗ RCON: {e}. ¿Está el servidor arriba? (:status / :start)[/]")
            self.call_from_thread(self.pane_of(s).refresh_status)
            return
        resp = resp.strip()
        if resp:
            for line in resp.splitlines():
                t = Text("› ", style="cyan")
                t.append(response_text(line))
                self.call_from_thread(self.say, s, t)
        else:
            self.call_from_thread(self.say, s, "[dim]› (sin respuesta inmediata; si es un plugin, mira el log)[/]")
        self.call_from_thread(self.pane_of(s).refresh_status)

    @work(thread=True)
    def process_action(self, s: ServerSession, op: str) -> None:
        p = s.process
        if p is None:
            self.call_from_thread(self.say, s, "[red]este perfil no define [process] (pidfile/start)[/]")
            return
        if op in ("stop", "restart"):
            if p.pid() is None and op == "stop":
                self.call_from_thread(self.say, s, "[yellow]el servidor ya está parado[/]")
                return
            how = p.stop(s.rcon)
            self.call_from_thread(self.say, s, {"rcon": "■ stop enviado por RCON", "sigterm": "[yellow]RCON no respondió; SIGTERM[/]", "none": "[yellow]sin proceso que parar[/]"}[how])
            if op == "restart":
                self.call_from_thread(self.say, s, "⏳ esperando a que el proceso termine…")
                if not p.wait_stopped():
                    self.call_from_thread(self.say, s, "[red]sigue vivo tras 90 s; no arranco otro[/]")
                    return
                self.call_from_thread(self.say, s, "[green]■ parado[/]")
        if op in ("start", "restart"):
            if not p.can_start:
                self.call_from_thread(self.say, s, "[red]el perfil no define process.start[/]")
                return
            if p.pid() is not None:
                self.call_from_thread(self.say, s, f"[yellow]ya corre (pid {p.pid()})[/]")
                return
            p.start()
            self.call_from_thread(self.say, s, "[green]▶ arrancando… (RCON reconecta al primer comando)[/]")
        s.proc_pid = p.pid()
        self.call_from_thread(self.pane_of(s).refresh_status)

    # --- refresco del log (desde el bucle principal)
    def flush_pending(self) -> None:
        for s in self.sessions:
            with s.pending_lock:
                if not s.pending:
                    continue
                batch = list(s.pending)
                s.pending.clear()
            pane = self.pane_of(s)
            f = s.filter.lower()
            before = s.unseen
            for line in batch:
                s.lines.append(line)
                if not f or f in strip_ansi(line).lower():
                    pane.append(log_line_text(line))
            if s.unseen != before:
                pane.refresh_status()

    # --- entrada
    @on(Input.Submitted, "#prompt")
    def on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        s = self.session
        s.history_idx = -1
        if not text:
            return
        if text.startswith(":"):
            self.local_command(text[1:])
            return
        cmd = text[1:] if text.startswith("/") else text
        s.remember(text)
        if cmd.split()[0].lower() == "stop":
            self.push_screen(Confirm(f"¿Parar el servidor {s.profile.name}?"), lambda ok: ok and self.process_or_rcon_stop(s))
            return
        self.send_command(s, cmd)

    def process_or_rcon_stop(self, s: ServerSession) -> None:
        if s.process is not None:
            self.process_action(s, "stop")
        else:
            self.send_command(s, "stop")

    def local_command(self, text: str) -> None:
        op, _, rest = text.partition(" ")
        s = self.session
        match op:
            case "q" | "quit" | "exit":
                self.exit(0)
            case "help":
                self.action_help()
            case "clear":
                self.action_clear_log()
            case "reconnect":
                self.action_reconnect()
            case "status":
                self.poll_process(s)
                pid = s.process.pid() if s.process else None
                self.say(s, f"servidor: {'corriendo, pid ' + str(pid) if pid else ('parado' if s.process else 'sin control de proceso')} · RCON: {'conectado' if s.rcon and s.rcon.connected else 'desconectado'}")
            case "start":
                self.process_action(s, "start")
            case "stop":
                self.push_screen(Confirm(f"¿Parar el servidor {s.profile.name}?"), lambda ok: ok and self.process_action(s, "stop"))
            case "restart":
                self.push_screen(Confirm(f"¿Reiniciar el servidor {s.profile.name}?"), lambda ok: ok and self.process_action(s, "restart"))
            case "tail":
                n = int(rest or 40) if (rest or "40").isdigit() else 40
                if s.source:
                    for line in s.source.tail(n):
                        self.pane.append(log_line_text(line))
            case "filter":
                s.filter = rest.strip()
                self.pane.rerender(); self.pane.refresh_status()
            case "profile":
                sub, _, name = rest.strip().partition(" ")
                if sub in ("new", "add"):
                    self.push_screen(ProfileForm(), self._profile_created)
                elif sub == "list":
                    self.say(s, "perfiles: " + ", ".join(f"[b]{x.profile.name}[/b] ({x.profile.label})" for x in self.sessions))
                elif sub == "close" and len(self.sessions) > 1:
                    self.close_session(s)
                else:
                    self.say(s, ":profile new | list | close")
            case "tab":
                tabs = self.query_one("#tabs", TabbedContent)
                if rest.strip() and self.query(f"#pane-{rest.strip()}"):
                    tabs.active = f"pane-{rest.strip()}"
                else:
                    self.say(s, f"pestañas: {', '.join(x.profile.name for x in self.sessions)}")
            case _:
                self.say(s, f"[red]comando local desconocido: :{op}[/]  (:help)")

    # --- acciones
    def _profile_created(self, prof: Profile | None) -> None:
        if prof is None:
            return
        try:
            append_profile(prof)
        except (ProfileError, OSError) as e:
            self.notify(str(e), severity="error", timeout=5)
            return
        session = ServerSession(prof)
        session.completer = Completer(session.catalog, lambda: [x.profile.name for x in self.sessions])
        self.sessions.append(session)
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.add_pane(ServerPane(session))
        tabs.active = f"pane-{prof.name}"
        self.notify(f"perfil '{prof.name}' guardado en {config_path()}", timeout=4)

    def close_session(self, s: ServerSession) -> None:
        """Cierra la pestaña (no borra el perfil del archivo)."""
        s.stop.set()
        if s.rcon:
            s.rcon.close()
        self.sessions.remove(s)
        self.query_one("#tabs", TabbedContent).remove_pane(f"pane-{s.profile.name}")

    def action_clear_log(self) -> None:
        self.session.lines.clear()
        self.pane.log.clear()

    def action_reconnect(self) -> None:
        if self.session.rcon:
            self.session.rcon.close()
        self.connect_rcon(self.session, verbose=True)

    def action_help(self) -> None:
        self.say(self.session, Text.from_markup(HELP))

    def action_filter(self) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.value = ":filter "
        prompt.cursor_position = len(prompt.value)
        prompt.focus()

    def action_clear_filter(self) -> None:
        s = self.session
        if s.filter:
            s.filter = ""
            self.pane.rerender(); self.pane.refresh_status()
        else:
            self.query_one("#prompt", Input).value = ""

    def _step_tab(self, delta: int) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        ids = [f"pane-{x.profile.name}" for x in self.sessions]
        if len(ids) < 2:
            return
        i = ids.index(tabs.active) if tabs.active in ids else 0
        tabs.active = ids[(i + delta) % len(ids)]

    def action_next_tab(self) -> None:
        self._step_tab(+1)

    def action_prev_tab(self) -> None:
        self._step_tab(-1)

    def _history_step(self, delta: int) -> None:
        s = self.session
        prompt = self.query_one("#prompt", Input)
        if not s.history:
            return
        if s.history_idx == -1:
            s.history_idx = len(s.history)
        s.history_idx = max(0, min(len(s.history), s.history_idx + delta))
        prompt.value = s.history[s.history_idx] if s.history_idx < len(s.history) else ""
        prompt.cursor_position = len(prompt.value)

    def action_history_prev(self) -> None:
        self._history_step(-1)

    def action_history_next(self) -> None:
        self._history_step(+1)

    def action_history_search(self) -> None:
        """Ctrl-R: busca hacia atrás en el historial lo escrito hasta ahora."""
        s = self.session
        prompt = self.query_one("#prompt", Input)
        needle = prompt.value.strip().lower()
        start = (s.history_idx if s.history_idx != -1 else len(s.history)) - 1
        for i in range(start, -1, -1):
            if needle in s.history[i].lower():
                s.history_idx = i
                prompt.value = s.history[i]
                prompt.cursor_position = len(prompt.value)
                return
        self.notify("sin más coincidencias en el historial", severity="warning", timeout=2)

    def action_complete(self) -> None:
        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            prompt.focus()
            return
        self._complete_worker(self.session, prompt.value)

    @work(thread=True, exclusive=True, group="complete")
    def _complete_worker(self, s: ServerSession, text: str) -> None:
        if s.completer is None:
            return
        try:
            result = s.completer.complete(text)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.say, s, f"[red]autocompletado: {e}[/]")
            return
        if result is None:
            return
        self.call_from_thread(self._apply_completion, s, text, result)

    def _apply_completion(self, s: ServerSession, original: str, result) -> None:
        prompt = self.query_one("#prompt", Input)
        if prompt.value != original:  # el usuario siguió escribiendo mientras tanto
            return
        self._last_completion_value = result.text  # Input.Changed llega después: no debe resetear el ciclo
        prompt.value = result.text
        prompt.cursor_position = len(result.text)
        import re as _re
        cycling = bool(_re.match(r"^\d+/\d+", result.hint))
        if cycling:
            # una sola línea compacta por vuelta: "2/5 · a  b  [c]  d  e"
            marked = [f"[{c}]" if i == s.completer._cycle_i else c for i, c in enumerate(result.candidates[:40])]  # type: ignore[union-attr]
            self.say(s, Text(f"  {result.hint.split(' · ')[0]} · " + "  ".join(marked), style="dim"))
            return
        if result.candidates:
            shown = result.candidates[:40]
            extra = f"  … +{len(result.candidates) - 40}" if len(result.candidates) > 40 else ""
            self.say(s, Text("  " + "  ".join(shown) + extra, style="dim"))
        if result.hint:
            self.say(s, Text("  " + result.hint, style="dim italic"))

    @on(Input.Changed, "#prompt")
    def on_prompt_changed(self, event: Input.Changed) -> None:
        if event.value != getattr(self, "_last_completion_value", None) and self.session.completer:
            self.session.completer.reset_cycle()

    # --- selección: al soltar el ratón, lo seleccionado se copia solo (como la selección primaria de X11 / Claude Code)
    def on_mouse_up(self, event: events.MouseUp) -> None:
        self.call_after_refresh(self._copy_selection_if_any)

    def _copy_selection_if_any(self) -> None:
        text = self.screen.get_selected_text()
        if not text or text == self._last_copied:
            return
        self._last_copied = text
        self.copy_to_clipboard(text)
        n = text.count("\n") + 1
        self.notify(f"copiado: {n} línea{'s' if n != 1 else ''}", timeout=1.5)

    def action_log_page(self, direction: int) -> None:
        log = self.pane.log
        (log.scroll_page_down if direction > 0 else log.scroll_page_up)(animate=False)

    def action_log_home(self) -> None:
        self.pane.log.scroll_home(animate=False)

    def action_log_end(self) -> None:
        log = self.pane.log
        log.scroll_end(animate=False)
        log._set_following(True)

    def action_quit(self) -> None:
        self.exit(0)

    async def on_unmount(self) -> None:
        for s in self.sessions:
            s.stop.set()
            if s.rcon:
                s.rcon.close()


def run_app(*profiles: Profile, tail: int | None = None, active: str | None = None) -> int:
    if not sys.stdout.isatty():
        print("mc-console necesita una terminal; para scripts usa mc-rcon", file=sys.stderr)
        return 4
    app = ConsoleApp(list(profiles), tail=tail, active=active)
    return app.run() or 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mc-console", description="Consola TUI para servidores Minecraft (log + RCON + proceso). Modo máquina: mc-rcon.")
    ap.add_argument("profiles", nargs="*", help="perfil a activar (todos los perfiles se abren como pestañas); varios → solo esos")
    ap.add_argument("--only", action="store_true", help="abrir únicamente los perfiles indicados")
    ap.add_argument("--tail", type=int, default=None, metavar="N", help="cargar solo las últimas N líneas (por defecto: el archivo actual entero = el arranque)")
    ap.add_argument("--init", action="store_true", help="escribir un servers.toml de ejemplo y salir")
    ap.add_argument("--profiles", dest="list_profiles", action="store_true", help="listar perfiles y salir")
    ap.add_argument("--version", action="version", version=f"mc-console {__version__}")
    args = ap.parse_args(argv)

    if args.init:
        p = write_example()
        print(f"perfiles: {p}")
        return 0
    if args.list_profiles:
        for p in load_profiles().values():
            print(f"{p.name:16s} {p.label}")
        return 0
    try:
        all_profiles = list(load_profiles().values())
        if args.profiles and (args.only or len(args.profiles) > 1):
            profiles, active = [resolve(n) for n in args.profiles], args.profiles[0]
        elif all_profiles:
            profiles, active = all_profiles, (args.profiles[0] if args.profiles else None)
            if active and active not in {p.name for p in profiles}:
                raise ProfileError(f"perfil '{active}' no existe (disponibles: {', '.join(p.name for p in profiles)})")
        else:
            profiles, active = [resolve(None)], None  # variables de entorno o error claro
    except (ProfileError, ValueError) as e:
        print(f"mc-console: {e}", file=sys.stderr)
        return 4
    return run_app(*profiles, tail=args.tail, active=active)


if __name__ == "__main__":
    sys.exit(main())
