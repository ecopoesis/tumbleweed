"""The saved-tools list as a state machine, without a window (S24, D30 (9))."""

from collections.abc import Callable
from datetime import UTC, datetime

from tests.shell.prompt_support import show_message
from tumbleweed.core.ids import GrantId, ToolId
from tumbleweed.core.types import ContentHash, ToolVersion
from tumbleweed.ipc.broker_messages import ReadAccess
from tumbleweed.ipc.tool_messages import ToolGrantInfo, ToolSummary
from tumbleweed.shell.viewmodel.prompts import card_of
from tumbleweed.shell.viewmodel.tools import (
    GrantRow,
    ToolRow,
    ToolsModel,
    ToolsOutcome,
    VersionRow,
    grant_row_of,
    row_of,
)

WHEN = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def summary(
    tool: ToolId | None = None,
    version: int = 1,
    name: str = "Photo tool",
    taint: tuple[str, ...] = (),
    description: str = "Adjusts photos.",
) -> ToolSummary:
    return ToolSummary.model_validate(
        {
            "tool": tool or ToolId.new(),
            "version": version,
            "hash": ContentHash(f"{version:064x}"),
            "name": name,
            "description": description,
            "tags": ("photo",),
            "taint": taint,
            "claimed_taint": (),
            "origin": "saved",
            "saved_at": WHEN,
        }
    )


def grant(
    tool: ToolId, *, version: int | None = 1, newest: bool = True, path: str | None = "/home/u/p"
) -> ToolGrantInfo:
    return ToolGrantInfo(
        grant=GrantId.new(),
        tool=tool,
        tool_name="Photo tool",
        version=None if version is None else ToolVersion(version),
        newest=newest,
        access="fs.read" if path is not None else "net.full",
        path=path,
        issued_at=WHEN,
        expires_at=None,
    )


class FakePort:
    def __init__(self, *tools: ToolSummary, grants: tuple[ToolGrantInfo, ...] = ()) -> None:
        self.tools: tuple[ToolSummary, ...] | None = tools
        self.history: dict[ToolId, tuple[ToolSummary, ...]] = {}
        self.granted: tuple[ToolGrantInfo, ...] | None = grants
        self.calls: list[tuple[str, object]] = []
        self.outcome = ToolsOutcome("done", "Done.")
        self.pending: list[Callable[[], None]] = []

    def list(self, done: Callable[[tuple[ToolSummary, ...] | None], None]) -> None:
        done(self.tools)

    def versions(
        self, tool: ToolId, done: Callable[[tuple[ToolSummary, ...] | None], None]
    ) -> None:
        done(self.history.get(tool))

    def grants(self, done: Callable[[tuple[ToolGrantInfo, ...] | None], None]) -> None:
        done(self.granted)

    def open(
        self, tool: ToolId, version: ToolVersion | None, done: Callable[[ToolsOutcome], None]
    ) -> None:
        self.calls.append(("open", (tool, version)))
        self.pending.append(lambda: done(self.outcome))

    def restore(
        self, tool: ToolId, version: ToolVersion, done: Callable[[ToolsOutcome], None]
    ) -> None:
        self.calls.append(("restore", (tool, version)))
        self.pending.append(lambda: done(self.outcome))

    def revoke(self, grant: GrantId, done: Callable[[ToolsOutcome], None]) -> None:
        self.calls.append(("revoke", grant))
        self.pending.append(lambda: done(self.outcome))


class FakeView:
    def __init__(self) -> None:
        self.shown: list[
            tuple[tuple[ToolRow, ...], tuple[VersionRow, ...], tuple[GrantRow, ...], str | None]
        ] = []
        self.busy_states: list[bool] = []

    def present(
        self,
        tools: tuple[ToolRow, ...],
        history: tuple[VersionRow, ...],
        grants: tuple[GrantRow, ...],
        note: str | None,
    ) -> None:
        self.shown.append((tools, history, grants, note))

    def busy(self, busy: bool) -> None:
        self.busy_states.append(busy)


def test_a_tools_text_is_one_plain_capped_line_and_the_badge_is_ours() -> None:
    hostile = summary(
        name="Photo\ntool‮ <b>x</b>" + "n" * 40,
        taint=("fs",),
        description="line one\nline two " + "d" * 400,
    )

    row = row_of(hostile)

    assert "\n" not in row.name
    assert "‮" not in row.name
    assert len(row.name) <= 64
    assert len(row.description) <= 160
    assert row.taint == "saw your files"
    assert "<b>x</b>" in row.name  # kept as the text it is: the label shows it literally


def test_the_list_shows_the_tools_and_the_grants_and_a_note_when_there_are_none() -> None:
    tool = summary()
    port, view = FakePort(tool, grants=(grant(tool.tool),)), FakeView()
    model = ToolsModel(port, view)

    model.refresh()

    tools, history, grants, note = view.shown[-1]
    assert [row.tool for row in tools] == [tool.tool]
    assert history == ()
    assert [row.access for row in grants] == ["may read"]
    assert note is None
    empty = FakePort()
    nothing = FakeView()
    ToolsModel(empty, nothing).refresh()
    assert nothing.shown[-1][3] == "No tool has been saved yet."


