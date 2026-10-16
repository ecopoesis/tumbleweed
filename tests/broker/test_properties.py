"""Property tests: generated escape attempts against the deny rules, the safe open and taint.

Each property compares the code with an oracle written differently: `os.path.realpath` and
`os.path.samefile` (which compare `st_dev` and `st_ino`, where the code under test uses
`f_fsid` and `st_ino`), and a walk with `lstat`. The generated paths are built from segments
that are escape attempts: `..`, `.`, empty segments, symlinks to protected places and to their
ancestors, chains and loops of symlinks, case variants, and Unicode variants.
"""

import datetime as dt
import os
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests.broker.consts import NOW
from tests.broker.requests import request, run_context
from tests.broker.support import World, resolve
from tumbleweed.broker import (
    AccessRequest,
    Allow,
    Capability,
    CapabilityKind,
    Deny,
    DenyRelation,
    FileKind,
    FsIdentity,
    FsRead,
    Grant,
    GrantOrigin,
    GrantScope,
    Issued,
    NeedsPrompt,
    NetFull,
    OpenedPath,
    OpenStrategy,
    PathError,
    PathFault,
    ResolvedPath,
    RunTrigger,
    StoredPath,
    TaskState,
    ToolGrant,
    decide,
    deny_entries,
    issue_grant,
    open_in_workspace,
    openat2_available,
    select_grants,
)
from tumbleweed.broker.paths import fold_name
from tumbleweed.broker.policy import StaticDeny
from tumbleweed.broker.task import other_taint, taint_of
from tumbleweed.core.ids import GrantId, TaskId, ToolId
from tumbleweed.core.types import WorkspaceRelPath

if TYPE_CHECKING:
    from collections.abc import Callable

# Deterministic and database-free: a failure reproduces from the printed example, and a run
# leaves no `.hypothesis` directory behind.
PROPERTY = settings(
    max_examples=300,
    deadline=None,
    database=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.function_scoped_fixture],
)

ZWJ = chr(0x200D)


def fullwidth(text: str) -> str:
    return "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text)


@pytest.fixture(scope="module")
def attack(shared_world: World) -> World:
    """The shared world plus symlinks that lead to protected places, to their ancestors, in
    chains and in loops."""
    home, base = shared_world.home, shared_world.base
    links = {
        home / "link_ssh": ".ssh",
        home / "abs_ssh": str(home / ".ssh"),
        home / "chain1": "chain2",
        home / "chain2": ".ssh",
        home / "loop_a": "loop_b",
        home / "loop_b": "loop_a",
        home / "dangling": "nowhere",
        home / "Documents" / "up": "..",
        home / "Documents" / "up2": "../..",
        home / "Documents" / "to_config": "../.config",
        home / "Documents" / "to_gcloud": "../.config/gcloud",
        home / "Documents" / "to_netrc": "../.netrc",
        home / "Photos" / "sshlink": "../.ssh",
        home / "Photos" / "root": "/",
        home / "Photos" / "base": str(base),
    }
    for link, target in links.items():
        link.symlink_to(target)
    return shared_world


def protected_places(world: World) -> list[str]:
    """The protected places that exist in the fake home, for the oracle."""
    home = world.home
    places = [home / ".ssh", home / ".gnupg", home / ".aws", home / ".config" / "gcloud"]
    places += [home / ".local" / "share" / "tumbleweed", home / ".netrc", world.base / "runtime"]
    return [str(place) for place in places]


def prefixes(path: str) -> list[str]:
    parts = Path(path).parts
    return [str(Path(*parts[: i + 1])) for i in range(len(parts))]


def same_file(left: str, right: str) -> bool:
    try:
        return Path(left).samefile(right)
    except OSError:
        return False


def touches_protected(real: str, protected: list[str]) -> bool:
    """Whether `real` is at, below or above any protected place, by `st_dev` and `st_ino`."""
    for place in protected:
        if any(same_file(prefix, place) for prefix in prefixes(real)):
            return True
        if any(same_file(real, prefix) for prefix in prefixes(place)):
            return True
    return False


SEGMENTS = [
    "..",
    "..",
    "..",
    ".",
    "",
    ".ssh",
    ".SSH",
    ".Ssh",
    fullwidth(".ssh"),
    ".s" + ZWJ + "sh",
    unicodedata.normalize("NFD", ".ssh"),
    ".gnupg",
    ".aws",
    ".config",
    "gcloud",
    "GCLOUD",
    ".local",
    "share",
    "tumbleweed",
    "link_ssh",
    "abs_ssh",
    "chain1",
    "chain2",
    "loop_a",
    "dangling",
    "up",
    "up2",
    "to_config",
    "to_gcloud",
    "to_netrc",
    "sshlink",
    "root",
    "base",
    "Documents",
    "Photos",
    "2026",
    "notes.txt",
    "a.jpg",
    "id_rsa",
    ".netrc",
    "runtime",
    "home",
    "nothing",
]


