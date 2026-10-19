---
phase: quick-261019-ojn
plan: 01
subsystem: bootstrap
tags: [security, bootstrap, bash, portability, IN-07, WR-03]
requires: []
provides:
  - newline-safe registration validators in both bootstrap scripts
affects:
  - scripts/bootstrap_exapp.sh
  - scripts/bootstrap_spike_opendesk.sh
tech-stack:
  added: []
  patterns:
    - "bash whole-string ERE match: local re='...'; [[ \"$value\" =~ $re ]]"
key-files:
  created: []
  modified:
    - scripts/bootstrap_exapp.sh
    - scripts/bootstrap_spike_opendesk.sh
    - tests/unit/test_exapp_env_setup.py
decisions:
  - "Validators match with [[ =~ ]] against an unquoted $re variable (works on bash 3.2 and 5), no grep"
  - "CR test values are passed via ANSI-C quoting $'...' because Git for Windows bash drops raw CR bytes from script text"
metrics:
  completed: 2026-10-19
  tasks: 2
  files: 3
---

# Quick 261019-ojn: Bootstrap validators newline-safe and BSD portable Summary

Die vier Registrierungs-Validatoren (require_hex64, require_port_number, require_registry_shape, require_url_shape) prüfen in beiden Bootstrap-Skripten jetzt per `[[ "$value" =~ $re ]]` den ganzen Wert statt per zeilenbasiertem `grep -E/-Ez`; damit fällt ein Wert mit eingebettetem LF auf GNU und BSD (macOS, PR #26) gleichermaßen durch.

## Commits

| Task | Commit | Beschreibung |
| ---- | ------ | ------------ |
| 1 (RED) | bef1cba | test(bootstrap): Mehrzeilen-Fälle, Spike-Abdeckung, No-grep-Guard |
| 2 (GREEN) | a83d4cf | fix(bootstrap): Validatoren auf `[[ =~ ]]` in beiden Skripten |

## Testverlauf

- RED (gefilterte Auswahl, alter Stand): 9 failed, 22 passed
  - Port LF, Registry LF, hex64 LF, Spike Port/Registry/hex64 LF, No-grep-Guard beide Skripte
- GREEN: `tests/unit/test_exapp_env_setup.py` komplett 182 passed

## Gates

- ruff check: grün
- ruff format --check: grün (362 Dateien)
- pyright (PYRIGHT_PYTHON_FORCE_VERSION=latest): 19 Fehler, alle `reportMissingImports` für docx/pptx/pypdf (optionale Extras nicht im frischen Worktree-venv), keiner in berührten Dateien, vorbestehend
- vulture src scripts vulture_whitelist.py: grün
- shellcheck: nicht installiert, übersprungen

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] CR-Testfälle erreichten den Validator unter Windows nicht**
- Found during: Task 2
- Issue: Git-for-Windows-bash entfernt rohe CR-Bytes aus dem `-c`-Skripttext. Der Fall "a secret with a carriage return" übergab dadurch einen sauberen Secret und war nach dem Fix weiter rot (falsch-rot im RED-Lauf ebenso).
- Fix: CR-Werte per ANSI-C-Quoting `$'...\r'` übergeben, bash expandiert das CR selbst. Port-CR-Fall dabei auf `$'23000\r'` (abschließendes CR, der relevante CRLF-Fall) statt `'23000\rX'` umgestellt.
- Positivkontrolle: "a generated secret" liefert 0, das CR-Secret 1, also kommt das CR an.
- Files modified: tests/unit/test_exapp_env_setup.py
- Commit: a83d4cf

**2. Commit-Aufteilung**
- Plan sah einen Commit vor, Auftrag verlangte atomare Commits pro Task: Test-Commit (RED) plus Fix-Commit.

### Hinweise

- Die CR-Fälle sind Regressions-Pins: das alte grep lehnte ein abschließendes CR hier bereits ab; rot im RED-Lauf waren nur die LF-Fälle und der Guard.
- Akzeptanz-grep `grep -cE "grep -Ez? '\^(https|\[0-9"` meldet je 1 Treffer außerhalb der vier Validatoren (Shared-Dir-Suffix-Prüfung nach `head -n1`, Zeile 783 bzw. 698). Dort ist der Wert schon einzeilig, außerhalb des Scopes, nicht geändert.

## Threat Flags

Keine neuen Angriffsflächen. T-ojn-01 bis T-ojn-03 umgesetzt, T-ojn-04 (Meldungen unverändert) bestätigt.

## Self-Check: PASSED

- scripts/bootstrap_exapp.sh: 4x `[[ "$value" =~ $re ]]`
- scripts/bootstrap_spike_opendesk.sh: 4x `[[ "$value" =~ $re ]]`
- Commits bef1cba und a83d4cf vorhanden, Autor street1983nk <k.cherif@outlook.de>, keine Trailer
