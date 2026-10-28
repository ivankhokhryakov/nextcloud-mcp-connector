---
status: complete
date: 2026-10-22
issue: 31
---

# Quick 261022-vg2: Issue #31, files_update folgt dem Admin-Schalter

- Ursache: `entry_exapp` importierte `mcp_connector.server` auf Modulebene; die Registrierung von `files_update` las `os.environ` vor `_resolved_env()`. Deploy-Variable entschied allein, in beide Richtungen (Haken an = Tool fehlt; Haken aus = Schreib-Tool bleibt aktiv, wenn die Variable on sagt).
- Fix: Server-Import in `build_exapp_app()`, `main()` schreibt den aufgelösten `NC_MCP_FILES_UPDATE` vor dem Build nach `os.environ` (zweiter benannter Schlüssel neben `NC_MCP_TALK_SEND`).
- Tests: `tests/unit/test_exapp_files_update_switch.py` (frischer Interpreter je Fall, 5 Kombinationen + Import-Isolation); Gegenprobe gegen alten Code: 4 Bug-Fälle rot, 2 Kontrollen grün. Drei In-Process-Tests und zwei angepasste Strukturtests in `test_exapp_entry.py`.
- Gates: ruff, format, pyright (latest), vulture, check_tool_budget, volle Suite mit Extra `documents` grün.
- Commit: 9c3422d. Release 0.6.1 nach Owner-Freigabe 22.10.
