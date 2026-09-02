"""Shared test fixtures and environment pinning."""

import pytest


@pytest.fixture(autouse=True)
def _plain_cli_output(monkeypatch):
    """Pin Rich's rendering so assertions on CLI error text are stable.

    Typer renders errors through Rich, whose output depends on the environment
    in two ways that both break substring assertions:

    * **Colour.** Rich highlights option names, injecting escape codes *inside*
      the token — ``--from`` becomes ``\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-from``.
      GitHub Actions enables colour, so a test asserting on a message
      containing an option name passes locally and fails in CI.
    * **Width.** Rich wraps to the detected terminal width, so a long message
      splits across lines on a narrow terminal.

    Pinning both keeps these tests about the message, not its styling.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERMINAL_WIDTH", "200")
