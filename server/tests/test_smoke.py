"""Smoke test — package imports cleanly. Replace with real tests as we build."""

from __future__ import annotations

import friday


def test_package_imports() -> None:
    assert friday.__doc__ is not None
