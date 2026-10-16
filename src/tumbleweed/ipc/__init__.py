"""Typed, bounded, authenticated local RPC between the shell and the broker (D4, S13).

This package is asyncio only and never imports `gi`. The pieces, from the bottom up:

- `framing`: a 4-byte big-endian length prefix, then UTF-8 JSON; hard frame size limit.
- `messages`: the closed registry of request and response bodies. Later steps add theirs there.
- `wire`: the frames of the protocol (handshake, request, response, error) and their codec.
- `schema`: the JSON Schema of every frame, committed under `schemas/`.
- `peercred`: the same-uid check (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS).
- `sockdir`: the private socket directory and the claim on one socket path.
- `limits`: the bounds on one connection (I9).
- `handshake`: the protocol version handshake that opens a connection.
- `connection`, `server`, `client`: request/response correlation in both directions.
"""
