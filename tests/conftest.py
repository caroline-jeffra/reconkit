"""Shared test fixtures and environment pinning."""

import pytest


@pytest.fixture(autouse=True)
def _fixed_terminal_width(monkeypatch):
    """Pin the terminal width so Rich-rendered CLI output is stable.

    Typer renders errors through Rich, which wraps text to the detected
    terminal width. CI runners report a narrower width than a local shell, so
    an unpinned width makes assertions against error messages pass locally and
    fail in CI. 200 columns is wide enough that no message wraps.
    """
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERMINAL_WIDTH", "200")
