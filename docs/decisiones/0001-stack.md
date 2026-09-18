# ADR 0001 — Stack de mc-console: Python + Textual (TUI) / argparse (CLI)

**Fecha**: 2026-09-17 · **Estado**: aceptada · **Método**: skill `tui-architect` (decisión por modo → restricciones → panorama).

## Contexto
Consola para servidores Minecraft desacoplados de la terminal. La usan a la vez un humano (ventana de foot) y un
agente (comandos externos). Hay que ver el log en vivo, mandar comandos RCON, arrancar/parar el proceso, y servir
tanto servidores locales como remotos (VPS por SSH). Uso personal; sin necesidad de distribuir binarios.

## Decisión
- **Modo dual**: núcleo sin UI (`rcon`, `sources`, `process`, `profiles`) + TUI (`mc-console`) + CLI máquina (`mc-rcon`).
- **Python**: ya es el lenguaje del tooling alrededor; instalación aislada con `uv tool install`.
- **Textual 8.2** para la TUI: paneles/pestañas/filtro/ratón opcional, tests de pantalla con `Pilot`, refresco
  diferencial, y `textual-serve` como camino a una consola web sin reescribir. Alternativa descartada:
  `prompt_toolkit` (suficiente para un panel, corto para pestañas y estados).
- **argparse** para la CLI: sin dependencias extra; la ergonomía de Typer no aporta aquí.
- **Cliente RCON propio** (protocolo Source, 80 líneas) en vez de `mcrcon`: multipaquete correcto, sin proceso
  externo, reutilizable por ambos frontends.

## Consecuencias
- Requiere Python ≥ 3.11 (tomllib) en la máquina del usuario; aceptable (uso personal, Arch).
- Textual tiene un mantenedor principal (Textualize cerró en 2025): riesgo de bus factor asumido; la API que usamos
  es básica (RichLog, Input, TabbedContent, workers) y estable desde 1.0.
- Remoto: el log viaja por `ssh tail -F` y el proceso se controla por SSH; RCON debe ser alcanzable (túnel si no).
