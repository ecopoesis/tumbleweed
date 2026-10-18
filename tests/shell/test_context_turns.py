"""What the chat controller tells the responder about the conversation so far."""

from tests.shell.fakes import ManualResponder
from tumbleweed.agent.responder import (
    MAX_CONTEXT_TURNS,
    ContextTurn,
    ReplyDelta,
    ReplyFailed,
    ReplyFinished,
)
from tumbleweed.core.ids import MessageId
from tumbleweed.shell.viewmodel.chat import ChatController, context_turns
from tumbleweed.shell.viewmodel.transcript import MessageState, Role, Transcript, TranscriptMessage


def message(
    role: Role, text: str, state: MessageState = MessageState.COMPLETE
) -> TranscriptMessage:
    return TranscriptMessage(
        id=MessageId.new(),
        role=role,
        text=text,
        state=state,
        error="why" if state is MessageState.FAILED else None,
    )


def user(text: str) -> TranscriptMessage:
    return message(Role.USER, text)


def reply(text: str, state: MessageState = MessageState.COMPLETE) -> TranscriptMessage:
    return message(Role.ASSISTANT, text, state)


def turns(*pairs: tuple[str, str]) -> tuple[ContextTurn, ...]:
    return tuple(ContextTurn(user=u, assistant=a) for u, a in pairs)


class TestWhichMessagesAreTurns:
    def test_a_user_message_and_the_reply_that_completed(self) -> None:
        assert context_turns([user("q1"), reply("a1"), user("q2"), reply("a2")]) == turns(
            ("q1", "a1"), ("q2", "a2")
        )

    def test_a_reply_that_failed_leaves_no_turn_and_takes_its_question_with_it(self) -> None:
        messages = [user("q1"), reply("half", MessageState.FAILED), user("q2"), reply("a2")]

        assert context_turns(messages) == turns(("q2", "a2"))

    def test_a_question_nothing_answered_is_not_a_turn(self) -> None:
        assert context_turns([user("q1"), user("q2"), reply("a2")]) == turns(("q2", "a2"))
        assert context_turns([user("q1")]) == ()

    def test_a_reply_that_never_ended_is_not_a_turn(self) -> None:
        assert context_turns([user("q"), reply("so far", MessageState.STREAMING)]) == ()

    def test_a_blank_question_is_not_a_turn_the_provider_would_refuse_it(self) -> None:
        assert context_turns([user("   "), reply("a")]) == ()
        assert context_turns([user("\n"), reply("a"), user("q"), reply("b")]) == turns(("q", "b"))

    def test_an_answer_without_a_question_is_not_a_turn(self) -> None:
        assert context_turns([reply("orphan"), user("q"), reply("a")]) == turns(("q", "a"))

    def test_a_reply_with_no_text_is_not_a_turn_the_provider_would_refuse_it(self) -> None:
        assert context_turns([user("q"), reply("  \n")]) == ()

    def test_only_the_newest_turns_are_built(self) -> None:
        pairs = [(f"q{n}", f"a{n}") for n in range(MAX_CONTEXT_TURNS + 7)]
        messages = [m for q, a in pairs for m in (user(q), reply(a))]

        result = context_turns(messages)

        assert len(result) == MAX_CONTEXT_TURNS
        assert result[-1] == ContextTurn(user=pairs[-1][0], assistant=pairs[-1][1])
        assert result[0].user == "q7"


class TestWhatTheControllerSends:
    def test_it_is_the_transcript_not_a_memory_of_the_responder(self) -> None:
        responder = ManualResponder()
        transcript = Transcript()
        transcript.replace([user("restored q"), reply("restored a")])
        controller = ChatController(transcript, responder)

        controller.send("new q")

        (request,) = responder.requests
        assert request.history == turns(("restored q", "restored a"))
        assert request.text == "new q"

    def test_a_failed_reply_is_not_carried_into_the_next_request(self) -> None:
        responder = ManualResponder()
        transcript = Transcript()
        controller = ChatController(transcript, responder)
        controller.send("first")
        responder.sink.emit(ReplyFailed(reason="no network"))

        controller.send("second")

        assert responder.requests[1].history == ()

    def test_a_finished_reply_is_carried(self) -> None:
        responder = ManualResponder()
        transcript = Transcript()
        controller = ChatController(transcript, responder)
        controller.send("first")
        responder.sink.emit(ReplyDelta(text="answer"))
        responder.sink.emit(ReplyFinished())

        controller.send("second")

        assert responder.requests[1].history == turns(("first", "answer"))
