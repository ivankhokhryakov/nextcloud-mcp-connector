"""One file id lookup per ``prepare_context`` bundle, byte for byte like the lookup per excerpt.

Plan 27-09 closes the wall clock gap of 27-VERIFICATION: an excerpt of ``detail="full"``
used to cost three serial round trips (the file id SEARCH of ``find_by_fileid``, the stat
PROPFIND of ``files.read``, the GET), three times in parallel. Now one SEARCH resolves every
file excerpt of the bundle, and ``files.read`` takes the entry of that SEARCH as ``known``
instead of asking PROPFIND for the same facts a second time.

Fewer lookups must not become an oracle (T-27-90). So the load bearing tests here are pairs:
the same bundle, once on the new route and once on the **old** one, compared as
``json.dumps(..., sort_keys=True)``. The old route is the code before this plan, forced
through the two seams the plan keeps: the batch lookup refuses (so every excerpt goes the
single route of ``fetch``) and ``files.read`` loses its ``known`` (so it asks the stat
again). Beside the pairs stand the request counts on the wire, the guard that ``known`` never
skips (T-27-92), and the proof that nothing survives the call (E3, T-27-91).

No ``patch_untagged`` here: every guard state is mocked on its real routes.
"""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import guard_routes
import httpx
import pytest
import respx

from mcp_connector.errors import ToolError
from mcp_connector.nextcloud import NcClients, capabilities
from mcp_connector.nextcloud.clients import dav
from mcp_connector.nextcloud.clients import xml as davxml
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import calendar as calendar_tools
from mcp_connector.tools import chatgpt as chatgpt_tools
from mcp_connector.tools import context as context_tools
from mcp_connector.tools import files as files_tools
from mcp_connector.tools import mail as mail_tools
from mcp_connector.tools import notes as notes_tools
from mcp_connector.tools import search as search_tools
from mcp_connector.tools import talk as talk_tools
from mcp_connector.tools import withhold

BASE = guard_routes.BASE
USER = guard_routes.USER
DAV_ROOT = f"{BASE}/remote.php/dav/"
FILES_ROOT = f"{BASE}/remote.php/dav/files/{USER}"
WINDOW = ("2026-09-27T10:00:00+00:00", "2026-10-04T10:00:00+00:00")

_LITERAL = re.compile(rb"<d:literal>([^<]*)</d:literal>")


@dataclass(frozen=True)
class Node:
    """One file or folder of the mocked instance."""

    path: str
    content: bytes = b""
    content_type: str = "text/plain"
    folder: bool = False


#: The instance every test reads from. Three text files, a folder, a PDF, a large file,
#: a file below a folder the ``active`` tests tag, and a file outside ``/Docs``.
NODES: dict[str, Node] = {
    "11": Node("/Docs/eins.txt", b"Erster offener Text.\n"),
    "12": Node("/Docs/zwei.md", "Zweiter Text mit Umlauten: Straße, Übergabe.\n".encode()),
    "13": Node("/Docs/drei.txt", b"Dritter Text.\n"),
    "14": Node("/Docs/Ordner", folder=True),
    "15": Node("/Docs/scan.pdf", b"%PDF-1.7", content_type="application/pdf"),
    "16": Node("/Docs/lang.txt", b"x" * 9000),
    "17": Node("/Projekt/innen.txt", b"Unter dem getaggten Ordner.\n"),
    "18": Node("/Andere/draussen.txt", b"Ausserhalb der Wurzel.\n"),
    # The files of two notes (a note id is the file id of its file, K4). 27-10.
    "31": Node("/Notes/Offen/lesbar.md", b"Lesbare Notiz.\n"),
    "32": Node("/Notes/Geheim/geheim.md", b"Geheime Notiz.\n"),
}

#: The tagged folder of the ``active`` cases (it holds node 17).
TAGGED_FOLDER = ("Projekt", "900", True)

#: A tagged category folder of notes (it holds note 32), as in test_notes_exclusion.py.
GEHEIM = ("Notes/Geheim", "932", True)

#: The notes the Notes app knows: id onto (category, content). Any other id answers 404.
NOTES: dict[str, tuple[str, str]] = {
    "31": ("Offen", "Inhalt der lesbaren Notiz 31.\n"),
    "32": ("Geheim", "Inhalt der geheimen Notiz 32.\n"),
}

CAPABILITIES_URL = f"{BASE}/ocs/v2.php/cloud/capabilities"
NOTES_BASE = f"{BASE}/index.php/apps/notes/api/v1/notes"


def literals(body: bytes) -> list[str]:
    return [match.decode() for match in _LITERAL.findall(body)]


def entry_xml(fileid: str, node: Node) -> str:
    href = f"/remote.php/dav/files/{USER}{node.path}"
    size = "" if node.folder else f"<d:getcontentlength>{len(node.content)}</d:getcontentlength>"
    kind = "<d:collection/>" if node.folder else ""
    ctype = "httpd/unix-directory" if node.folder else node.content_type
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
        f"<d:displayname>{node.path.rsplit('/', 1)[-1]}</d:displayname>"
        f"<d:getcontenttype>{ctype}</d:getcontenttype>{size}"
        f"<d:getlastmodified>Thu, 14 Aug 2026 10:00:00 GMT</d:getlastmodified>"
        "<d:getetag>&quot;etag-1&quot;</d:getetag>"
        f"<d:resourcetype>{kind}</d:resourcetype><oc:fileid>{fileid}</oc:fileid>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def _props_of(response_xml: str) -> dict[str, str]:
    """The props of one ``d:response`` as ``xml.parse_multistatus`` reads them."""
    body = multistatus(response_xml).content
    return davxml.parse_multistatus(body)[0][1]


def multistatus(responses: str) -> httpx.Response:
    return httpx.Response(
        207,
        text='<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        f'xmlns:oc="http://owncloud.org/ns">{responses}</d:multistatus>',
    )


