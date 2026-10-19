---
phase: quick-261019-ojn
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - tests/unit/test_exapp_env_setup.py
  - scripts/bootstrap_exapp.sh
  - scripts/bootstrap_spike_opendesk.sh
autonomous: true
requirements: [QUICK-261019-ojn]

must_haves:
  truths:
    - "A registration input with an embedded LF or CR is refused by all four validators on GNU and BSD alike (no external grep involved)"
    - "Every value accepted before is still accepted, every error message is character-identical"
    - "bootstrap_exapp.sh and bootstrap_spike_opendesk.sh carry the same newline-safe validator bodies"
    - "DaniW42's failing macOS case 'a public address with a second line' no longer depends on GNU grep -z"
  artifacts:
    - path: "scripts/bootstrap_exapp.sh"
      provides: "require_hex64, require_port_number, require_registry_shape, require_url_shape via [[ =~ ]]"
      contains: '[[ "$value" =~ $re ]]'
    - path: "scripts/bootstrap_spike_opendesk.sh"
      provides: "the same four validators, same form"
      contains: '[[ "$value" =~ $re ]]'
    - path: "tests/unit/test_exapp_env_setup.py"
      provides: "multiline cases for port/registry/hex64, spike-script coverage, no-grep guard"
  key_links:
    - from: "tests/unit/test_exapp_env_setup.py::shell_function"
      to: "scripts/*.sh validator definitions"
      via: "'name() {' opening line + column-one '}' closing"
      pattern: "^require_(hex64|port_number|registry_shape|url_shape)\\(\\) \\{$"
---

<objective>
Make the four bootstrap input validators newline-safe and portable to BSD grep (macOS) by replacing their `printf | grep -E/-Ez` pipelines with bash-native whole-string ERE matching, in both bootstrap scripts, and pin the behaviour with tests.

Purpose: PR #26 (DaniW42, macOS) showed `grep -Ez` degrades to line matching on BSD grep, so `https://ok.example.com` + LF + `"evil":1` passes require_url_shape. The other three validators use plain line-based `grep -E` and accept a multiline value on Linux too, which defeats the IN-07/WR-03 JSON-injection pinning.
Output: patched validators in scripts/bootstrap_exapp.sh and scripts/bootstrap_spike_opendesk.sh, new test cases in tests/unit/test_exapp_env_setup.py, one commit.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

<interfaces>
Current validator bodies (identical in both scripts except the registry variable name in the message):

scripts/bootstrap_exapp.sh lines 1009-1058, scripts/bootstrap_spike_opendesk.sh lines 924-973:
- require_hex64 name value origin: `printf '%s' "$value" | grep -E '^[0-9a-f]{64}$'`
- require_port_number name value: `grep -E '^[0-9]+$'`
- require_registry_shape value: `grep -E '^[0-9A-Za-z_.:-]+$'` (message names NC_EXAPP_REGISTRY in bootstrap_exapp.sh, NC_SPIKE_OD_REGISTRY in the spike script)
- require_url_shape name value: `grep -Ez '^https?://[A-Za-z0-9._:-]+(/[A-Za-z0-9._/-]*)?$'`
Each has the shape `if ! <pipeline> >/dev/null; then echo ... >&2; return 1; fi`.

