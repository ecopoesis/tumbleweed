"""Tabs and the expander: plain titles, children built like any child, no popup (I4, I5, I9)."""

from typing import TYPE_CHECKING

import pytest

from tests.shell.gtk_support import labels_in
from tests.ui.gtk.support import labels_text, of_type, render, spec_of
from tests.ui.runtime.support import text
from tests.ui.spec.support import Json

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk import RenderedTool
    from tumbleweed.ui.gtk.context import BuildContext
    from tumbleweed.ui.spec.components import Component

pytestmark = pytest.mark.gtk

MARKUP = "<b>bold</b> _under <a href='file:///x'>l</a> &amp;"


def tabs_root(*titles: str) -> Json:
    return {
        "kind": "tabs",
        "pages": [
            {"title": title, "content": {"kind": "label", "text": f"page {index}"}}
            for index, title in enumerate(titles)
        ],
    }


def page_of(notebook: "Gtk.Notebook", index: int) -> "Gtk.Widget":
    page = notebook.get_nth_page(index)
    assert page is not None
    return page


def title_of(notebook: "Gtk.Notebook", index: int) -> "Gtk.Label":
    from tumbleweed.shell._gi import Gtk

    label = notebook.get_tab_label(page_of(notebook, index))
    assert isinstance(label, Gtk.Label)
    return label


def all_texts(tool: "RenderedTool") -> list[str]:
    return [label.get_text() for label in labels_in(tool.widget)]


def failing(component: "Component", path: str, ctx: "BuildContext") -> "Gtk.Widget":
    raise RuntimeError("the widget could not be built")