def stat_xml(fileid: str, node: Node) -> str:
    href = f"/remote.php/dav/files/{USER}{node.path}"
    size = "" if node.folder else f"<d:getcontentlength>{len(node.content)}</d:getcontentlength>"
    kind = "<d:collection/>" if node.folder else ""
    ctype = "httpd/unix-directory" if node.folder else node.content_type
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{size}"
        f"<d:getcontenttype>{ctype}</d:getcontenttype>"
        "<d:getlastmodified>Thu, 14 Aug 2026 10:00:00 GMT</d:getlastmodified>"
        "<d:getetag>&quot;etag-1&quot;</d:getetag>"
        f"<d:resourcetype>{kind}</d:resourcetype><oc:fileid>{fileid}</oc:fileid>"
        "<oc:permissions>RGDNVW</oc:permissions>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


@dataclass
class Instance:
    """The mocked Nextcloud of one run: the nodes, the guard state, the SEARCH behaviour.

    ``search`` decides what a file id SEARCH answers: ``"ok"``, ``"fail_batch"`` (500 for
    a body with more than one id, the single lookups still answer), ``"fail_all"`` (500
    for every body) or ``"hang"`` (never answers). ``gone`` lists ids the SEARCH no
    longer finds. ``crowd`` doubles the first entry of a body with more than one id and
    cuts the answer at ``nresults``, so the last id of that block is crowded out (27-09
    deviation 1); a single lookup still finds it.
    """

    guard: str = "active"
    tagged: tuple[tuple[str, str, bool], ...] = (TAGGED_FOLDER,)
    search: str = "ok"
    gone: tuple[str, ...] = ()
    crowd: bool = False
    searches: list[list[str]] = field(default_factory=list)
    before_search: Callable[[], Any] | None = None

    async def _search(self, request: httpx.Request) -> httpx.Response:
        asked = literals(request.content)
        self.searches.append(asked)
        if self.before_search is not None:
            await self.before_search()
        if self.search == "hang":
            await asyncio.sleep(3600)
        if self.search == "fail_all" or (self.search == "fail_batch" and len(asked) > 1):
            return httpx.Response(500)
        found = [fileid for fileid in asked if fileid in NODES and fileid not in self.gone]
        if self.crowd and len(asked) > 1 and found:
            found = [found[0], *found][: len(asked)]
        return multistatus("".join(entry_xml(fileid, NODES[fileid]) for fileid in found))

    @staticmethod
    def _note(request: httpx.Request) -> httpx.Response:
        note_id = request.url.path.rsplit("/", 1)[-1]
        if note_id not in NOTES:
            return httpx.Response(404, json={"status": 404, "message": "Note not found"})
        category, content = NOTES[note_id]
        return httpx.Response(
            200,
            json={
                "id": int(note_id),
                "title": f"Notiz {note_id}",
                "content": content,
                "category": category,
                "favorite": False,
                "modified": 1789584914,
            },
        )

    @staticmethod
    def _node_of(request: httpx.Request) -> tuple[str, Node] | None:
        path = request.url.path.removeprefix(f"/remote.php/dav/files/{USER}")
        return next(((i, n) for i, n in NODES.items() if n.path == path), None)

    def _stat(self, request: httpx.Request) -> httpx.Response:
        found = self._node_of(request)
        if found is None:
            return httpx.Response(404)
        return multistatus(stat_xml(*found))

    def _get(self, request: httpx.Request) -> httpx.Response:
        found = self._node_of(request)
        if found is None:
            return httpx.Response(404)
        return httpx.Response(200, content=found[1].content)

    def mount(self, mock: respx.MockRouter) -> dict[str, respx.Route]:
        routes: dict[str, respx.Route] = {}
        if self.guard == "untagged":
            routes["tags"] = guard_routes.untagged(mock)
        elif self.guard == "unverifiable":
            routes["tags"], routes["report"] = guard_routes.unverifiable(mock)
        else:
            routes["tags"], routes["report"] = guard_routes.active(mock, *self.tagged)
        routes["search"] = mock.route(method="SEARCH", url=DAV_ROOT).mock(side_effect=self._search)
        routes["propfind"] = mock.route(method="PROPFIND", url__startswith=FILES_ROOT).mock(
            side_effect=self._stat
        )
        routes["get"] = mock.route(method="GET", url__startswith=FILES_ROOT).mock(
            side_effect=self._get
        )
        routes["capabilities"] = mock.get(CAPABILITIES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ocs": {
                        "meta": {"status": "ok", "statuscode": 200, "message": "OK"},
                        "data": {
                            "capabilities": {
                                "core": {},
                                "notes": {"api_version": ["1.3"], "version": "6.1.0"},
                            }
                        },
                    }
                },
            )
        )
        routes["notes"] = mock.route(method="GET", url__startswith=NOTES_BASE).mock(
            side_effect=self._note
        )
        return routes


def fresh() -> NcClients:
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False),
        creds=Credentials(BASE, USER, "app-password-test"),
    )