def escape_attempts(world: World) -> st.SearchStrategy[str]:
    bases = [
        f"{world.home}/",
        f"{world.home}//",
        f"{world.home}/Documents/",
        f"{world.home}/Photos/",
        f"{world.base}/",
        "/",
        f"{world.base}/home/./",
    ]
    return st.builds(
        lambda base, segments, tail: base + "/".join(segments) + tail,
        st.sampled_from(bases),
        st.lists(st.sampled_from(SEGMENTS), max_size=7),
        st.sampled_from(["", "/", "/.", "//"]),
    )


class TestEscapeAttemptsAgainstTheDenyRules:
    def test_the_oracle_itself_sees_what_it_should(self, attack: World) -> None:
        """Guards the oracle: it flags the protected places and not the ordinary ones."""
        protected = protected_places(attack)
        home = attack.home

        flagged = [home / ".ssh", home, attack.base, Path("/"), home / "link_ssh" / "id_rsa"]
        clean = [home / "Documents", home / "Photos" / "2026", home / "Documents" / "notes.txt"]

        assert all(touches_protected(os.path.realpath(p), protected) for p in flagged)
        assert not any(touches_protected(os.path.realpath(p), protected) for p in clean)

    def test_the_generator_reaches_both_outcomes_and_every_kind_of_link(
        self, attack: World
    ) -> None:
        """Guards against a vacuous property: both kinds of path are really generated."""
        protected = protected_places(attack)
        outcomes: set[bool] = set()
        resolved = 0

        @settings(
            max_examples=300,
            deadline=None,
            database=None,
            derandomize=True,
            suppress_health_check=list(HealthCheck),
        )
        @given(escape_attempts(attack))
        def collect(text: str) -> None:
            nonlocal resolved
            try:
                resolve(text)
            except PathError:
                return
            resolved += 1
            outcomes.add(touches_protected(os.path.realpath(text), protected))

        collect()

        assert outcomes == {True, False}
        assert resolved > 100

    @PROPERTY
    @given(data=st.data())
    def test_nothing_that_touches_a_protected_place_is_allowed_asked_about_or_granted(
        self, attack: World, data: st.DataObject
    ) -> None:
        text = data.draw(escape_attempts(attack))
        try:
            resolved = resolve(text)
        except PathError:
            assume(False)  # refused before any decision: nothing to check
        touches = touches_protected(os.path.realpath(text), protected_places(attack))
        task = TaskState(task=TaskId.new())
        asking = request(task.task, text)

        decision = decide(task, [], asking, rules=attack.rules, now=NOW)
        issuance = issue_grant(
            task,
            asking,
            scope=GrantScope.TASK,
            origin=GrantOrigin.PICKER,
            grant_id=GrantId.new(),
            rules=attack.rules,
            now=NOW,
        )

        if touches:
            assert isinstance(decision, Deny), (text, resolved.text)
            assert isinstance(decision.reason, StaticDeny)
            assert isinstance(issuance, Deny)
            assert isinstance(issuance.reason, StaticDeny)
        else:
            # the rules are not a blanket refusal: an ordinary existing path can be granted
            exists = Path(resolved.text).is_dir() or Path(resolved.text).is_file()
            assume(exists)
            assert isinstance(decision, NeedsPrompt), (text, resolved.text, decision)
            assert isinstance(issuance, Issued)

    @PROPERTY
    @given(data=st.data())
    def test_a_grant_never_covers_what_touches_a_protected_place(
        self, attack: World, data: st.DataObject
    ) -> None:
        """Even with a grant on an ordinary folder in hand, a path that leads to a protected
        place by symlinks or dots is not allowed."""
        text = data.draw(escape_attempts(attack))
        try:
            resolve(text)
        except PathError:
            assume(False)
        task = TaskState(task=TaskId.new())
        granted = issue_grant(
            task,
            request(task.task, attack.home / "Photos"),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=attack.rules,
            now=NOW,
        )
        assert isinstance(granted, Issued)

        decision = decide(
            granted.task, [granted.grant], request(task.task, text), rules=attack.rules, now=NOW
        )

        if touches_protected(os.path.realpath(text), protected_places(attack)):
            assert isinstance(decision, Deny)
        if isinstance(decision, Allow):
            real = os.path.realpath(text)
            assert same_file(real, str(attack.home / "Photos")) or any(
                same_file(prefix, str(attack.home / "Photos")) for prefix in prefixes(real)
            )

    @PROPERTY
    @given(
        data=st.data(),
        index=st.integers(min_value=0),
        extra=st.lists(st.sampled_from(["x", "child", "a.txt", "SSH"]), max_size=3),
    )
    def test_every_entry_in_every_spelling_equal_below_and_above(
        self, shared_world: World, data: st.DataObject, index: int, extra: list[str]
    ) -> None:
        entries = list(deny_entries(shared_world.context))
        definition = entries[index % len(entries)]
        components = Path(definition.path).parts[1:]
        spelling: list[Callable[[str], str]] = [
            str,
            str.upper,
            str.title,
            fullwidth,
            lambda name: name[:1] + ZWJ + name[1:],
            lambda name: unicodedata.normalize("NFD", name),
        ]
        chosen = [data.draw(st.sampled_from(spelling)) for _ in components]
        spelled = [change(name) for change, name in zip(chosen, components, strict=True)]
        identity = FsIdentity(fsid=1, ino=2)

        for count in range(len(spelled) + 1):
            above = (
                shared_world.rules.check_text(
                    StoredPath(path="/" + "/".join(spelled[:count]), identity=identity)
                )
                if count
                else shared_world.rules.check_text(StoredPath(path="/", identity=identity))
            )
            assert above is not None, (definition.label, count)
            if count == len(spelled):
                assert above.relation is DenyRelation.EQUAL
            else:
                assert above.relation is DenyRelation.ANCESTOR
        below = shared_world.rules.check_text(
            StoredPath(path="/" + "/".join([*spelled, *extra]), identity=identity)
        )
        assert below is not None
        assert below.relation is (DenyRelation.INSIDE if extra else DenyRelation.EQUAL)


