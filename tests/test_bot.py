import threading

import pytest

from bot import SecretBox, normalize_proxy, proxy_dict, proxy_label
from FunPayAPI import Runner


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("user:pass@127.0.0.1:8080", "http://user:pass@127.0.0.1:8080"),
        ("socks5://user:pass@example.org:1080", "socks5://user:pass@example.org:1080"),
        ("https://example.org:443", "https://example.org:443"),
    ],
)
def test_normalize_proxy(raw, expected):
    assert normalize_proxy(raw) == expected
    assert proxy_dict(expected) == {"http": expected, "https": expected}


@pytest.mark.parametrize("value", ["example.org", "ftp://example.org:21", "http://example.org:nope"])
def test_normalize_proxy_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_proxy(value)


def test_proxy_label_hides_credentials():
    assert proxy_label("http://secret:password@example.org:8080") == "http://example.org:8080"


def test_secret_box_round_trip_and_no_plaintext():
    box = SecretBox("test-secret")
    encrypted = box.encrypt("golden-key-value")
    assert "golden-key-value" not in encrypted
    assert box.decrypt(encrypted) == "golden-key-value"


class FakeAccount:
    is_initiated = True
    runner = None


def test_runner_threads_can_stop_without_network_requests():
    runner = Runner(FakeAccount())
    stop_event = threading.Event()
    stop_event.set()
    runner.loop(stop_event)
    assert list(runner.listen(stop_event=stop_event)) == []
