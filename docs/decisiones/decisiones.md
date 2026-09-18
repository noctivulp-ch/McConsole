
## 0002 — Gestión de mundos desde la consola (2026-09-18)
Contexto: crear/borrar/cambiar mundos exigía editar `server.properties`/`bukkit.yml` a mano y saber qué presets tiene
el servidor. Decisión: `:world` en CLI y TUI (`worlds.py` + `worldcmd.py` compartidos), **sin inventar formatos**:
solo edita lo que el servidor lee (properties, bukkit.yml) y descubre los tipos escaneando jars/datapacks/packs de
Terra. Lector NBT propio (`nbt.py`, ~80 líneas) en vez de una dependencia. Acciones destructivas: confirmación en la
TUI, papelera `worlds-trash/` por defecto, nunca el mundo activo, servidor parado para crear/usar/renombrar.
Fuera de alcance por ahora: perfiles SSH (la lógica es de carpeta local; se haría con un `ServerFS` remoto).
