"""`SttEndpoint`: the speech server's URL is validated wherever one is made, and only its
`scheme://host:port` form is ever shown (I6)."""

import pytest

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.voice.endpoint import MAX_URL_CHARS, SttEndpoint


@pytest.mark.parametrize(
    ("url", "inference", "display"),
    [
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/inference", "http://127.0.0.1:8080"),
        ("http://localhost:8080/", "http://localhost:8080/inference", "http://localhost:8080"),
        (
            "https://stt.example.com",
            "https://stt.example.com/inference",
            "https://stt.example.com:443",
        ),
        ("http://host.local", "http://host.local/inference", "http://host.local:80"),
        (
            "http://10.0.2.2:9000/whisper/",
            "http://10.0.2.2:9000/whisper/inference",
            "http://10.0.2.2:9000",
        ),
        ("http://[::1]:8080", "http://[::1]:8080/inference", "http://[::1]:8080"),
        (
            "HTTP://Example.COM:81/a-b_c.d~e",
            "http://example.com:81/a-b_c.d~e/inference",
            "http://example.com:81",
        ),
    ],
)
def test_a_good_url_becomes_the_inference_url_and_a_display_form(
    url: str, inference: str, display: str
) -> None:
    endpoint = SttEndpoint(url)

    assert endpoint.inference_url == inference
    assert endpoint.display == display


@pytest.mark.parametrize(
    "url",
    [
        "",
        " ",
        "http://",
        "http:///x",
        "127.0.0.1:8080",
        "localhost",
        "ftp://example.com",
        "file:///etc/passwd",
        "ws://example.com",
        "javascript:alert(1)",
        "http://user@example.com",
        "http://user:secret@example.com",
        "http://:secret@example.com",
        "http://example.com?x=1",
        "http://example.com/?",
        "http://example.com/#frag",
        "http://example.com#",
        "http://example.com:0",
        "http://example.com:65536",
        "http://example.com:abc",
        "http://example.com:-1",
        "http://exa mple.com",
        "http://example.com/a b",
        "http://example.com/\x00",
        "http://example.com/\n",
        "http://example.com/\N{RIGHT-TO-LEFT OVERRIDE}",
        "http://example.com/../x",
        "http://example.com/a/../x",
        "http://example.com/%2e%2e/x",
        "http://example.com/a\\b",
        "http://exa<b>mple.com",
        "http://exa_mple.com@evil.example",
        "http://-bad-.example",
        "http://[fe80::1%25en0]:80",
        "http://[::1",
        "http://éxample.com",
        "http://example.com/" + "a" * MAX_URL_CHARS,
    ],
)
def test_a_bad_url_is_refused(url: str) -> None:
    with pytest.raises(InvalidValueError):
        SttEndpoint(url)


@pytest.mark.parametrize("bad", [None, 5, b"http://x.example", ["http://x.example"]])
def test_only_text_is_accepted(bad: object) -> None:
    with pytest.raises(InvalidValueError, match="must be text"):
        SttEndpoint(bad)  # type: ignore[arg-type]  # the point


def test_an_error_never_repeats_the_url_it_refuses() -> None:
    for url in (
        "http://user:hunter2@example.com",
        "http://example.com/?token=hunter2",
        "ftp://hunter2",
    ):
        with pytest.raises(InvalidValueError) as raised:
            SttEndpoint(url)

        assert "hunter2" not in str(raised.value)


def test_printing_an_endpoint_shows_no_path_and_no_credentials() -> None:
    endpoint = SttEndpoint("http://10.0.0.5:8080/private-prefix/")

    assert "private-prefix" not in repr(endpoint) + str(endpoint) + endpoint.display
    assert repr(endpoint) == "SttEndpoint(http://10.0.0.5:8080)"


@pytest.mark.parametrize(
    ("url", "in_clear"),
    [
        ("http://127.0.0.1:8080", False),
        ("http://localhost", False),
        ("http://[::1]:8080", False),
        ("https://10.0.0.5", False),
        ("http://10.0.0.5:8080", True),
        ("http://stt.example.com", True),
    ],
)
def test_it_knows_when_audio_would_cross_a_network_unencrypted(url: str, in_clear: bool) -> None:
    assert SttEndpoint(url).sends_audio_in_clear is in_clear
