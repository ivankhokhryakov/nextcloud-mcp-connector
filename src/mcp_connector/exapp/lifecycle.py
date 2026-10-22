"""The three endpoints AppAPI calls: ``/heartbeat``, ``/init`` and ``/enabled``.

Deliberate deviation from the research sketch, which registered these with the custom
route decorator of the shared server object: they are handed out by a factory here and
attached by ``entry_exapp`` alone. A registration on the singleton would make
``/heartbeat``, ``/init`` and ``/enabled`` appear in the standalone HTTP server of phase 1
as soon as anything imports this module, and D-23 requires that stdio and that server stay
exactly as they were. The cost is one line in ``build_exapp_app``; the benefit is that the
two phase 1 modes cannot grow a lifecycle endpoint by accident.

Three rules that come straight from the AppAPI source:

* ``/heartbeat`` authenticates nothing. Non HaRP daemons send no headers at all, and a 401
  there makes the registration run into its ten minute timeout (pitfall 10). It answers
  ``{"status": "ok"}`` and nothing else, no version, no configuration (T-02-06).
* Every answer carries ``Cache-Control: no-store``. The PHP proxy caches JSON answers for
  3600 seconds whenever the answer does not say otherwise (pitfall 4, T-02-07).
* ``x-origin-ip`` means the request came through the PHP proxy, which does not protect
  these paths and attaches valid AppAPI headers itself. Such a request is answered with
  404 as defense in depth; the primary protection is the narrow route declaration in
  ``appinfo/info.xml`` (pitfall 2, T-02-04).
"""

import logging
import os
import signal
from collections.abc import Callable, Mapping

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..errors import ToolError
from . import admin_settings, config_values, occ, settings_form, status
from .auth import AppApiRejected, require_appapi
from .responses import NO_STORE, json_response

__all__ = ["lifecycle_routes"]

#: The only two values AppAPI ever sends as ``?enabled=``.
ENABLED_VALUES = ("0", "1")

#: Reported once, right after a successful ``/init``. There is nothing to do in between.
INIT_PROGRESS = 100

#: Set on the proxy path, never by HaRP and never by a client that reached us directly.
HEADER_ORIGIN_IP = "x-origin-ip"

logger = logging.getLogger("mcp_connector.exapp.lifecycle")


def _restart_this_process() -> None:
    """Ask the server of this process to stop, so the container starts once more.

    uvicorn handles SIGTERM with a graceful shutdown, the process is PID 1 of the container
    (``start.sh`` ends in ``exec``), and AppAPI creates ExApp containers with the restart
    policy ``unless-stopped`` unless an administrator changed ``container_restart_policy``.
    """
    signal.raise_signal(signal.SIGTERM)


async def _stored_values_differ(env: Mapping[str, str] | None) -> bool:
    """Whether the stored admin values are not the ones this process serves with (#33).

    ``occ app_api:app:update`` disables the app, deploys the new container and enables the
    app only after ``/init``. The new container therefore reads its admin values while the
    app is still disabled, AppAPI answers that read with 401, and the deploy environment is
    what the process was built with. ``enableExAppInternal`` sets the app to enabled before it
    calls ``/enabled``, so the same read succeeds here, and a difference means the start
    missed what the administrator stored. A failed read here is no difference: it logs its
    own line and changes nothing, exactly as at the start.
    """
    overlay, _ = await config_values.admin_values(env=env)
    in_force = os.environ if env is None else env
    return any(in_force.get(name) != value for name, value in overlay.items())


