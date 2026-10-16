"""The JSON codec of frames: strict in what it accepts, bounded in what it says about it."""

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tumbleweed.core.ids import IpcRequestId, TaskId
from tumbleweed.ipc.errors import ErrorCode, MalformedFrameError
from tumbleweed.ipc.messages import Echo, Echoed, IpcMessage, Ping, Pong
from tumbleweed.ipc.wire import (
    PROTOCOL_VERSION,
    ErrorFrame,
    Frame,
    Hello,
    ProtocolVersion,
    RequestFrame,
    ResponseFrame,
    decode_message,
    encode_message,
)

PROPERTY = settings(derandomize=True, deadline=None, database=None, max_examples=300)
RID = IpcRequestId.new()


def valid_frames() -> list[Frame]:
    return [
        Hello(kind="hello", version=PROTOCOL_VERSION),
        Hello(kind="hello", version=ProtocolVersion(65535)),
        RequestFrame(kind="request", id=RID, body=Ping(kind="ping")),
        RequestFrame(kind="request", id=RID, body=Echo(kind="echo", text="héllo \U0001f600 \x00")),
        ResponseFrame(kind="response", id=RID, body=Pong(kind="pong")),
        ResponseFrame(kind="response", id=RID, body=Echoed(kind="echoed", text="")),
        *(
            ErrorFrame(kind="error", id=error_id, code=code, message="why")
            for code in ErrorCode
            for error_id in (RID, None)
        ),
    ]


def j(**fields: object) -> bytes:
    return json.dumps(fields).encode()


def request_json(body: object) -> bytes:
    return j(kind="request", id=str(RID), body=body)


class TestRoundTrip:
    @pytest.mark.parametrize("frame", valid_frames())
    def test_every_kind_of_frame_survives_encoding_and_decoding(self, frame: Frame) -> None:
        assert decode_message(encode_message(frame)) == frame

    def test_the_encoding_is_compact_utf8_json_with_the_documented_shape(self) -> None:
        frame = RequestFrame(kind="request", id=RID, body=Echo(kind="echo", text="é"))

        assert encode_message(frame) == (
            f'{{"kind":"request","id":"{RID}","body":{{"kind":"echo","text":"é"}}}}'.encode()
        )

    def test_an_error_frame_without_an_id_says_null_not_nothing(self) -> None:
        frame = ErrorFrame(kind="error", id=None, code=ErrorCode.INTERNAL, message="m")

        assert json.loads(encode_message(frame))["id"] is None

    def test_the_ids_on_the_wire_are_request_ids_in_their_serialized_form(self) -> None:
        assert str(RID).startswith("req_")
        assert json.loads(
            encode_message(RequestFrame(kind="request", id=RID, body=Ping(kind="ping")))
        )["id"] == str(RID)

    @PROPERTY
    @given(text=st.text(max_size=1024))
    def test_any_text_in_an_echo_survives(self, text: str) -> None:
        frame = RequestFrame(kind="request", id=RID, body=Echo(kind="echo", text=text))

        assert decode_message(encode_message(frame)) == frame


