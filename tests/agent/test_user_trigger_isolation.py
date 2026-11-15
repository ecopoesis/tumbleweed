"""A tripwire for D30 (2) and (8): what can claim a user event, and who can revoke a tool grant.

A static check of Python is not a security boundary: it stops accidents and casual changes, and the
behaviour is proved elsewhere (the broker's tests, `test_saved_tools_e2e.py`). What it holds:

- the model's tools (`access`, `canvas`, `saved_tools`, `code`'s `run_code`, the registry) never
  name a run's origin: every broker call they make is the default, `model`, with no binding;
- only the handler executor (`agent/code.py`) and the card's access port (`agent/tool_access.py`)
  build a `RunOrigin` or a `WireBinding` anywhere in the source, and only they say `user_event`;
- only the chrome's tools port calls `revoke_tool_grant`, and no module of the agent imports it;
- (S25, D31) a value reaches an input through `connection_input` only from the canvas's endpoint,
  a run says `connection` only where `agent/code.py` builds its origin, and only the chrome's port
  removes a connection (no model tool does).
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "tumbleweed"
MAY_CLAIM_A_USER_EVENT = {"agent/code.py", "agent/tool_access.py"}
DEFINES = {"ipc/broker_messages.py", "broker/client.py", "broker/service.py", "broker/grants.py"}
MODEL_TOOLS = (
    "agent/access.py",
    "agent/canvas.py",
    "agent/composition.py",
    "agent/saved_tools.py",
    "agent/tools.py",
    "agent/loop.py",
    "agent/runtime.py",
)


def modules() -> list[tuple[str, ast.Module]]:
    return [
        (path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def names_in(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
    return found


def strings_in(tree: ast.Module) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_only_two_modules_build_an_origin_or_say_user_event() -> None:
    claimants = {
        name
        for name, tree in modules()
        if name not in DEFINES
        and (names_in(tree) & {"RunOrigin", "WireBinding"} or "user_event" in strings_in(tree))
    }

    assert claimants == MAY_CLAIM_A_USER_EVENT


def test_the_models_tools_never_name_an_origin() -> None:
    by_name = dict(modules())

    for name in MODEL_TOOLS:
        tree = by_name[name]
        assert not names_in(tree) & {"RunOrigin", "WireBinding", "origin"}, name
        assert "user_event" not in strings_in(tree), name


def test_run_code_in_the_executors_module_names_no_origin_either() -> None:
    tree = dict(modules())["agent/code.py"]
    (register,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "register_code_tools"
    ]

    inside = ast.Module(body=[register], type_ignores=[])

    assert not names_in(inside) & {"RunOrigin", "WireBinding", "origin", "_origin_of"}
    assert "user_event" not in strings_in(inside)


def test_the_tripwire_sees_a_claim_when_there_is_one() -> None:
    tree = ast.parse("from x import RunOrigin\norigin = RunOrigin(trigger='user_event')\n")

    assert names_in(tree) & {"RunOrigin"}
    assert "user_event" in strings_in(tree)


def test_nothing_but_the_chromes_port_revokes_a_tool_grant() -> None:
    callers = {
        name
        for name, tree in modules()
        if name not in DEFINES and "revoke_tool_grant" in names_in(tree)
    }

    assert callers == {"shell/broker_tools.py"}
    assert not any(name.startswith("agent/") for name in callers)


def test_only_the_canvas_endpoint_writes_a_connected_value() -> None:
    callers = {
        name
        for name, tree in modules()
        if name != "ui/runtime/runtime.py" and "connection_input" in names_in(tree)
    }

    assert callers == {"shell/canvas_tools.py"}


def test_a_run_is_started_by_a_connection_only_where_the_origin_is_built() -> None:
    claimants = {
        name
        for name, tree in modules()
        if name not in DEFINES
        and "connection" in strings_in(tree)
        and "RunOrigin" in names_in(tree)
    }

    assert claimants == {"agent/code.py"}


def test_nothing_but_the_chrome_removes_a_connection() -> None:
    callers = {
        name
        for name, tree in modules()
        if name not in DEFINES and "remove_connection" in names_in(tree)
    }

    # The chrome's own parts: the port that asks the broker, the model of the list and its panel.
    assert callers == {"shell/broker_tools.py", "shell/viewmodel/tools.py", "shell/tools_panel.py"}
    assert not any(name.startswith("agent/") for name in callers)
