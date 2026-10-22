"""The admin switch of files_update reaches the tool registry of a started ExApp (issue #31).

``files_update`` registers itself only when ``config.files_update_enabled`` answers True at
the moment ``mcp_connector.server`` is imported. Until issue #31 ``entry_exapp`` imported
that module at its top, before ``main`` had resolved the administration settings, so the
deploy variable decided alone: a box ticked in Nextcloud left the tool missing, and a box
unticked there left an opt-in write tool armed.

The checks run in a fresh interpreter each, because the order of imports is the defect.
Inside this test process ``mcp_connector.server`` was imported long ago by other modules,
its registry is fixed, and an in-process start could never show a difference.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: One start of ``main`` in a fresh interpreter, with the network read of the admin values
#: and the data key answered locally and ``uvicorn.run`` replaced by a recorder. It prints
#: whether ``.server`` was already imported before ``main`` ran, and the tool names of the
#: registry the built application serves.
DRIVER = """
import asyncio, json, os, sys

from mcp_connector import entry_exapp
from mcp_connector.exapp import config_values
from mcp_connector.oauth import store

imported_before_main = "mcp_connector.server" in sys.modules

overlay = json.loads(os.environ.pop("TEST_ADMIN_OVERLAY"))

async def fake_admin_values(env):
    return config_values.AdminValues(overlay=dict(overlay), refused=frozenset())

async def fake_key(env=None):
    return bytes(range(32))

entry_exapp._admin_values = fake_admin_values
store.crypto.data_key = fake_key
entry_exapp.uvicorn.run = lambda app, **kwargs: None

entry_exapp.main()

from mcp_connector.server import mcp

names = sorted(tool.name for tool in asyncio.run(mcp.list_tools()))
print(json.dumps({"imported_before_main": imported_before_main, "tools": names}))
"""

ENV_FILES_UPDATE = "NC_MCP_FILES_UPDATE"


def started_tools(tmp_path: Path, *, deploy: str | None, admin: str | None) -> dict[str, object]:
    """Start the ExApp once with this deploy variable and this stored admin value."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("NC_MCP_", "APP_", "NEXTCLOUD_", "AA_", "HP_"))
    }
    env.update(
        {
            "APP_ID": "mcp_connector",
            "APP_SECRET": "s3cret-of-the-test",
            "APP_VERSION": "0.6.0",
            "NEXTCLOUD_URL": "http://nc.test",
            "APP_PERSISTENT_STORAGE": str(tmp_path),
            "HP_SHARED_KEY": "a" * 64,
            "NC_MCP_DISABLE_DNS_REBINDING_PROTECTION": "1",
            "PYTHONUTF8": "1",
            "TEST_ADMIN_OVERLAY": json.dumps({} if admin is None else {ENV_FILES_UPDATE: admin}),
        }
    )
    if deploy is not None:
        env[ENV_FILES_UPDATE] = deploy
    result = subprocess.run(  # noqa: S603 - a literal driver from this test, no user input
        [sys.executable, "-c", DRIVER],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_the_entry_point_registers_no_tool(tmp_path: Path) -> None:
    """The defect itself: nothing may fix the registry before ``main`` has resolved."""
    started = started_tools(tmp_path, deploy=None, admin=None)

    assert started["imported_before_main"] is False


@pytest.mark.parametrize(
    ("deploy", "admin", "expected"),
    [
        # The report of issue #31: deploy off, the box ticked in Nextcloud.
        ("off", "on", True),
        # The other direction, and the one that matters for safety: an unticked box disarms
        # the write tool even when the deploy environment armed it.
        ("on", "off", False),
        # A store installation sets no variable at all; the stored value alone decides.
        (None, "on", True),
        # Nothing stored: the deploy variable keeps deciding, exactly as before.
        ("on", None, True),
        (None, None, False),
    ],
)
def test_the_resolved_switch_decides_whether_files_update_is_served(
    tmp_path: Path, deploy: str | None, admin: str | None, expected: bool
) -> None:
    started = started_tools(tmp_path, deploy=deploy, admin=admin)
    tools = started["tools"]
    assert isinstance(tools, list)

    assert ("files_update" in tools) is expected