# Each payload is a frame that is wrong in exactly one way.
REFUSED = {
    "not json": b"hello",
    "empty": b"",
    "only whitespace": b"  \n",
    "an array": b"[]",
    "a string": b'"hello"',
    "a number": b"1",
    "null": b"null",
    "an object without kind": j(version=1),
    "an unknown kind": j(kind="goodbye"),
    "a kind of the wrong type": j(kind=1),
    "an unknown field in a hello": j(kind="hello", version=1, extra=True),
    "no version": j(kind="hello"),
    "a string version": j(kind="hello", version="1"),
    "a boolean version": j(kind="hello", version=True),
    "a float version": j(kind="hello", version=1.0),
    "a null version": j(kind="hello", version=None),
    "version zero": j(kind="hello", version=0),
    "a negative version": j(kind="hello", version=-1),
    "a version over the maximum": j(kind="hello", version=65536),
    "a request without an id": j(kind="request", body={"kind": "ping"}),
    "a request with a null id": j(kind="request", id=None, body={"kind": "ping"}),
    "a request with an id of another type": j(
        kind="request", id=str(TaskId.new()), body={"kind": "ping"}
    ),
    "a request id with the wrong case": j(
        kind="request", id=str(RID).upper(), body={"kind": "ping"}
    ),
    "a request id that is a number": j(kind="request", id=1, body={"kind": "ping"}),
    "a request without a body": j(kind="request", id=str(RID)),
    "a request with an unknown body kind": request_json({"kind": "launch_missiles"}),
    "a request with a response body": request_json({"kind": "pong"}),
    "a request body without a kind": request_json({"text": "x"}),
    "a request body with an extra field": request_json({"kind": "ping", "extra": 1}),
    "an echo without text": request_json({"kind": "echo"}),
    "an echo with a non-string text": request_json({"kind": "echo", "text": 5}),
    "an echo over the text limit": request_json({"kind": "echo", "text": "x" * 1025}),
    "a response with a request body": j(kind="response", id=str(RID), body={"kind": "ping"}),
    "an error without a code": j(kind="error", id=None, message="m"),
    "an error with an unknown code": j(kind="error", id=None, code="melted", message="m"),
    "an error with a non-string code": j(kind="error", id=None, code=3, message="m"),
    "an error without a message": j(kind="error", id=None, code="internal"),
    "an error message over the limit": j(kind="error", id=None, code="internal", message="m" * 257),
    "an error without the id field": j(kind="error", code="internal", message="m"),
    "duplicate keys at the top": b'{"kind":"hello","version":1,"version":2}',
    "duplicate keys that disagree on the kind": b'{"kind":"hello","kind":"hello","version":1}',
    "duplicate keys in a body": (
        b'{"kind":"request","id":"%s","body":{"kind":"ping","kind":"ping"}}' % str(RID).encode()
    ),
    "trailing text": b'{"kind":"hello","version":1} x',
    "two frames": b'{"kind":"hello","version":1}{"kind":"hello","version":1}',
    "a byte order mark": b"\xef\xbb\xbf" + b'{"kind":"hello","version":1}',
    "invalid UTF-8": b'{"kind":"hello","version":1,"x":"\xff"}',
    "a lone surrogate escape": b'{"kind":"echo","text":"\\ud800"}',
    "NaN": b'{"kind":"hello","version":NaN}',
    "Infinity": b'{"kind":"hello","version":Infinity}',
    "minus Infinity": b'{"kind":"hello","version":-Infinity}',
    "a number too large for a float": b'{"kind":"hello","version":1e999}',
    "a truncated object": b'{"kind":"hello","version":1',
    "a NUL byte": b'{"kind":"hello"\x00,"version":1}',
    "single quotes": b"{'kind':'hello','version':1}",
}