def lifecycle_routes(
    env: Mapping[str, str] | None = None,
    restart: Callable[[], None] = _restart_this_process,
) -> list[Route]:
    """Build the three AppAPI routes against one environment.

    The environment is a parameter for the same reason ``entry_http.build_app`` takes one:
    it lets every test build its own application without touching the process environment.
    ``restart`` is a parameter for the same reason: a test records the call instead of
    stopping its own interpreter.
    """

    async def heartbeat(request: Request) -> Response:
        """Liveness for the AppAPI registration. Unauthenticated by contract."""
        return json_response({"status": "ok"})

    async def init(request: Request) -> Response:
        """Accept the install call, then report progress 100 out of band."""
        guarded = _guard(request, env)
        if isinstance(guarded, Response):
            return guarded

        try:
            await status.report_init_progress(INIT_PROGRESS, env=env)
        except Exception:
            # Nothing that happens on the way to Nextcloud may turn this into a 500:
            # AppAPI aborts the whole installation on a failing /init (pitfall 3).
            logger.error("the init progress push failed, the installation may stay below 100")
        return json_response({})

    async def enabled(request: Request) -> Response:
        """Confirm the enable or disable call with an empty ``error`` field."""
        guarded = _guard(request, env)
        if isinstance(guarded, Response):
            return guarded

        value = request.query_params.get("enabled", "")
        if value not in ENABLED_VALUES:
            return json_response({"error": "enabled must be 0 or 1"}, status_code=400)

        if value == "1":
            try:
                await settings_form.register_settings_form(env=env)
            except Exception:
                # Same asymmetry as the init progress push: a non empty error field makes
                # AppAPI disable the app again at once, while a missing signpost costs
                # discoverability and this one line (pitfall 11).
                logger.error("the settings form registration failed, the signpost is missing")
            try:
                await admin_settings.register_admin_form(env=env)
            except Exception:
                # Its own try block, and not a second statement in the one above: the two
                # forms are independent, so a failure of one may not cost the other. The
                # reason for tolerating either failure is pitfall 11 again, and it weighs
                # more here: without the admin form an administrator cannot enter the
                # public address at all, and an app AppAPI disabled again cannot be
                # configured either.
                logger.error("the admin form registration failed, the admin settings are missing")
            try:
                await admin_settings.retire_legacy_admin_form(env=env)
            except Exception:
                logger.error("the removal of the old admin form failed, it may show twice")
            try:
                await occ.register_occ_commands(env=env)
            except Exception:
                # The third independent registration, in its own try for the same reason the
                # second one has one. The tolerance is pitfall 11 again: without the commands
                # an administrator has to fall back to the runbook, while an app AppAPI
                # disabled again cannot be purged or checked at all.
                #
                # This block covers both commands since plan 18-08, and only as their common
                # last resort: register_occ_commands has a try per command inside it, so a
                # single refused registration never reaches here and never costs the other
                # command. What lands here is a failure of the whole call, which is why the
                # line says both are missing.
                logger.error(
                    "the occ command registration failed, the purge and the audit log check "
                    "are missing"
                )
            # Last, after every registration, and answered before the restart: AppAPI only
            # learns that the enable worked from this answer, and the restart runs as a
            # background task once it is sent (issue #33).
            try:
                differ = await _stored_values_differ(env)
            except Exception:
                logger.error("the admin values could not be compared on enable")
                differ = False
            if differ:
                logger.warning(
                    "the administration settings of this app differ from the values this "
                    "process started with, which happens when the start ran while the app "
                    "was still disabled, as during an update. The process stops now so the "
                    "container starts again and reads them."
                )
                response = json_response({"error": ""})
                response.background = BackgroundTask(restart)
                return response
        # enabled=0 registers nothing, unregisters nothing and above all destroys nothing.
        # AppAPI hands out the forms of enabled apps only, so a disabled app disappears from
        # the settings page by itself, and an uninstall is cleaned up on AppAPI's side
        # (measured, 04-RESEARCH.md). Cleaning up the data of this app here would look
        # natural, because the Remove button of Nextcloud 34 fires exactly this branch, and
        # it would be a catastrophe: the same branch runs on every update
        # (lib/Command/ExApp/Update.php) and on every ordinary disable, so every connection
        # of every user would be gone after an update. The instance wide purge is therefore
        # an explicit administrative act (exapp/purge.py, plan 05-06, T-05-28).
        return json_response({"error": ""})

    return [
        Route("/heartbeat", heartbeat, methods=["GET"]),
        Route("/init", init, methods=["POST"]),
        Route("/enabled", enabled, methods=["PUT"]),
    ]


def _guard(request: Request, env: Mapping[str, str] | None) -> str | Response:
    """Return the Nextcloud user id of this request, or the response that ends it.

    A response instead of an exception, so the handlers keep one straight control flow and
    no rejection can escape as a 500.
    """
    if HEADER_ORIGIN_IP in request.headers:
        return _text("Not Found", status_code=404)
    try:
        return require_appapi(request, env=env)
    except (AppApiRejected, ToolError):
        # No detail, no WWW-Authenticate: the caller is a proxy, and every hint here would
        # only tell an attacker which of the checks rejected the request (T-02-03).
        #
        # ToolError is in the tuple because require_appapi reads the deploy environment on
        # every call and exapp_settings raises it when a variable is missing (IN-02). main
        # validates that at startup, so the branch is unreachable in a deployed process,
        # but a boundary that cannot decide has to refuse rather than answer 500.
        return json_response({}, status_code=401)


def _text(body: str, status_code: int) -> Response:
    return Response(body, status_code=status_code, media_type="text/plain", headers=NO_STORE)
