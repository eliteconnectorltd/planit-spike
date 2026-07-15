"""Unit tests for exception formatting.

The branch that matters is the empty-message case: httpx raises bare
ConnectErrors whose str() is "", which is what made the Watford dry-run
failure undiagnosable.
"""

from __future__ import annotations

from planit_spike.errors import format_exception


def test_message_present_is_used_directly():
    error = ValueError("something specific went wrong")
    assert format_exception(error) == "ValueError: something specific went wrong"


def test_empty_message_falls_back_to_cause():
    underlying = OSError("[Errno 11001] getaddrinfo failed")
    error = ConnectionError()
    error.__cause__ = underlying

    assert format_exception(error) == (
        "ConnectionError: caused by OSError: [Errno 11001] getaddrinfo failed"
    )


def test_empty_message_falls_back_to_context_when_no_cause():
    underlying = OSError("connection refused")
    error = ConnectionError()
    error.__context__ = underlying   # set by an implicit re-raise, not `raise from`

    assert format_exception(error) == (
        "ConnectionError: caused by OSError: connection refused"
    )


def test_empty_message_and_no_chain_falls_back_to_repr():
    error = ConnectionError()

    assert format_exception(error) == "ConnectionError: ConnectionError()"
