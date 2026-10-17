"""The conversation keeps the newest turns that fit its bounds, always starting with the user."""

from tumbleweed.agent.conversation import Conversation
from tumbleweed.agent.limits import MAX_HISTORY_BYTES, MAX_HISTORY_MESSAGES
from tumbleweed.agent.llm import TextBlock


def texts(conversation: Conversation) -> list[str]:
    return [b.text for m in conversation.messages for b in m.content if isinstance(b, TextBlock)]


def test_exchanges_are_kept_in_order() -> None:
    conversation = Conversation()

    conversation.commit("q1", "a1")
    conversation.commit("q2", "a2")

    assert texts(conversation) == ["q1", "a1", "q2", "a2"]
    assert [m.role for m in conversation.messages] == ["user", "assistant"] * 2


def test_the_number_of_messages_is_bounded_and_the_newest_survive() -> None:
    conversation = Conversation()

    for index in range(MAX_HISTORY_MESSAGES):  # twice as many messages as the bound
        conversation.commit(f"q{index}", f"a{index}")

    assert len(conversation.messages) == MAX_HISTORY_MESSAGES
    assert conversation.messages[0].role == "user"
    assert texts(conversation)[-2:] == [
        f"q{MAX_HISTORY_MESSAGES - 1}",
        f"a{MAX_HISTORY_MESSAGES - 1}",
    ]
    assert texts(conversation)[0] == f"q{MAX_HISTORY_MESSAGES // 2}"


def test_the_size_is_bounded_too() -> None:
    conversation = Conversation()
    big = "x" * (MAX_HISTORY_BYTES // 4)

    for _ in range(10):
        conversation.commit(big, big)

    total = sum(len(t.encode()) for t in texts(conversation))
    assert total <= MAX_HISTORY_BYTES
    assert conversation.messages[0].role == "user"
    assert len(conversation.messages) >= 2


def test_bytes_not_characters_are_counted() -> None:
    conversation = Conversation()
    wide = "é" * (MAX_HISTORY_BYTES // 4)  # two bytes each

    for _ in range(6):
        conversation.commit(wide, wide)

    assert sum(len(t.encode()) for t in texts(conversation)) <= MAX_HISTORY_BYTES
