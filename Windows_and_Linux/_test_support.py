"""Shared process setup for tests that import Qt or keyboard libraries."""

import os


# Set these before importing PySide6, pynput, or application modules. Respect
# explicit choices made by the test runner while keeping default runs headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYNPUT_BACKEND", "dummy")
