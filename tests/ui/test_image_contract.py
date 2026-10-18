"""The resolver contract, run on every implementation there is in this step: the renderer's fake
and a real directory. S19's resolver adds its own subclass of `ResolverContract` here or next to
the broker's tests."""

from pathlib import Path

from tests.ui.image_contract import DirfdWorkspace, ResolverContract, Workspace
from tests.ui.image_support import FakeResolver


class TestTheFakeResolver(ResolverContract):
    def make_workspace(self, tmp_path: Path) -> Workspace:
        return FakeResolver()


class TestADirectoryResolver(ResolverContract):
    def make_workspace(self, tmp_path: Path) -> Workspace:
        return DirfdWorkspace(tmp_path)