class TestTabs:
    def test_each_page_is_a_page_of_a_notebook_with_its_title_and_content(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(spec_of(tabs_root("First", "Second", "Third")))
        (notebook,) = of_type(tool, Gtk.Notebook)

        assert tool.widget is notebook
        assert notebook.get_n_pages() == 3
        for index in range(3):
            assert tool.widget_at(f"root.pages[{index}].content") is page_of(notebook, index)
        assert [title_of(notebook, i).get_text() for i in range(3)] == ["First", "Second", "Third"]

    def test_only_the_current_page_is_shown_and_a_tab_switches_it(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(spec_of(tabs_root("a", "b")))
        (notebook,) = of_type(tool, Gtk.Notebook)

        assert notebook.get_current_page() == 0
        notebook.set_current_page(1)

        assert notebook.get_current_page() == 1
        assert labels_text(tool)[1] == "page 1"

    def test_a_title_is_plain_text_never_markup_or_a_mnemonic(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(spec_of(tabs_root(MARKUP)))
        (notebook,) = of_type(tool, Gtk.Notebook)
        label = title_of(notebook, 0)

        assert label.get_text() == MARKUP
        assert label.get_use_markup() is False
        assert label.get_use_underline() is False

    def test_a_long_title_is_one_ellipsized_line(self) -> None:
        from tumbleweed.shell._gi import Gtk, Pango

        tool = render(spec_of(tabs_root("W" * 200)))
        (notebook,) = of_type(tool, Gtk.Notebook)
        label = title_of(notebook, 0)

        assert label.get_wrap() is False
        assert label.get_ellipsize() == Pango.EllipsizeMode.END
        assert label.get_max_width_chars() == 32

    def test_there_is_no_tab_menu_and_no_reordering_or_detaching(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(spec_of(tabs_root("a", "b")))
        (notebook,) = of_type(tool, Gtk.Notebook)
        page = page_of(notebook, 0)

        assert notebook.get_property("enable-popup") is False
        assert notebook.get_tab_reorderable(page) is False
        assert notebook.get_tab_detachable(page) is False
        assert notebook.get_menu_label(page) is None

    def test_a_page_that_fails_is_a_placeholder_in_its_tab_and_the_others_render(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        root: Json = {
            "kind": "tabs",
            "pages": [
                {"title": "ok", "content": {"kind": "label", "text": "fine"}},
                {"title": "bad", "content": {"kind": "separator"}},
                {"title": "also ok", "content": {"kind": "label", "text": "fine too"}},
            ],
        }
        tool = RenderedTool(spec_of(root), factories={"separator": failing})
        (notebook,) = of_type(tool, Gtk.Notebook)

        bad = page_of(notebook, 1)
        assert isinstance(bad, Gtk.Label)
        assert bad.get_text() == "A separator could not be shown."
        assert notebook.get_n_pages() == 3
        assert labels_text(tool)[0] == "fine"

    def test_a_tabs_node_that_fails_takes_its_pages_with_it(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        root: Json = {"kind": "box", "children": [tabs_root("a"), {"kind": "label", "text": "x"}]}
        tool = RenderedTool(spec_of(root), factories={"tabs": failing})

        assert labels_text(tool) == ["A tabs could not be shown.", "x"]
        assert tool.widget_at("root.children[0].pages[0].content") is None

    def test_the_titles_count_in_the_tools_text_budget(self) -> None:
        tool = render(spec_of(tabs_root("one", "three")))

        assert tool.shown_text_total == len("one") + len("three") + 2 * len("page 0")

    def test_64_pages_of_200_character_titles_fit_and_a_deeper_page_is_built_too(self) -> None:
        from tumbleweed.shell._gi import Gtk

        deep: Json = {
            "kind": "tabs",
            "pages": [{"title": "deep", "content": {"kind": "label", "text": "d"}}],
        }
        root: Json = {
            "kind": "tabs",
            "pages": [
                {
                    "title": f"{i:02d}" + "t" * 198,
                    "content": deep if i == 0 else {"kind": "label", "text": "p"},
                }
                for i in range(64)
            ],
        }
        tool = render(spec_of(root))
        (outer, inner) = of_type(tool, Gtk.Notebook)

        assert outer.get_n_pages() == 64
        assert inner.get_n_pages() == 1
        assert tool.widget_at("root.pages[0].content.pages[0].content") is not None


class TestExpander:
    def root(self, *, expanded: bool | None = None, title: str = "More") -> Json:
        expander: Json = {
            "kind": "expander",
            "title": title,
            "child": {"kind": "label", "text": "hidden text"},
        }
        if expanded is not None:
            expander["expanded"] = expanded
        return expander

    def test_it_has_a_plain_title_and_its_child_below(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk.text import TextBlock

        tool = render(spec_of(self.root()))
        (expander,) = of_type(tool, Gtk.Expander)
        title = expander.get_label_widget()

        assert tool.widget is expander
        assert isinstance(title, Gtk.Label)
        assert title.get_text() == "More"
        assert expander.get_child() is tool.widget_at("root.child")
        assert all_texts(tool)[0] == "More"
        child = tool.widget_at("root.child")
        assert isinstance(child, TextBlock)
        assert child.shown_text == "hidden text"

    def test_it_is_collapsed_unless_the_spec_says_expanded(self) -> None:
        from tumbleweed.shell._gi import Gtk

        collapsed = of_type(render(spec_of(self.root())), Gtk.Expander)[0]
        opened = of_type(render(spec_of(self.root(expanded=True))), Gtk.Expander)[0]

        assert collapsed.get_expanded() is False
        assert opened.get_expanded() is True

    def test_it_can_be_opened_and_closed(self) -> None:
        from tumbleweed.shell._gi import Gtk

        (expander,) = of_type(render(spec_of(self.root())), Gtk.Expander)

        expander.set_expanded(True)
        assert expander.get_expanded() is True
        expander.set_expanded(False)
        assert expander.get_expanded() is False

    def test_the_title_is_plain_text_never_markup_or_a_mnemonic(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(spec_of(self.root(title=MARKUP)))
        (expander,) = of_type(tool, Gtk.Expander)
        title = expander.get_label_widget()

        assert isinstance(title, Gtk.Label)
        assert title.get_text() == MARKUP
        assert title.get_use_markup() is False
        assert title.get_use_underline() is False
        assert expander.get_use_markup() is False
        assert expander.get_use_underline() is False

    def test_a_child_that_fails_is_a_placeholder_under_the_title(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec_of(self.root()), factories={"label": failing})
        (expander,) = of_type(tool, Gtk.Expander)

        child = expander.get_child()
        assert isinstance(child, Gtk.Label)
        assert child.get_text() == "A label could not be shown."
        assert expander.get_label_widget() is not None

    def test_the_title_counts_in_the_budget_and_a_nested_expander_is_built(self) -> None:
        from tumbleweed.shell._gi import Gtk

        root: Json = {
            "kind": "expander",
            "title": "outer",
            "expanded": True,
            "child": {
                "kind": "expander",
                "title": "inner",
                "child": {"kind": "label", "bind": "s"},
            },
        }
        tool = render(spec_of(root, [text("s", "bound")]))

        assert len(of_type(tool, Gtk.Expander)) == 2
        assert tool.shown_text_total == len("outer") + len("inner") + len("bound")
        assert tool.widget_at("root.child.child") is not None

    def test_no_label_of_a_container_parses_markup(self) -> None:

        root: Json = {
            "kind": "box",
            "children": [tabs_root(MARKUP), self.root(title=MARKUP)],
        }
        tool = render(spec_of(root))

        labels = [label for label in labels_in(tool.widget) if label.get_text() == MARKUP]
        assert len(labels) == 2
        assert not any(label.get_use_markup() for label in labels)