def test_a_broker_that_does_not_answer_is_a_note_not_a_crash() -> None:
    port, view = FakePort(), FakeView()
    port.tools = None

    ToolsModel(port, view).refresh()

    assert view.shown[-1][0] == ()
    assert "not answering" in str(view.shown[-1][3])
    assert view.busy_states == [True, False]


def test_a_grant_for_an_older_or_missing_version_says_so_in_our_words() -> None:
    tool = summary()

    older = grant_row_of(grant(tool.tool, version=1, newest=False))
    gone = grant_row_of(grant(tool.tool, version=None, newest=False, path=None))

    assert older.version == "version 1 (an older version)"
    assert gone.version == "a version that is no longer stored"
    assert gone.access == "may use the network"
    assert gone.path is None


def test_history_opens_for_one_tool_and_only_known_rows_can_be_acted_on() -> None:
    tool = summary(version=2)
    port, view = FakePort(tool), FakeView()
    port.history[tool.tool] = (summary(tool.tool, 2), summary(tool.tool, 1))
    model = ToolsModel(port, view)
    model.refresh()

    assert not model.open(ToolId.new())  # not a row of the list
    assert not model.restore(tool.tool, ToolVersion(1))  # the history is not open yet
    assert model.show_history(tool.tool)
    history = view.shown[-1][1]
    assert [(row.version, row.newest) for row in history] == [(2, True), (1, False)]
    assert not model.restore(tool.tool, ToolVersion(2))  # the newest is not restored
    assert model.restore(tool.tool, ToolVersion(1))
    assert port.calls == [("restore", (tool.tool, ToolVersion(1)))]


def test_one_action_at_a_time_and_its_text_is_the_note_of_the_next_list() -> None:
    tool = summary()
    port, view = FakePort(tool), FakeView()
    model = ToolsModel(port, view)
    model.refresh()

    assert model.open(tool.tool)
    assert not model.open(tool.tool)  # one is under way
    port.outcome = ToolsOutcome("refused", "Not done: refused.")
    port.pending.pop()()

    assert view.shown[-1][3] == "Not done: refused."
    assert view.busy_states[-1] is False


def test_a_grant_is_revoked_only_if_it_is_in_the_list() -> None:
    tool = summary()
    mine = grant(tool.tool)
    port, view = FakePort(tool, grants=(mine,)), FakeView()
    model = ToolsModel(port, view)
    model.refresh()

    assert not model.revoke(GrantId.new())
    assert model.revoke(mine.grant)
    assert port.calls == [("revoke", mine.grant)]


def test_the_prompt_card_names_the_tool_as_its_own_words_and_offers_the_tool_scope() -> None:
    card = card_of(
        show_message(
            access=ReadAccess(kind="fs.read", path="/home/u/p"),
            scopes=("once", "task", "tool"),
            tool=ToolId.new(),
            tool_name="Photo\ntool‮",
        )
    )

    assert card.scopes == ("once", "task", "tool")
    assert card.tool_name is not None
    assert "\n" not in card.tool_name
    assert "‮" not in card.tool_name


def test_every_grant_the_broker_can_hold_is_listed_so_each_can_be_revoked() -> None:
    tool = summary()
    many = tuple(grant(tool.tool, path=f"/home/u/p{n}") for n in range(100))
    port, view = FakePort(tool, grants=many), FakeView()

    ToolsModel(port, view).refresh()

    assert len(view.shown[-1][2]) == 100


def test_a_failed_read_of_the_grants_is_an_error_not_an_empty_list() -> None:
    port, view = FakePort(summary()), FakeView()
    port.granted = None

    ToolsModel(port, view).refresh()

    assert view.shown[-1][2] == ()
    assert "access list could not be read" in str(view.shown[-1][3])


def test_a_full_page_of_tools_says_that_only_the_newest_are_listed() -> None:
    port, view = FakePort(*(summary(name=f"T{n}") for n in range(50))), FakeView()

    ToolsModel(port, view).refresh()

    assert len(view.shown[-1][0]) == 50
    assert "Only the newest 50 saved tools are listed." in str(view.shown[-1][3])


def test_the_chrome_says_refusals_in_its_own_words_not_the_models() -> None:
    from tumbleweed.broker.client import BrokerRefused
    from tumbleweed.broker.toolflow import OPEN_REFUSAL_TEXT
    from tumbleweed.shell.broker_tools import _refusal

    for text in OPEN_REFUSAL_TEXT.values():
        said = _refusal(BrokerRefused("denied", text)).text

        assert "start_task" not in said
        assert "fresh_context" not in said
        assert ".." not in said
        assert "new task" in said
    assert (
        _refusal(BrokerRefused("not_found", "x")).text
        == "Not done: that tool or version is not stored."
    )