class TestFoldProperties:
    @PROPERTY
    @given(st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x24F), max_size=30))
    def test_case_variants_fold_alike(self, name: str) -> None:
        assert fold_name(name.upper()) == fold_name(name.lower()) == fold_name(name)

    @PROPERTY
    @given(st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x24F), max_size=30))
    def test_normalisation_forms_fold_alike(self, name: str) -> None:
        forms = {
            fold_name(unicodedata.normalize(form, name)) for form in ("NFC", "NFD", "NFKC", "NFKD")
        }

        assert len(forms) == 1


def workspace_oracle(root: Path, names: list[str]) -> PathFault | FileKind:
    """What opening `names` below `root` must do, from an `lstat` walk: the fault of the first
    thing that is wrong, or the kind of what is there."""
    import stat as stat_module

    current = root
    for index, name in enumerate(names):
        current = current / name
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return PathFault.MISSING
        if stat_module.S_ISLNK(mode):
            return PathFault.SYMLINK
        last = index == len(names) - 1
        if not last and not stat_module.S_ISDIR(mode):
            return PathFault.WRONG_KIND
        if last:
            if stat_module.S_ISDIR(mode):
                return FileKind.DIRECTORY
            if stat_module.S_ISREG(mode):
                return FileKind.REGULAR
            return PathFault.SPECIAL_FILE
    raise AssertionError("no names")


WORKSPACE_NAMES = [
    "a",
    "b",
    "dir",
    "file.txt",
    "link",
    "to_dir",
    "to_up",
    "to_abs",
    "dangling",
    "fifo",
    "nothing",
    "loop",
    "deep",
]


