"""A tiny ONNX model made in the test, so that nothing binary is committed: `Identity` over a
float vector of `size` numbers. It is what `onnxruntime` needs to prove that a session is made
from verified bytes (`voice.onnx`, `voice.store`)."""


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _int_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _value_info(name: str, size: int) -> bytes:
    dim = _int_field(1, size)
    shape = _field(1, dim)
    tensor = _int_field(1, 1) + _field(2, shape)  # elem_type FLOAT, shape
    return _field(1, name.encode()) + _field(2, _field(1, tensor))


def tiny_identity_model(size: int = 4) -> bytes:
    node = _field(1, b"x") + _field(2, b"y") + _field(3, b"identity") + _field(4, b"Identity")
    graph = (
        _field(1, node)
        + _field(2, b"tiny")
        + _field(11, _value_info("x", size))
        + _field(12, _value_info("y", size))
    )
    opset = _field(1, b"") + _int_field(2, 13)
    return _int_field(1, 8) + _field(8, opset) + _field(7, graph)
