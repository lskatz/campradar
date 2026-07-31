"""Shared pytest configuration.

Adds `src/` to the path so tests run against the package without requiring an
editable install first — useful in CI and for a fresh clone.
"""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class NetworkAccessAttempted(RuntimeError):
    """A test tried to open a real socket."""


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Fail any test that reaches the network, rather than letting it pass slowly.

    The suite is already offline — every adapter test drives `httpx` through a
    `MockTransport` against a recorded fixture. The problem with that being true
    only by convention is that it degrades quietly: one `httpx.get` added to a
    test during debugging, and the suite starts depending on a third party's
    HTML staying put. It then fails on a plane, in CI behind a proxy, or on the
    morning a provider redesigns their site — and it fails for reasons that have
    nothing to do with the change being tested. That is exactly the coupling
    worth designing out, because a test suite you cannot trust offline is one
    you stop running before you push.

    Making the invariant enforced rather than assumed also means the fixtures
    become the contract. When ACTIVE's real response disagrees with
    `tests/fixtures`, the fix is to re-record the fixture — a reviewable diff
    showing exactly what upstream changed — instead of a test that mysteriously
    turns red.

    Marked `@pytest.mark.allow_network` opts a test out, should a deliberate
    live contract test ever be wanted. Nothing uses it today.
    """
    if request.node.get_closest_marker("allow_network"):
        return

    def deny(*args, **kwargs):
        raise NetworkAccessAttempted(
            "This test tried to open a real network connection. Tests must run "
            "offline: drive httpx through httpx.MockTransport against a fixture "
            "in tests/fixtures/. If a live call is genuinely intended, mark the "
            "test @pytest.mark.allow_network."
        )

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: permit this test to make real network calls"
    )
