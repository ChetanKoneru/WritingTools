"""Shared process setup for tests that import Qt or keyboard libraries."""

import os
import types


# Set these before importing PySide6, pynput, or application modules. Respect
# explicit choices made by the test runner while keeping default runs headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYNPUT_BACKEND", "dummy")


def fake_keyring(store):
    """Build a small in-memory keyring module for isolated credential tests."""
    keyring = types.ModuleType("keyring")
    keyring.set_password = lambda service, account, value: store.__setitem__(account, value)
    keyring.get_password = lambda service, account: store.get(account)

    def delete_password(service, account):
        if account not in store:
            raise type("PasswordDeleteError", (Exception,), {})("missing")
        del store[account]

    keyring.delete_password = delete_password
    return keyring