def dumped(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def triple(error: ToolError) -> tuple[str, str, str]:
    return (error.message, error.hint, error.reason)


def file_hit(fileid: str) -> dict[str, Any]:
    node = NODES.get(fileid, Node(f"/weg-{fileid}.txt"))
    return {
        "id": f"file:{fileid}",
        "title": node.path.rsplit("/", 1)[-1],
        "subline": "in Docs",
        "url": f"{BASE}/index.php/f/{fileid}",
        "provider": "files",
        "kind": "file",
    }


def other_hit(identifier: str, kind: str, provider: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "title": identifier,
        "subline": "",
        "url": f"{BASE}/index.php/apps/{provider}",
        "provider": provider,
        "kind": kind,
    }


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    guard_routes.reset()
    capabilities.clear_cache()
    monkeypatch.setattr(context_tools, "_window", lambda: WINDOW)


@pytest.fixture(autouse=True)
def _quiet_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calendar, Talk and mail carry no file excerpt; they answer empty without a request."""

    async def no_events(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"range": {"start": "", "end": "", "timezone": "UTC"}, "count": 0, "events": []}

    async def no_rooms(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"level": "conversations", "count": 0, "results": []}

    async def no_accounts(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"level": "accounts", "count": 0, "results": []}

    monkeypatch.setattr(calendar_tools, "list_events", no_events)
    monkeypatch.setattr(talk_tools, "browse", no_rooms)
    monkeypatch.setattr(mail_tools, "browse", no_accounts)


def wire_search(monkeypatch: pytest.MonkeyPatch, hits: list[dict[str, Any]]) -> None:
    async def answer(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "query": "docs",
            "count": len(hits),
            "results": [dict(hit) for hit in hits],
            "note": search_tools.SEARCH_NOTE,
        }

    monkeypatch.setattr(search_tools, "unified_search", answer)


def force_old_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code before plan 27-09: one SEARCH per excerpt, and the stat inside ``read``."""

    async def no_batch(_clients: NcClients, _identifiers: Any, **_kinds: Any) -> Any:
        raise ToolError(
            message="the batch lookup is switched off for the reference run", hint="none"
        )

    real_read = files_tools.read

    async def read_with_stat(
        clients: NcClients,
        path: str,
        offset: int = 0,
        max_bytes: int = files_tools.DEFAULT_MAX_BYTES,
        **_known: Any,
    ) -> dict[str, Any]:
        return await real_read(clients, path, offset, max_bytes)

    monkeypatch.setattr(chatgpt_tools, "file_entries", no_batch)
    monkeypatch.setattr(files_tools, "read", read_with_stat)


@dataclass
class Run:
    answer: dict[str, Any]
    counts: dict[str, int]
    searches: list[list[str]]


async def run_bundle(instance: Instance, *, clients: NcClients | None = None) -> Run:
    guard_routes.reset()
    capabilities.clear_cache()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        routes = instance.mount(mock)
        answer = await context_tools.prepare_context(clients or fresh(), "docs", detail="full")
    counts = {name: route.call_count for name, route in routes.items()}
    counts["search_started"] = len(instance.searches)
    return Run(answer, counts, list(instance.searches))


async def pair(
    monkeypatch: pytest.MonkeyPatch,
    hits: list[dict[str, Any]],
    make: Callable[[], Instance],
) -> tuple[Run, Run]:
    """The same bundle on the new route, then on the old one."""
    wire_search(monkeypatch, hits)
    new = await run_bundle(make())
    force_old_route(monkeypatch)
    old = await run_bundle(make())
    return new, old


# --- dav.entries_of_fileids ----------------------------------------------------------------


@pytest.mark.anyio
async def test_entries_of_fileids_asks_one_search_and_the_first_entry_wins() -> None:
    creds = Credentials(BASE, USER, "app-password-test")
    doubled = entry_xml("11", NODES["11"]) + entry_xml("11", Node("/Docs/zweitpfad.txt"))
    with respx.mock(assert_all_called=True) as mock:
        route = mock.route(method="SEARCH", url=DAV_ROOT).mock(
            side_effect=[
                multistatus(doubled + entry_xml("13", NODES["13"])),
                multistatus(entry_xml("11", NODES["11"])),
            ]
        )
        async with httpx.AsyncClient() as client:
            entries = await dav.entries_of_fileids(client, creds, ["11", "13", "11"])
            missing = await dav.entries_of_fileids(client, creds, ["11", "404"])

    assert route.call_count == 2, "one SEARCH per call"
    body = route.calls[0].request.content
    assert body == dav.build_fileids_body(dav.search_scope(creds), ["11", "13"])
    first = entries["11"]
    assert first is not None
    assert first["path"] == "/Docs/eins.txt", "the first entry wins"
    assert first["size"] == len(NODES["11"].content)
    assert first["fileid"] == "11"
    assert first == dav._entry("/Docs/eins.txt", _props_of(entry_xml("11", NODES["11"])))
    third = entries["13"]
    assert third is not None
    assert third["path"] == "/Docs/drei.txt"
    assert missing["404"] is None, "an answer below nresults proves the id missing"


@pytest.mark.anyio
async def test_entries_of_fileids_leaves_out_ids_a_full_answer_may_have_crowded_out() -> None:
    """``nresults`` equals the number of ids; a doubled entry could push another id out.

    The single lookup has ``nresults`` 1 and would still have found it, so such an id is
    left out of the answer (not ``None``) and its excerpt goes the single route.
    """
    creds = Credentials(BASE, USER, "app-password-test")
    doubled = entry_xml("11", NODES["11"]) + entry_xml("11", Node("/Docs/zweitpfad.txt"))
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="SEARCH", url=DAV_ROOT).mock(return_value=multistatus(doubled))
        async with httpx.AsyncClient() as client:
            entries = await dav.entries_of_fileids(client, creds, ["11", "13"])

    assert set(entries) == {"11"}


@pytest.mark.anyio
async def test_entries_of_fileids_refuses_before_any_request_and_asks_nothing_for_nothing() -> None:
    creds = Credentials(BASE, USER, "app-password-test")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(method="SEARCH", url=DAV_ROOT).mock(return_value=multistatus(""))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="not a numeric"):
                await dav.entries_of_fileids(client, creds, ["11", "1 OR 1"])
            assert await dav.entries_of_fileids(client, creds, []) == {}
    assert route.call_count == 0


@pytest.mark.anyio
async def test_paths_of_fileids_still_answers_path_by_id() -> None:
    creds = Credentials(BASE, USER, "app-password-test")
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="SEARCH", url=DAV_ROOT).mock(
            return_value=multistatus(entry_xml("11", NODES["11"]) + entry_xml("13", NODES["13"]))
        )
        async with httpx.AsyncClient() as client:
            paths = await dav.paths_of_fileids(client, creds, ["11", "13", "404"])
    assert paths == {"11": "/Docs/eins.txt", "13": "/Docs/drei.txt"}


