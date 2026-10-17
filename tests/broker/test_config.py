"""The broker's configuration, and the deny entries it adds to the policy core's (S17)."""

import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import scratch_home
from tests.broker.support import World, make_world, resolve
from tumbleweed.broker import DenyRelation, DenyRules
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.deny import DenyMatch
from tumbleweed.core.errors import InvalidValueError


@pytest.fixture
def home_world() -> Iterator[World]:
    with scratch_home() as root:
        yield make_world(root)


def config_for(world: World, **changes: object) -> BrokerConfig:
    base: dict[str, object] = {
        "data_home": world.base / "data",
        "socket_directory": world.base / "sockets",
        "deny": world.context,
    }
    return BrokerConfig(**(base | changes))  # type: ignore[arg-type]


def rules_for(world: World, environ: dict[str, str] | None = None) -> DenyRules:
    return DenyRules.build(config_for(world).deny_context(environ or {}))


def check(rules: DenyRules, path: str | Path) -> DenyMatch | None:
    return rules.check(resolve(path))


class TestWhatTheSandboxBuildsItself:
    """The sandbox refuses a grant at or inside these; the broker refuses it at decision time
    (#79), so that a prompt is never shown for something that cannot be run."""

    @pytest.fixture
    def under_tmp(self) -> Iterator[Path]:
        directory = Path(tempfile.mkdtemp(prefix="twcfg-", dir="/tmp"))
        (directory / "inner").mkdir()
        try:
            yield directory
        finally:
            (directory / "inner").rmdir()
            directory.rmdir()

    def test_a_directory_inside_tmp_is_refused(self, home_world: World, under_tmp: Path) -> None:
        match = check(rules_for(home_world), under_tmp)

        assert match is not None
        assert match.relation in {DenyRelation.INSIDE, DenyRelation.EQUAL}

    def test_a_directory_deep_inside_tmp_is_refused(
        self, home_world: World, under_tmp: Path
    ) -> None:
        assert check(rules_for(home_world), under_tmp / "inner") is not None

    def test_tmp_itself_is_refused(self, home_world: World) -> None:
        assert check(rules_for(home_world), "/tmp") is not None

    def test_the_control_without_the_broker_entries_would_have_allowed_it(
        self, home_world: World, under_tmp: Path
    ) -> None:
        # The policy core's own rules do not know the sandbox's /tmp: that is what S17 adds.
        assert check(home_world.rules, under_tmp) is None

    @pytest.mark.parametrize("path", ["/usr/share", "/usr", "/etc"])
    def test_the_runtime_and_system_paths_are_refused(self, home_world: World, path: str) -> None:
        if not Path(path).exists():
            pytest.skip(f"{path} does not exist on this host")

        assert check(rules_for(home_world), path) is not None

    def test_paths_the_sandbox_makes_are_refused_even_if_they_do_not_exist(
        self, home_world: World
    ) -> None:
        rules = rules_for(home_world)

        for path in ("/workspace", "/workspace/x", "/inputs", "/inputs/conn_x/y"):
            assert check(rules, path) is not None, path

    def test_an_ordinary_directory_of_the_user_is_not_refused(self, home_world: World) -> None:
        rules = rules_for(home_world)

        assert check(rules, home_world.home / "Photos") is None
        assert check(rules, home_world.home / "Documents" / "notes.txt") is None


class TestWhatTheBrokerKnows:
    def test_its_own_directories_are_refused_wherever_they_are(self, home_world: World) -> None:
        config = config_for(home_world)
        config.broker_directory.mkdir(parents=True)
        config.workspaces_directory.mkdir(parents=True)
        (home_world.base / "sockets").mkdir()
        rules = DenyRules.build(config.deny_context({}))

        for directory in (
            config.broker_directory,
            config.workspaces_directory,
            home_world.base / "sockets",
        ):
            assert check(rules, directory) is not None, directory
            assert check(rules, directory / "inside") is not None, directory

    def test_the_agent_socket_directory_is_refused(self, home_world: World) -> None:
        agent = home_world.base / "agent-dir"
        agent.mkdir()
        environ = {"SSH_AUTH_SOCK": str(agent / "agent.1234")}

        rules = rules_for(home_world, environ)

        assert check(rules, agent) is not None
        assert check(home_world.rules, agent) is None  # nothing but this entry refuses it

    @pytest.mark.parametrize("value", ["", "relative/agent.sock", "/agent.sock", "agent"])
    def test_an_agent_socket_that_names_no_directory_adds_nothing(
        self, home_world: World, value: str
    ) -> None:
        with_it = config_for(home_world).deny_context({"SSH_AUTH_SOCK": value})
        without = config_for(home_world).deny_context({})

        assert with_it.extra == without.extra
        # And the root of the file system, which would refuse everything, is not an entry:
        assert check(DenyRules.build(with_it), home_world.home / "Photos") is None

    def test_the_users_own_extra_entries_are_kept(self, home_world: World) -> None:
        from tumbleweed.broker.deny import DenyContext, ExtraDeny

        mine = ExtraDeny(label="mine", path=str(home_world.base / "mine"))
        context = DenyContext(
            homes=home_world.context.homes,
            config_home=home_world.context.config_home,
            data_home=home_world.context.data_home,
            state_home=home_world.context.state_home,
            cache_home=home_world.context.cache_home,
            extra=(mine,),
        )

        final = config_for(home_world, deny=context).deny_context({})

        assert final.extra[0] == mine
        assert len(final.extra) > 1

    def test_the_context_is_rebuilt_not_edited(self, home_world: World) -> None:
        config = config_for(home_world)

        first = config.deny_context({})
        second = config.deny_context({})

        assert first == second
        assert first is not config.deny
        assert config.deny.extra == ()


class TestTheSettings:
    def test_the_target_requirements_are_the_defaults(self, home_world: World) -> None:
        config = config_for(home_world)

        assert config.require_landlock is True
        assert config.require_scope_limits is True
        assert config.retention == timedelta(days=7)

    def test_the_directories_are_the_ones_of_the_architecture(self, home_world: World) -> None:
        config = config_for(home_world)

        assert config.broker_directory == home_world.base / "data" / "tumbleweed" / "broker"
        assert config.workspaces_directory == home_world.base / "data" / "tumbleweed" / "workspaces"

    @pytest.mark.parametrize(
        "changes",
        [
            {"require_landlock": 1},
            {"require_scope_limits": "yes"},
            {"retention": timedelta(0)},
            {"retention": 7},
            {"fs_timeout": timedelta(hours=1)},
            {"max_live_tasks": 0},
            {"max_live_tasks": True},
            {"max_concurrent_runs": 1000},
            {"max_runs_per_task": 0},
            {"sandbox_path": ""},
            {"sandbox_path": "a\0b"},
        ],
    )
    def test_a_setting_outside_its_bounds_is_refused(
        self, home_world: World, changes: dict[str, object]
    ) -> None:
        with pytest.raises(InvalidValueError):
            config_for(home_world, **changes)