@pytest.fixture(scope="module")
def hostile(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("hostile", numbered=True).resolve() / "ws"
    (root / "a" / "b").mkdir(parents=True)
    (root / "dir" / "deep").mkdir(parents=True)
    (root / "file.txt").write_text("x")
    (root / "a" / "file.txt").write_text("y")
    (root / "dir" / "deep" / "file.txt").write_text("z")
    (root / "link").symlink_to("file.txt")
    (root / "to_dir").symlink_to("dir")
    (root / "to_up").symlink_to("..")
    (root / "to_abs").symlink_to(root.parent)
    (root / "dangling").symlink_to("nowhere")
    (root / "loop").symlink_to("loop")
    (root / "a" / "link").symlink_to("../file.txt")
    (root / "a" / "b" / "to_up").symlink_to("../..")
    (root / "dir" / "to_dir").symlink_to("..")
    os.mkfifo(root / "fifo")
    os.mkfifo(root / "a" / "fifo")
    return root


STRATEGIES = [
    pytest.param(OpenStrategy.COMPONENTWISE, id="componentwise"),
    pytest.param(OpenStrategy.AUTO, id="auto"),
    pytest.param(OpenStrategy.OPENAT2, id="openat2", marks=pytest.mark.linux),
]


class TestWorkspaceSafeOpen:
    @pytest.mark.timeout(120)
    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_the_open_agrees_with_an_lstat_walk_and_never_follows_a_symlink(
        self, hostile: Path, strategy: OpenStrategy
    ) -> None:
        if strategy is OpenStrategy.OPENAT2:
            assert openat2_available()
        root_opened = StoredPath.record(ResolvedPath.resolve(str(hostile))).open()
        seen_faults: set[PathFault] = set()
        seen_kinds: set[FileKind] = set()

        @PROPERTY
        @given(st.lists(st.sampled_from(WORKSPACE_NAMES), min_size=1, max_size=6))
        def check(names: list[str]) -> None:
            expected = workspace_oracle(hostile, names)
            rel = WorkspaceRelPath("/".join(names))
            opened: OpenedPath | None = None
            fault: PathFault | None = None
            try:
                opened = open_in_workspace(root_opened, rel, strategy=strategy)
            except PathError as error:
                fault = error.fault
            if opened is None:
                assert fault is expected, (names, fault, expected)
                seen_faults.add(fault)
            else:
                with opened:
                    assert opened.kind is expected, (names, opened.kind, expected)
                    seen_kinds.add(opened.kind)
                    assert opened.identity.ino == hostile.joinpath(*names).stat().st_ino

        try:
            check()
        finally:
            root_opened.close()

        # not vacuous: successes of both kinds and every refusal the walk can produce
        assert seen_kinds == {FileKind.DIRECTORY, FileKind.REGULAR}
        assert seen_faults >= {
            PathFault.SYMLINK,
            PathFault.MISSING,
            PathFault.WRONG_KIND,
            PathFault.SPECIAL_FILE,
        }


class TestTaintProperties:
    """Whatever happens to a task, I12 holds: never both, and taint only grows."""

    OPERATIONS: ClassVar[list[str]] = [
        "issue_file_once",
        "issue_file_task",
        "issue_file_tool",
        "issue_net_once",
        "issue_net_task",
        "issue_net_tool",
        "store_tool_file",
        "store_tool_net",
        "apply_tools",
        "expire",
        "revoke",
        "ask_file",
        "ask_net",
        "successor",
        "successor_fresh",
    ]

    @PROPERTY
    @given(st.lists(st.sampled_from(OPERATIONS), min_size=1, max_size=25))
    def test_never_both_taints_only_grow_and_no_allow_crosses_them(
        self, shared_world: World, operations: list[str]
    ) -> None:
        world = shared_world
        photos = world.home / "Photos"
        tool = ToolId.new()
        run = run_context(RunTrigger.USER_EVENT, tool=tool)  # a user-triggered run of `tool`
        task = TaskState(task=TaskId.new())
        grants: list[Grant] = []
        now = NOW

        def asking(kind: str) -> AccessRequest:
            if kind == "net":
                return request(task.task, None, capability=CapabilityKind.NET_FULL, run=run)
            return request(task.task, photos, run=run)

        for operation in operations:
            now += dt.timedelta(seconds=1)
            before = task.taint
            verb, _, rest = operation.partition("_")
            kind, _, scope = rest.partition("_")
            if verb == "issue":
                result = issue_grant(
                    task,
                    asking(kind),
                    scope=GrantScope(scope),
                    origin=GrantOrigin.PROMPT,
                    grant_id=GrantId.new(),
                    rules=world.rules,
                    now=now,
                    expires_at=now + dt.timedelta(seconds=30),
                )
                if isinstance(result, Issued):
                    task = result.task
                    grants.append(result.grant)
            elif verb == "store":
                # a tool grant that was issued earlier, in some other task, found in the store
                capability: Capability = (
                    NetFull() if kind == "net" else FsRead(path=StoredPath.record(resolve(photos)))
                )
                assert run.tool is not None
                grants.append(
                    ToolGrant(
                        id=GrantId.new(),
                        capability=capability,
                        origin=GrantOrigin.PROMPT,
                        issued_at=now,
                        tool=tool,
                        content_hash=run.tool.content_hash,
                    )
                )
            elif verb == "apply":  # the run starts: its tool grants are applied to the task
                selection = select_grants(task, run, grants, rules=world.rules, now=now)
                task = TaskState(task=task.task, taint=selection.task_taint)
            elif verb == "expire":
                now += dt.timedelta(minutes=5)
            elif verb == "revoke":
                grants.clear()
            elif verb == "ask":
                decision = decide(task, grants, asking(kind), rules=world.rules, now=now)
                if isinstance(decision, Allow):
                    # an allowed request is never one the task's taint forbids
                    assert other_taint(taint_of(asking(kind).capability)) not in decision.task_taint
                    task = TaskState(task=task.task, taint=decision.task_taint)
            elif operation == "successor":
                task = task.end().successor(TaskId.new(), fresh_context=False)
                grants.clear()
            elif operation == "successor_fresh":
                task = task.end().successor(TaskId.new(), fresh_context=True)
                grants.clear()
                before = frozenset()
            # TaskState refuses to hold both, so reaching here already means "never both"
            assert before <= task.taint, (operation, before, task.taint)