# --- files.read(known=...) -----------------------------------------------------------------


async def _entry_of(fileid: str) -> dict[str, Any]:
    """The entry the file id SEARCH gives for one node, exactly as ``_fetch_file`` sees it."""
    creds = Credentials(BASE, USER, "app-password-test")
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="SEARCH", url=DAV_ROOT).mock(
            return_value=multistatus(entry_xml(fileid, NODES[fileid]))
        )
        async with httpx.AsyncClient() as client:
            entry = await dav.find_by_fileid(client, creds, fileid)
    assert entry is not None
    return entry


async def _outcome(call: Callable[[], Any]) -> str:
    try:
        return dumped(await call())
    except ToolError as error:
        return dumped({"error": triple(error)})


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fileid", "max_bytes"),
    [("11", 4000), ("12", 4000), ("16", 4000), ("16", 20), ("14", 4000), ("15", 4000)],
    ids=["text", "umlauts", "truncated", "tiny-slice", "folder", "not-text"],
)
async def test_a_known_entry_skips_the_propfind_and_answers_like_the_stat(
    fileid: str, max_bytes: int
) -> None:
    entry = await _entry_of(fileid)
    path = NODES[fileid].path
    outcomes: dict[str, tuple[str, dict[str, int]]] = {}
    for label, known in (("known", entry), ("stat", None)):
        instance = Instance(guard="untagged")
        guard_routes.reset()
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            routes = instance.mount(mock)
            clients = fresh()
            text = await _outcome(
                lambda c=clients, k=known: files_tools.read(c, path, max_bytes=max_bytes, known=k)
            )
        outcomes[label] = (text, {name: r.call_count for name, r in routes.items()})

    assert outcomes["known"][0] == outcomes["stat"][0]
    assert outcomes["known"][1]["propfind"] == 0
    assert outcomes["stat"][1]["propfind"] == 1
    assert outcomes["known"][1]["tags"] == 1, "the guard is asked on the known route too"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fileid", "guard", "tagged"),
    [
        ("11", "active", (("Docs/eins.txt", "11", False),)),
        ("17", "active", (TAGGED_FOLDER,)),
        ("11", "unverifiable", ()),
    ],
    ids=["tagged-file", "below-tagged-folder", "unverifiable"],
)
async def test_a_known_entry_never_skips_the_guard(
    fileid: str, guard: str, tagged: tuple[tuple[str, str, bool], ...]
) -> None:
    entry = await _entry_of(fileid)
    path = NODES[fileid].path
    errors: dict[str, tuple[str, str, str]] = {}
    for label, known in (("known", entry), ("stat", None)):
        instance = Instance(guard=guard, tagged=tagged)
        guard_routes.reset()
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            routes = instance.mount(mock)
            with pytest.raises(ToolError) as excinfo:
                await files_tools.read(fresh(), path, known=known)
        errors[label] = triple(excinfo.value)
        assert routes["report"].call_count == 1, "the known route asks the guard"
        assert routes["get"].call_count == 0, "not one byte of a withheld file is read"

    assert errors["known"] == errors["stat"]
    expected = withhold.unavailable_error() if guard == "unverifiable" else dav.not_found(path)
    assert errors["known"] == triple(expected)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mangle",
    [
        lambda e: {**e, "path": "/Docs/drei.txt"},
        lambda e: {**e, "fileid": ""},
        lambda e: {k: v for k, v in e.items() if k != "fileid"},
    ],
    ids=["other-path", "empty-fileid", "no-fileid"],
)
async def test_a_known_entry_that_does_not_fit_falls_back_to_the_stat(
    mangle: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    entry = mangle(await _entry_of("11"))
    instance = Instance(guard="untagged")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        routes = instance.mount(mock)
        answer = await files_tools.read(fresh(), NODES["11"].path, known=entry)
    assert routes["propfind"].call_count == 1
    assert answer["content"] == NODES["11"].content.decode()


# --- chatgpt.fetch(file) and file_entries --------------------------------------------------


@pytest.mark.anyio
async def test_fetch_without_resolved_sends_one_search_one_get_and_no_propfind() -> None:
    instance = Instance(guard="untagged")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        routes = instance.mount(mock)
        answer = await chatgpt_tools.fetch(fresh(), "file:12", max_bytes=4000)
    assert (routes["search"].call_count, routes["propfind"].call_count) == (1, 0)
    assert routes["get"].call_count == 1
    assert answer["text"] == NODES["12"].content.decode()
    assert answer["metadata"] == {
        "kind": "file",
        "path": "/Docs/zwei.md",
        "content_type": "text/plain",
    }


@pytest.mark.anyio
async def test_fetch_with_resolved_sends_no_search_and_answers_the_same_bytes() -> None:
    entry = await _entry_of("12")
    outcomes: list[str] = []
    counts: list[tuple[int, int]] = []
    for resolved in ({"12": entry}, None):
        instance = Instance(guard="active")
        guard_routes.reset()
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            routes = instance.mount(mock)
            outcomes.append(
                await _outcome(
                    lambda r=resolved: chatgpt_tools.fetch(
                        fresh(), "file:12", max_bytes=4000, resolved=r
                    )
                )
            )
        counts.append((routes["search"].call_count, routes["report"].call_count))
    assert outcomes[0] == outcomes[1]
    assert counts == [(0, 1), (1, 1)]


@pytest.mark.anyio
async def test_a_resolved_none_answers_exactly_like_an_unknown_id() -> None:
    outcomes: list[str] = []
    for resolved in ({"12": None}, None):
        instance = Instance(guard="untagged", gone=("12",))
        guard_routes.reset()
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            instance.mount(mock)
            outcomes.append(
                await _outcome(
                    lambda r=resolved: chatgpt_tools.fetch(fresh(), "file:12", resolved=r)
                )
            )
    assert outcomes[0] == outcomes[1]
    assert outcomes[0] == dumped({"error": triple(chatgpt_tools._no_file("12"))})


@pytest.mark.anyio
async def test_an_id_missing_from_resolved_goes_the_single_route() -> None:
    instance = Instance(guard="untagged")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        routes = instance.mount(mock)
        answer = await chatgpt_tools.fetch(
            fresh(), "file:13", max_bytes=4000, resolved={"11": None}
        )
    assert routes["search"].call_count == 1
    assert instance.searches == [["13"]]
    assert answer["text"] == NODES["13"].content.decode()


@pytest.mark.anyio
async def test_file_entries_takes_only_file_ids_and_asks_once() -> None:
    instance = Instance(guard="untagged", gone=("13",))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        routes = instance.mount(mock)
        entries = await chatgpt_tools.file_entries(
            fresh(), ["file:11", "note:5", "file:13", "file: 7", "card:1:2:3", "file:11"]
        )
    assert routes["search"].call_count == 1
    assert instance.searches == [["11", "13"]]
    assert set(entries) == {"11", "13"}
    eins = entries["11"]
    assert eins is not None
    assert eins["path"] == "/Docs/eins.txt"
    assert entries["13"] is None


# --- the bundle ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_bundle_resolves_its_file_excerpts_with_one_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [file_hit("11"), file_hit("12"), file_hit("13")]
    new, old = await pair(monkeypatch, hits, Instance)

    assert new.searches == [["11", "12", "13"]], "one SEARCH for the three excerpts"
    assert new.counts["propfind"] == 0, "no stat after the SEARCH"
    assert new.counts["get"] == 3
    assert new.counts["report"] == 1, "one REPORT per answer"
    assert old.counts["search"] == 3, "the reference route: one SEARCH per excerpt"
    assert old.counts["propfind"] == 3, "the reference route: one stat per excerpt"
    assert dumped(new.answer) == dumped(old.answer)
    assert [hit["excerpt"] for hit in new.answer["results"]["file"]] == [
        NODES[i].content.decode() for i in ("11", "12", "13")
    ]


BUNDLE_CASES: dict[str, tuple[list[str], dict[str, Any]]] = {
    "all-readable": (["11", "12", "13"], {}),
    "a-folder-hit": (["11", "14", "12"], {}),
    "a-not-text-file": (["15", "11", "12"], {}),
    "a-truncated-file": (["16", "11"], {}),
    "a-vanished-id": (["11", "19", "12"], {"gone": ("19",)}),
    "a-tagged-file": (["11", "12"], {"tagged": (("Docs/zwei.md", "12", False),)}),
    "below-a-tagged-folder": (["17", "11"], {}),
    "unverifiable-guard": (["11", "12", "13"], {"guard": "unverifiable"}),
    "untagged-instance": (["11", "12", "13"], {"guard": "untagged"}),
}


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(BUNDLE_CASES))
async def test_the_batched_route_answers_byte_for_byte_like_the_single_route(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fileids, options = BUNDLE_CASES[case]
    new, old = await pair(monkeypatch, [file_hit(i) for i in fileids], lambda: Instance(**options))

    assert dumped(new.answer) == dumped(old.answer)
    assert new.counts["search"] <= 1
    assert new.counts["propfind"] == 0
    assert new.counts.get("report", 0) == old.counts.get("report", 0) <= 1


@pytest.mark.anyio
async def test_a_file_outside_the_files_root_answers_like_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NC_MCP_FILES_ROOT", "/Docs")
    new, old = await pair(
        monkeypatch, [file_hit("18"), file_hit("11")], lambda: Instance(guard="untagged")
    )
    assert dumped(new.answer) == dumped(old.answer)
    assert new.counts["search"] == 1
    assert {"source": "file:18", "reason": triple(chatgpt_tools._no_file("18"))[0]} in (
        new.answer["degraded"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize("search", ["fail_batch", "fail_all"])
async def test_a_failed_batch_falls_back_to_the_single_route_sentences(
    search: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [file_hit("11"), file_hit("12"), file_hit("13")]
    new, old = await pair(monkeypatch, hits, lambda: Instance(search=search))

    assert dumped(new.answer) == dumped(old.answer)
    assert new.searches[0] == ["11", "12", "13"], "the batch was tried first"
    assert sorted(map(tuple, new.searches[1:])) == [("11",), ("12",), ("13",)]
    if search == "fail_all":
        reasons = [entry["reason"] for entry in new.answer["degraded"]]
        assert len(reasons) == 3
        assert all("the file with id" in reason for reason in reasons), reasons


@pytest.mark.anyio
async def test_a_batch_that_misses_its_budget_reads_like_a_timed_out_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_tools, "EXCERPT_TIMEOUT", 0.2)
    hits = [file_hit("11"), file_hit("12")]
    new, old = await pair(monkeypatch, hits, lambda: Instance(search="hang"))

    assert dumped(new.answer) == dumped(old.answer)
    sentence = (
        f"The excerpt source did not answer within {context_tools.EXCERPT_TIMEOUT:g} seconds."
    )
    assert new.answer["degraded"] == [
        {"source": "file:11", "reason": sentence},
        {"source": "file:12", "reason": sentence},
    ]
    assert new.searches == [["11", "12"]]


@pytest.mark.anyio
async def test_the_budget_sentence_names_five_seconds() -> None:
    assert context_tools.EXCERPT_TIMEOUT == 5.0
    reason = context_tools._reason(TimeoutError(), "excerpt source", context_tools.EXCERPT_TIMEOUT)
    assert reason == "The excerpt source did not answer within 5 seconds."


@pytest.mark.anyio
async def test_no_batch_request_outlives_the_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_tools, "EXCERPT_TIMEOUT", 0.1)
    wire_search(monkeypatch, [file_hit("11")])
    await run_bundle(Instance(search="hang"))
    await asyncio.sleep(0)
    current = asyncio.current_task()
    left = [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done() and "file_entries" in repr(task.get_coro())
    ]
    assert left == []


@pytest.mark.anyio
async def test_notes_and_cards_never_wait_for_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [
        file_hit("11"),
        other_hit("note:5", "note", "notes"),
        other_hit("card:1:2:3", "card", "search-deck-card-board"),
    ]
    wire_search(monkeypatch, hits)
    real_fetch = chatgpt_tools.fetch
    others_read = asyncio.Event()
    calls: list[tuple[str, Any]] = []
    batches: list[list[str]] = []

    async def fetch(clients: NcClients, resource_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((resource_id, kwargs.get("resolved")))
        if resource_id.startswith("file:"):
            return await real_fetch(clients, resource_id, **kwargs)
        if sum(1 for rid, _ in calls if not rid.startswith("file:")) == 2:
            others_read.set()
        return {"id": resource_id, "title": "t", "text": f"text of {resource_id}", "url": "u"}

    real_entries = chatgpt_tools.file_entries

    async def entries(clients: NcClients, identifiers: Any, **kwargs: Any) -> Any:
        batches.append(list(identifiers))
        return await real_entries(clients, identifiers, **kwargs)

    async def wait_for_others() -> None:
        await asyncio.wait_for(others_read.wait(), timeout=2)

    monkeypatch.setattr(chatgpt_tools, "fetch", fetch)
    monkeypatch.setattr(chatgpt_tools, "file_entries", entries)
    run = await run_bundle(Instance(before_search=wait_for_others))

    # 27-10: a note that checks its path joins the batch, so its id is asked with the files;
    # the fake fetch never awaits it, which is what "never waits" means for a note without
    # a path check. Cards stay out of the batch.
    assert batches == [["file:11", "note:5"]], "only file and note ids go into the batch"
    assert "degraded" not in run.answer, run.answer.get("degraded")
    assert run.answer["results"]["note"][0]["excerpt"] == "text of note:5"
    assert run.answer["results"]["file"][0]["excerpt"] == NODES["11"].content.decode()
    assert dict(calls)["note:5"] is None
    assert dict(calls)["card:1:2:3"] is None
    assert dict(calls)["file:11"] is not None


@pytest.mark.anyio
async def test_two_bundles_resolve_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """E3: the entries belong to one call; the same NcClients asks again the next time."""
    wire_search(monkeypatch, [file_hit("11"), file_hit("12")])
    clients = fresh()
    first = await run_bundle(Instance(), clients=clients)
    second = await run_bundle(Instance(), clients=clients)
    assert first.searches == [["11", "12"]]
    assert second.searches == [["11", "12"]]
    assert dumped(first.answer) == dumped(second.answer)


# --- 27-10: the path check of a note excerpt joins the batch -------------------------------
#
# The reference route of these pairs is the code before plan 27-10: the files still share
# the batch of 27-09, but the batch holds no note id and ``fetch`` hands the note no
# ``note_batch``, so ``notes.read`` asks its own path SEARCH exactly as before.


def note_hit(note_id: str) -> dict[str, Any]:
    return other_hit(f"note:{note_id}", "note", "notes")


def force_single_note_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code before plan 27-10: no note id in the batch, no ``note_batch`` for a note."""
    real_entries = chatgpt_tools.file_entries
    real_fetch = chatgpt_tools.fetch

    async def files_only(clients: NcClients, identifiers: Any, **_kinds: Any) -> Any:
        return await real_entries(clients, identifiers)

    async def fetch_without_note_batch(
        clients: NcClients, resource_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        kwargs.pop("note_batch", None)
        return await real_fetch(clients, resource_id, **kwargs)

    monkeypatch.setattr(chatgpt_tools, "file_entries", files_only)
    monkeypatch.setattr(chatgpt_tools, "fetch", fetch_without_note_batch)


async def note_pair(
    monkeypatch: pytest.MonkeyPatch,
    hits: list[dict[str, Any]],
    make: Callable[[], Instance],
) -> tuple[Run, Run]:
    """The same bundle with the note path in the batch, then with its own path SEARCH."""
    wire_search(monkeypatch, hits)
    new = await run_bundle(make())
    force_single_note_path(monkeypatch)
    old = await run_bundle(make())
    return new, old


#: Tagged: the folder of the 27-09 cases plus the note category ``Geheim``.
WITH_GEHEIM = (TAGGED_FOLDER, GEHEIM)

NOTE_CASES: dict[str, tuple[list[dict[str, Any]], dict[str, Any], str]] = {
    "readable-note": ([file_hit("11"), file_hit("12"), note_hit("31")], {}, "/"),
    "below-a-tagged-category": (
        [file_hit("11"), file_hit("12"), note_hit("32")],
        {"tagged": WITH_GEHEIM},
        "/",
    ),
    "outside-the-files-root": ([file_hit("11"), file_hit("12"), note_hit("31")], {}, "/Docs"),
    "a-tagged-note-file": (
        [file_hit("11"), note_hit("31")],
        {"tagged": (TAGGED_FOLDER, ("Notes/Offen/lesbar.md", "31", False))},
        "/",
    ),
    "an-unknown-note-id": ([file_hit("11"), file_hit("12"), note_hit("39")], {}, "/"),
    "crowded-out-of-the-batch": (
        [file_hit("11"), file_hit("12"), note_hit("31")],
        {"crowd": True},
        "/",
    ),
    "a-failed-batch": (
        [file_hit("11"), file_hit("12"), note_hit("31")],
        {"search": "fail_batch"},
        "/",
    ),
    "every-search-fails": (
        [file_hit("11"), file_hit("12"), note_hit("31")],
        {"search": "fail_all"},
        "/",
    ),
    "unverifiable-guard": (
        [file_hit("11"), file_hit("12"), note_hit("31")],
        {"guard": "unverifiable"},
        "/",
    ),
    "untagged-instance": (
        [file_hit("11"), file_hit("12"), note_hit("31")],
        {"guard": "untagged"},
        "/",
    ),
}


@pytest.mark.anyio
async def test_the_note_path_check_joins_the_batch_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario B of the wall clock: excerpts file,file,note below a tagged folder."""
    hits = [file_hit("11"), file_hit("12"), note_hit("31")]
    new, old = await note_pair(monkeypatch, hits, Instance)

    assert new.searches == [["11", "12", "31"]], "one SEARCH for the files and the note"
    assert new.counts["search"] == 1, "no path SEARCH of the note of its own"
    assert new.counts["propfind"] == 0
    assert new.counts["report"] == 1, "one REPORT per answer"
    assert old.searches == [["11", "12"], ["31"]], "the reference route: the note asks alone"
    assert dumped(new.answer) == dumped(old.answer)
    assert new.answer["results"]["note"][0]["excerpt"] == NOTES["31"][1]
    assert "degraded" not in new.answer


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(NOTE_CASES))
async def test_a_batched_note_answers_byte_for_byte_like_the_single_route(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits, options, root = NOTE_CASES[case]
    monkeypatch.setenv("NC_MCP_FILES_ROOT", root)
    new, old = await note_pair(monkeypatch, hits, lambda: Instance(**options))

    assert dumped(new.answer) == dumped(old.answer)
    note_id = str(hits[-1]["id"]).partition(":")[2]
    assert note_id in new.searches[0], "the note id rides in the one batch SEARCH"
    assert note_id not in old.searches[0], "the reference batch holds file ids only"
    assert new.counts["search"] <= old.counts["search"]
    assert new.counts["propfind"] == 0
    assert new.counts.get("report", 0) == old.counts.get("report", 0) <= 1


@pytest.mark.anyio
async def test_a_note_below_a_tagged_category_answers_the_one_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [file_hit("11"), file_hit("12"), note_hit("32")]
    new, _ = await note_pair(monkeypatch, hits, lambda: Instance(tagged=WITH_GEHEIM))
    assert new.searches == [["11", "12", "32"]]
    assert new.answer["degraded"] == [
        {"source": "note:32", "reason": notes_tools._note_not_found("32").message}
    ]


@pytest.mark.anyio
async def test_a_note_missing_from_the_batch_goes_the_single_path_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [file_hit("11"), file_hit("12"), note_hit("31")]
    new, old = await note_pair(monkeypatch, hits, lambda: Instance(crowd=True))

    assert new.searches[0] == ["11", "12", "31"]
    assert ["31"] in new.searches[1:], "the crowded out note asks its own path SEARCH"
    assert dumped(new.answer) == dumped(old.answer)
    assert new.answer["results"]["note"][0]["excerpt"] == NOTES["31"][1]


@pytest.mark.anyio
@pytest.mark.parametrize("search", ["fail_batch", "fail_all"])
async def test_a_failed_batch_leaves_the_note_its_own_sentences(
    search: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [file_hit("11"), file_hit("12"), note_hit("31")]
    new, old = await note_pair(monkeypatch, hits, lambda: Instance(search=search))

    assert dumped(new.answer) == dumped(old.answer)
    assert new.searches[0] == ["11", "12", "31"], "the batch was tried first"
    assert ["31"] in new.searches[1:], "then the note asked on its own"
    degraded = {entry["source"]: entry["reason"] for entry in new.answer.get("degraded", [])}
    assert "note:31" not in degraded
    if search == "fail_all":
        # The note's own lookup failed as well: withheld, said once for the bundle.
        assert degraded["exclusion"] == withhold.EXCLUSION_UNAVAILABLE
    else:
        assert new.answer["results"]["note"][0]["excerpt"] == NOTES["31"][1]


@pytest.mark.anyio
async def test_a_batch_that_misses_its_budget_times_the_note_out_like_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_tools, "EXCERPT_TIMEOUT", 0.2)
    hits = [file_hit("11"), note_hit("31")]
    new, old = await note_pair(monkeypatch, hits, lambda: Instance(search="hang"))

    assert dumped(new.answer) == dumped(old.answer)
    sentence = (
        f"The excerpt source did not answer within {context_tools.EXCERPT_TIMEOUT:g} seconds."
    )
    assert new.answer["degraded"] == [
        {"source": "file:11", "reason": sentence},
        {"source": "note:31", "reason": sentence},
    ]
    assert new.searches == [["11", "31"]]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("guard", "tagged"),
    [
        ("untagged", ()),
        ("active", (TAGGED_FOLDER, ("Notes/Offen/lesbar.md", "31", False))),
        ("unverifiable", ()),
    ],
    ids=["no-path-check", "tagged-note-file", "unverifiable"],
)
async def test_a_note_without_path_check_never_waits_for_the_batch(
    guard: str, tagged: tuple[tuple[str, str, bool], ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch SEARCH is held until the note excerpt is done; a waiting note would hang."""
    wire_search(monkeypatch, [file_hit("11"), note_hit("31")])
    real_fetch = chatgpt_tools.fetch
    note_done = asyncio.Event()

    async def fetch(clients: NcClients, resource_id: str, **kwargs: Any) -> dict[str, Any]:
        try:
            return await real_fetch(clients, resource_id, **kwargs)
        finally:
            if resource_id.startswith("note:"):
                note_done.set()

    async def hold() -> None:
        await asyncio.wait_for(note_done.wait(), timeout=2)

    monkeypatch.setattr(chatgpt_tools, "fetch", fetch)
    run = await run_bundle(Instance(guard=guard, tagged=tagged, before_search=hold))

    assert run.searches == [["11", "31"]]
    degraded = {entry["source"]: entry["reason"] for entry in run.answer.get("degraded", [])}
    if guard == "untagged":
        assert degraded == {}
        assert run.answer["results"]["note"][0]["excerpt"] == NOTES["31"][1]
    elif guard == "active":
        assert degraded == {"note:31": notes_tools._note_not_found("31").message}
    else:
        # Every excerpt is withheld; the bundle says so once and no excerpt timed out.
        assert set(degraded) == {"exclusion"}


@pytest.mark.anyio
async def test_a_notes_only_bundle_sends_no_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [note_hit("31"), note_hit("32")]
    new, old = await note_pair(monkeypatch, hits, lambda: Instance(tagged=WITH_GEHEIM))

    assert sorted(new.searches) == [["31"], ["32"]], "each note asks its own path, no batch"
    assert dumped(new.answer) == dumped(old.answer)
    assert new.counts == old.counts


@pytest.mark.anyio
async def test_no_note_path_reaches_the_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [file_hit("11"), note_hit("31"), note_hit("32")]
    new, old = await note_pair(monkeypatch, hits, lambda: Instance(tagged=WITH_GEHEIM))

    text = dumped(new.answer)
    assert new.searches == [["11", "31", "32"]]
    for path in (NODES["31"].path, NODES["32"].path, "Notes/Geheim", "Notes/Offen"):
        assert path not in text, path
    assert text == dumped(old.answer)


@pytest.mark.anyio
async def test_two_bundles_with_a_note_resolve_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """E3: the note path of one bundle is not the note path of the next one."""
    wire_search(monkeypatch, [file_hit("11"), note_hit("31")])
    clients = fresh()
    first = await run_bundle(Instance(), clients=clients)
    second = await run_bundle(Instance(), clients=clients)
    assert first.searches == [["11", "31"]]
    assert second.searches == [["11", "31"]]
    assert dumped(first.answer) == dumped(second.answer)


@pytest.mark.anyio
async def test_file_entries_takes_note_ids_into_the_same_search_when_asked() -> None:
    instance = Instance(guard="untagged")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        routes = instance.mount(mock)
        entries = await chatgpt_tools.file_entries(
            fresh(),
            ["file:11", "note:31", "card:1:2:3", "note:11", "note:x"],
            kinds=("file", "note"),
        )
    assert routes["search"].call_count == 1
    assert instance.searches == [["11", "31"]], "a digit id as file and note is asked once"
    assert set(entries) == {"11", "31"}


# --- 27-10: notes.read(batch=...) on its own ------------------------------------------------


async def _read(instance: Instance, batch: Callable[[], Any] | None) -> tuple[str, dict[str, int]]:
    guard_routes.reset()
    capabilities.clear_cache()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        routes = instance.mount(mock)
        text = await _outcome(lambda: notes_tools.read(fresh(), "note:31", batch=batch))
    return text, {name: route.call_count for name, route in routes.items()}


def batch_of(answer: Any) -> tuple[Callable[[], Any], list[int]]:
    calls: list[int] = []

    async def batch() -> Any:
        calls.append(1)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return batch, calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tagged", "root", "batched", "searches"),
    [
        ((TAGGED_FOLDER,), "/", "entry", 0),
        ((GEHEIM, ("Notes/Offen", "931", True)), "/", "entry", 0),
        ((TAGGED_FOLDER,), "/Docs", "none", 0),
        ((TAGGED_FOLDER,), "/", "none", 0),
        ((TAGGED_FOLDER,), "/", "absent", 1),
        ((TAGGED_FOLDER,), "/", "tool-error", 1),
        ((TAGGED_FOLDER,), "/", "http-error", 1),
        ((TAGGED_FOLDER,), "/", "value-error", 1),
    ],
    ids=[
        "readable",
        "below-tagged-category",
        "outside-the-root",
        "certainly-missing",
        "crowded-out",
        "tool-error",
        "http-error",
        "value-error",
    ],
)
async def test_read_with_a_batch_answers_like_the_single_path_check(
    tagged: tuple[tuple[str, str, bool], ...],
    root: str,
    batched: str,
    searches: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _entry_of("31")
    monkeypatch.setenv("NC_MCP_FILES_ROOT", root)
    answers: dict[str, Any] = {
        "entry": {"31": entry},
        "none": {"31": None},
        "absent": {"99": None},
        "tool-error": ToolError(message="batch failed", hint="none"),
        "http-error": httpx.ConnectError("batch failed"),
        "value-error": ValueError("batch failed"),
    }
    batch, calls = batch_of(answers[batched])
    # A batch answers None only for a note the single lookup does not find either: outside
    # the root it drops out of the answer, and a vanished note is gone for both routes.
    gone = ("31",) if batched == "none" and root == "/" else ()
    new, new_counts = await _read(Instance(tagged=tagged, gone=gone), batch)
    old, old_counts = await _read(Instance(tagged=tagged, gone=gone), None)

    assert new == old
    assert calls == [1]
    assert new_counts["search"] == searches
    assert old_counts["search"] == 1


@pytest.mark.anyio
async def test_read_with_a_batch_keeps_the_unavailable_sentence_when_both_fail() -> None:
    batch, calls = batch_of(ToolError(message="batch failed", hint="none"))
    new, counts = await _read(Instance(search="fail_all"), batch)
    assert new == dumped({"error": triple(withhold.unavailable_error())})
    assert calls == [1]
    assert counts["search"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("guard", "tagged"),
    [
        ("untagged", ()),
        ("active", (TAGGED_FOLDER, ("Notes/Offen/lesbar.md", "31", False))),
        ("unverifiable", ()),
    ],
    ids=["no-path-check", "tagged-note-file", "unverifiable"],
)
async def test_read_never_asks_the_batch_without_a_path_check(
    guard: str, tagged: tuple[tuple[str, str, bool], ...]
) -> None:
    batch, calls = batch_of({"31": None})
    new, _ = await _read(Instance(guard=guard, tagged=tagged), batch)
    old, _ = await _read(Instance(guard=guard, tagged=tagged), None)
    assert new == old
    assert calls == [], "the guard decides before any wait for the batch"