class TestRefusal:
    @pytest.mark.parametrize("payload", REFUSED.values(), ids=list(REFUSED))
    def test_a_payload_that_is_not_exactly_a_frame_is_malformed(self, payload: bytes) -> None:
        with pytest.raises(MalformedFrameError):
            decode_message(payload)

    def test_each_refused_payload_has_a_valid_twin_so_the_refusal_is_about_the_flaw(self) -> None:
        # Guards the table above against everything being refused for one shared reason.
        assert decode_message(request_json({"kind": "ping"})) == RequestFrame(
            kind="request", id=RID, body=Ping(kind="ping")
        )
        assert decode_message(b'{"kind":"hello","version":1}\n') == Hello(
            kind="hello", version=PROTOCOL_VERSION
        )
        assert isinstance(
            decode_message(request_json({"kind": "echo", "text": "x" * 1024})), RequestFrame
        )
        assert isinstance(
            decode_message(j(kind="error", id=None, code="internal", message="m" * 256)),
            ErrorFrame,
        )

    @pytest.mark.parametrize(
        "payload",
        [
            b"[" * 1_000_000,
            b'{"a":' * 200_000,
            b'{"kind":"echo","text":' + b"[" * 500_000 + b"]" * 500_000 + b"}",
            b'{"kind":"hello","version":' + b"9" * 1_000_000 + b"}",
            b'{"kind":"hello","version":1,"' + b"k" * 1_000_000 + b'":1}',
            b'{"kind":"echo","text":"' + b"x" * 1_000_000 + b'"}',
        ],
        ids=[
            "a million open brackets",
            "nested objects",
            "nested arrays inside a field",
            "a million-digit number",
            "a million-byte key",
            "a million-byte string",
        ],
    )
    def test_hostile_shapes_at_the_frame_limit_are_malformed_not_a_crash(
        self, payload: bytes
    ) -> None:
        with pytest.raises(MalformedFrameError):
            decode_message(payload)

    def test_the_error_message_never_repeats_what_the_peer_sent(self) -> None:
        secret = "SECRET-" + "k" * 5000
        payloads = [
            j(kind="hello", version=1, **{secret: 1}),
            j(kind="hello", version=secret),
            j(kind=secret),
            j(kind="error", id=secret, code="internal", message="m"),
            request_json({"kind": secret}),
        ]

        for payload in payloads:
            with pytest.raises(MalformedFrameError) as raised:
                decode_message(payload)
            assert "SECRET" not in str(raised.value)
            assert len(str(raised.value)) < 200

    def test_text_that_is_not_utf8_cannot_get_into_a_message_to_be_encoded(self) -> None:
        with pytest.raises(ValidationError, match="unicode"):
            Echo(kind="echo", text="\ud800")


class WithAFloat(IpcMessage):
    """A message with a float field, which the protocol does not have yet (a later step may)."""

    x: float


class TestNonFiniteNumbers:
    @pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
    def test_a_float_field_refuses_them_on_the_wire(self, token: str) -> None:
        with pytest.raises(ValidationError):
            WithAFloat.model_validate_json(f'{{"x": {token}}}')

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_and_when_a_message_is_built(self, value: float) -> None:
        with pytest.raises(ValidationError):
            WithAFloat(x=value)

    def test_the_control_a_finite_number_is_accepted(self) -> None:
        assert WithAFloat.model_validate_json('{"x": 1.5}').x == 1.5
        assert WithAFloat.model_validate_json('{"x": 1e300}').x == 1e300


class TestProperties:
    @PROPERTY
    @given(data=st.binary(max_size=600))
    def test_arbitrary_bytes_are_a_frame_or_malformed_and_nothing_else(self, data: bytes) -> None:
        try:
            frame = decode_message(data)
        except MalformedFrameError:
            return
        assert decode_message(encode_message(frame)) == frame

    @PROPERTY
    @given(
        value=st.recursive(
            st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=20),
            lambda inner: (
                st.lists(inner, max_size=4)
                | st.dictionaries(st.text(max_size=8), inner, max_size=4)
            ),
            max_leaves=20,
        )
    )
    def test_arbitrary_json_is_a_frame_or_malformed_and_nothing_else(self, value: object) -> None:
        payload = json.dumps(value, allow_nan=True).encode()
        try:
            frame = decode_message(payload)
        except MalformedFrameError:
            return
        assert decode_message(encode_message(frame)) == frame

    @PROPERTY
    @given(
        frame=st.sampled_from(valid_frames()),
        key=st.sampled_from(["kind", "id", "body", "version", "code", "message", "text"]),
        replacement=st.recursive(
            st.none() | st.booleans() | st.integers() | st.text(max_size=10),
            lambda inner: (
                st.lists(inner, max_size=3)
                | st.dictionaries(st.text(max_size=5), inner, max_size=3)
            ),
            max_leaves=6,
        ),
    )
    def test_a_valid_frame_with_one_field_replaced_is_a_frame_or_malformed(
        self, frame: Frame, key: str, replacement: object
    ) -> None:
        document = json.loads(encode_message(frame))
        if isinstance(document.get("body"), dict) and key in {"text", "kind"}:
            document["body"][key] = replacement
        else:
            document[key] = replacement
        try:
            decoded = decode_message(json.dumps(document).encode())
        except MalformedFrameError:
            return
        assert decode_message(encode_message(decoded)) == decoded