tests/unit/test_exapp_env_setup.py:
- shell_function(name, path=None) at line 337: cuts from "\n{name}() {\n" to the first "\n}\n"; path defaults to BOOTSTRAP. Rewritten bodies MUST keep `name() {` on its own line and the closing `}` in column one, and MUST NOT contain a line that is exactly `}` before the end (an inner `fi` is fine).
- run_bash(script) at line 471.
- test_the_registration_inputs_are_pinned_before_json_info at lines 1223-1280: parametrize ("call", "expected_code") with a parallel ids list of short English phrases; the script defines require_port_number, require_registry_shape, require_url_shape via shell_function and runs `call` under `set -euo pipefail`.
- GOOD_SECRET = "0123456789abcdef" * 4 at line 1573; require_hex64 is only exercised indirectly through app_secret (lines 1588-1640), there is no parametrized require_hex64 block.
- test_the_bootstrap_calls_every_registration_validator and test_every_registration_validator_runs_before_json_info_is_built grep the CALL sites; call sites do not change.
- @needs_bash decorator marks tests that need a bash.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Add failing multiline cases and a no-grep guard (RED)</name>
  <files>tests/unit/test_exapp_env_setup.py</files>
  <read_first>
    - tests/unit/test_exapp_env_setup.py lines 330-370 (shell_function), 465-490 (run_bash, needs_bash), 1223-1300 (the parametrize block and the call-site tests), 1560-1650 (WEAK_SECRETS, GOOD_SECRET, app_secret tests)
  </read_first>
  <behavior>
    - "a port with a second line": `require_port_number NC_EXAPP_APP_PORT '23000\n"system":true'` -> 1
    - "a port with a carriage return": `require_port_number NC_EXAPP_APP_PORT '23000\rX'` -> 1
    - "a registry with a second line": `require_registry_shape '127.0.0.1:5000\nX'` -> 1
    - require_hex64 direct: GOOD_SECRET -> 0; GOOD_SECRET + "\n" + GOOD_SECRET -> 1; GOOD_SECRET + "\r" -> 1; GOOD_SECRET.upper() -> 1
    - The same multiline cases (port LF, registry LF, url LF, hex64 LF) are refused when the functions are cut from bootstrap_spike_opendesk.sh
    - None of the four validator bodies in either script contains the word "grep"
  </behavior>
  <action>
    1. In the parametrize list of test_the_registration_inputs_are_pinned_before_json_info insert, right after "a port with trailing whitespace", the two port cases ("a port with a second line", "a port with a carriage return") and, right after "an empty registry", "a registry with a second line", each with expected_code 1. Write them in the same style as the existing url LF case (Python "\n" / "\r" escapes inside the call string, so bash receives a literal LF/CR inside the single-quoted argument). Keep the ids list in the exact same order as the tuples.
    2. Add a module-level constant near GOOD_SECRET or near the parametrize block: SPIKE_BOOTSTRAP = BOOTSTRAP.parent / "bootstrap_spike_opendesk.sh" (check first whether BOOTSTRAP is a Path to scripts/bootstrap_exapp.sh; reuse an existing spike constant if one exists).
    3. Add a new @needs_bash test test_require_hex64_refuses_anything_but_one_hex_line, parametrized ("value", "expected_code") with ids "a generated secret", "a secret with a second line", "a secret with a carriage return", "an upper case secret". Build the script as "set -euo pipefail\n" + shell_function('require_hex64') + a call line `require_hex64 APP_SECRET '<value>' .env.exapp`; assert returncode == expected_code with stderr as message. Place it after test_a_missing_env_file_still_generates_a_fresh_secret.
    4. Add a new @needs_bash test test_the_spike_validators_refuse_multiline_values, parametrized over ("call", "expected_code") with: a plain port (0), a port with a second line (1), a registry with a second line using NC_SPIKE_OD_REGISTRY context (call is `require_registry_shape '127.0.0.1:5000\nX'`, 1), a public address with a second line (1), a secret with a second line (1). The script defines all four functions via shell_function(name, SPIKE_BOOTSTRAP). Docstring: both scripts carry the same validators, so the spike copy must not keep the line-based hole.
    5. Add a plain (no bash needed) test test_the_registration_validators_do_not_shell_out_to_grep, parametrized over both script paths (ids "the bootstrap", "the openDesk spike"), asserting for each of the four names that "grep" not in shell_function(name, path). Docstring: grep is line-based everywhere and -z is GNU only (PR #26 on macOS), so a multiline value must be judged by bash's whole-string match.
    6. Run the module: the new port/registry/hex64 LF and CR cases and the no-grep guard MUST fail now (RED); existing cases stay green. Do not commit yet.
  </action>
  <verify>
    <automated>uv run pytest tests/unit/test_exapp_env_setup.py -q -k "pinned_before_json_info or require_hex64 or spike_validators or shell_out_to_grep"</automated>
  </verify>
  <done>New cases exist with short English ids; the multiline/CR cases for port, registry, hex64 and the no-grep guard fail against the unpatched scripts; every pre-existing case still passes.</done>
  <acceptance_criteria>
    - grep -c "a port with a second line" tests/unit/test_exapp_env_setup.py >= 2
    - grep -c "a registry with a second line" tests/unit/test_exapp_env_setup.py >= 2
    - grep -c "def test_require_hex64_refuses_anything_but_one_hex_line" tests/unit/test_exapp_env_setup.py == 1
    - grep -c "def test_the_registration_validators_do_not_shell_out_to_grep" tests/unit/test_exapp_env_setup.py == 1
    - pytest run shows failures only in the newly added multiline/CR/no-grep cases
  </acceptance_criteria>
</task>

<task type="auto">
  <name>Task 2: Switch all four validators in both scripts to whole-string [[ =~ ]] (GREEN), gates, commit</name>
  <files>scripts/bootstrap_exapp.sh, scripts/bootstrap_spike_opendesk.sh</files>
  <read_first>
    - scripts/bootstrap_exapp.sh lines 1000-1060
    - scripts/bootstrap_spike_opendesk.sh lines 915-975
  </read_first>
  <action>
    In BOTH scripts rewrite the four function bodies the same way, leaving the function names, argument handling, every echo line and the return 1 unchanged:
    - Declare the pattern in a local single-quoted variable on its own line: `local re='<pattern>'` (for require_hex64 add it next to the existing local line, same for the others).
    - Replace the condition `! printf '%s' "$value" | grep -E... >/dev/null` with `! [[ "$value" =~ $re ]]`. The right-hand side MUST be the unquoted variable: that is the form bash 3.2 (macOS /bin/bash) and bash 5 both treat as a regex; a quoted or inline pattern would be literal on 3.2+ or need escaping.
    - Patterns, byte-exact: hex64 `^[0-9a-f]{64}$`, port `^[0-9]+$`, registry `^[0-9A-Za-z_.:-]+$`, url `^https?://[A-Za-z0-9._:-]+(/[A-Za-z0-9._/-]*)?$`.
    - Extend the comment block above require_url_shape (and the IN-07 block above require_port_number) by one or two English sentences: the check is bash's own ERE match against the whole value, so ^ and $ anchor at the string edges and an embedded LF or CR fails closed; grep is line-based and its -z switch is GNU only, which let a two-line address through on macOS (PR #26). No em-dashes.
    Keep `name() {` on its own line and the closing `}` in column one so shell_function still cuts the bodies exactly. Do not touch call sites.
    Then run the gates serially (under 4 GB free RAM never in parallel):
    1. uv run pytest tests/unit/test_exapp_env_setup.py -q (all green, including Task 1 cases and the call-site tests)
    2. uv run ruff check .
    3. uv run ruff format --check . (if it fails on the test file run uv run ruff format tests/unit/test_exapp_env_setup.py and re-check)
    4. PYRIGHT_PYTHON_FORCE_VERSION=latest uv run pyright
    5. uv run vulture src scripts vulture_whitelist.py
    6. If shellcheck is on PATH: shellcheck scripts/bootstrap_exapp.sh scripts/bootstrap_spike_opendesk.sh must not report new findings in the touched lines (skip silently if not installed).
    Commit both scripts and the test file with author street1983nk <k.cherif@outlook.de> (git -c user.name=street1983nk -c user.email=k.cherif@outlook.de commit), message `fix(bootstrap): pin registration inputs against multiline values on any grep`, no Co-Authored-By or other Claude trailers. Do not push.
  </action>
  <verify>
    <automated>uv run pytest tests/unit/test_exapp_env_setup.py -q && uv run ruff check . && uv run ruff format --check .</automated>
  </verify>
  <done>Whole test module green, ruff/pyright/vulture clean, both scripts free of grep inside the four validators, one commit authored by street1983nk without trailers.</done>
  <acceptance_criteria>
    - grep -c '\[\[ "\$value" =~ \$re \]\]' scripts/bootstrap_exapp.sh == 4
    - grep -c '\[\[ "\$value" =~ \$re \]\]' scripts/bootstrap_spike_opendesk.sh == 4
    - grep -cE "grep -Ez? '\^(https|\[0-9" scripts/bootstrap_exapp.sh scripts/bootstrap_spike_opendesk.sh reports 0 for both files
    - grep -c "is not a plain http or https address." in each script unchanged (1)
    - git log -1 --format='%an <%ae>%n%B' shows street1983nk <k.cherif@outlook.de> and no "Co-Authored-By"
  </acceptance_criteria>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| calling shell / env file -> bootstrap | NC_EXAPP_APP_PORT, NC_EXAPP_REGISTRY, PUBLIC_URL, APP_SECRET/HP_SHARED_KEY are overridable and land unescaped in the json_info registration payload |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-ojn-01 | Tampering | require_url_shape on BSD grep | mitigate | whole-string `[[ "$value" =~ $re ]]`, LF case tested on every platform |
| T-ojn-02 | Tampering | require_port_number / require_registry_shape / require_hex64 | mitigate | same whole-string match; LF and CR cases added for port, registry, hex64 |
| T-ojn-03 | Tampering | spike script copy drifting from the main bootstrap | mitigate | spike validators run through the same multiline cases plus a no-grep guard over both scripts |
| T-ojn-04 | Information disclosure | error messages | accept | messages unchanged; url value is still not echoed |
</threat_model>

<verification>
- uv run pytest tests/unit/test_exapp_env_setup.py -q green
- ruff check, ruff format --check, pyright (PYRIGHT_PYTHON_FORCE_VERSION=latest), vulture clean
- No grep inside the four validators in either script
</verification>

<success_criteria>
- Multiline and CR values are refused by all four validators in both scripts without relying on GNU grep
- All previously accepted values still pass, messages unchanged
- One local commit by street1983nk, no Claude trailers
</success_criteria>

<output>
Create `.planning/quick/261019-ojn-bootstrap-validators-newline-safe-and-bs/261019-ojn-SUMMARY.md` when done
</output>
