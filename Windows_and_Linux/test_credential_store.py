"""
Unit tests for API key storage / legacy decoding / provider config migration.

Run with:
    QT_QPA_PLATFORM=offscreen uv run python -m unittest test_credential_store -v

These cover the three cases that matter for the credential-store migration:
  * plaintext key  -> moved into the credential store
  * enc: key       -> decoded, then moved into the credential store
  * store failure  -> key kept in config.json and still usable by the app

The suite is hermetic: every keyring interaction is mocked, so it behaves
identically on machines with or without a real SecretService backend.
"""

import sys
import types
import unittest
from unittest import mock

from PySide6 import QtWidgets

import aiprovider
from aiprovider import (
    GeminiProvider,
    decode_legacy_api_key,
    _store_api_key,
    _retrieve_api_key,
    _delete_api_key,
    PROVIDER_KEYRING_IDS,
    GEMINI_KEYRING_ID,
    OPENAI_COMPATIBLE_KEYRING_ID,
)

# One shared QApplication is required before any Qt widget is created.
_qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def make_enc_key(plaintext: str) -> str:
    """Encode a key the way v8/v9 did (XOR 0x5A + base64) for round-trip tests."""
    import base64

    xored = bytes([b ^ 0x5A for b in plaintext.encode("utf-8")])
    return "enc:" + base64.b64encode(xored).decode("ascii")


class FakeApp:
    """Minimal stand-in for WritingToolApp used by provider load/save tests."""

    def __init__(self, config=None):
        self.config = config if config is not None else {}
        self.saved = None

    def save_config(self, config):
        self.saved = config
        self.config = config


class TestDecodeLegacyApiKey(unittest.TestCase):
    def test_plaintext_passthrough(self):
        self.assertEqual(decode_legacy_api_key("sk-plain-key"), "sk-plain-key")

    def test_empty_passthrough(self):
        self.assertEqual(decode_legacy_api_key(""), "")

    def test_enc_roundtrip(self):
        original = "AIzaSyExampleKey123"
        self.assertEqual(decode_legacy_api_key(make_enc_key(original)), original)

    def test_invalid_enc_returns_none(self):
        self.assertIsNone(decode_legacy_api_key("enc:not-valid-base64!!!"))


class TestCredentialStoreHelpers(unittest.TestCase):
    def setUp(self):
        self._orig_keyring = sys.modules.get("keyring")
        self.fake = types.ModuleType("keyring")
        self.fake._store = {}
        self.fake._fail = False

        def set_password(service, account, password):
            if self.fake._fail:
                raise RuntimeError("no backend")
            self.fake._store[account] = password

        def get_password(service, account):
            if self.fake._fail:
                raise RuntimeError("no backend")
            return self.fake._store.get(account)

        def delete_password(service, account):
            if self.fake._fail:
                raise RuntimeError("no backend")
            if account not in self.fake._store:
                err = type("PasswordDeleteError", (Exception,), {})
                raise err("not found")
            del self.fake._store[account]

        self.fake.set_password = set_password
        self.fake.get_password = get_password
        self.fake.delete_password = delete_password
        sys.modules["keyring"] = self.fake

    def tearDown(self):
        if self._orig_keyring is not None:
            sys.modules["keyring"] = self._orig_keyring
        else:
            sys.modules.pop("keyring", None)

    def test_store_and_retrieve(self):
        self.assertTrue(_store_api_key("p1", "secret"))
        self.assertEqual(_retrieve_api_key("p1"), "secret")

    def test_store_reports_failure(self):
        self.fake._fail = True
        self.assertFalse(_store_api_key("p1", "secret"))
        self.assertIsNone(_retrieve_api_key("p1"))

    def test_delete_missing_is_success(self):
        # PasswordDeleteError (nothing stored) counts as success.
        self.assertTrue(_delete_api_key("never-stored"))

    def test_delete_when_unavailable_is_failure(self):
        self.fake._fail = True
        self.assertFalse(_delete_api_key("p1"))


class TestProviderLoadConfig(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp({"providers": {"Gemini (Recommended)": {}}})
        self.provider = GeminiProvider(self.app)

    def test_key_from_credential_store(self):
        with mock.patch.object(aiprovider, "_retrieve_api_key", return_value="STORE-KEY"):
            self.provider.load_config({"model_name": "gemini-flash-latest"})
            # Assert inside the patch: get_api_key() consults the credential
            # store, so the mock must still be active for the call.
            self.assertEqual(self.provider.get_api_key(), "STORE-KEY")
        self.assertEqual(self.provider.api_key, "STORE-KEY")

    def test_fallback_to_plaintext_config_key(self):
        with mock.patch.object(aiprovider, "_retrieve_api_key", return_value=None):
            self.provider.load_config(
                {"api_key": "CONFIG-KEY", "model_name": "gemini-flash-latest"}
            )
            # The key must still be usable even with no credential store.
            self.assertEqual(self.provider.get_api_key(), "CONFIG-KEY")
        self.assertEqual(self.provider.api_key, "CONFIG-KEY")

    def test_fallback_to_enc_config_key(self):
        with mock.patch.object(aiprovider, "_retrieve_api_key", return_value=None):
            self.provider.load_config(
                {
                    "api_key": make_enc_key("DECODED-KEY"),
                    "model_name": "gemini-flash-latest",
                }
            )
            self.assertEqual(self.provider.get_api_key(), "DECODED-KEY")
        self.assertEqual(self.provider.api_key, "DECODED-KEY")


class TestProviderSaveConfig(unittest.TestCase):
    def _provider_with_ui_value(self, ui_value, existing_config=None):
        config = {
            "providers": {
                "Gemini (Recommended)": existing_config
                if existing_config is not None
                else {}
            }
        }
        app = FakeApp(config)
        provider = GeminiProvider(app)
        # Simulate the Settings UI: set values, then render widgets so
        # `get_value()` has real Qt inputs to read from.
        for setting in provider.settings:
            if setting.name == "api_key":
                setting.set_value(ui_value)
            elif setting.name == "model_name":
                setting.set_value("gemini-flash-latest")
            setting.render_to_layout(QtWidgets.QVBoxLayout())
        return provider, app

    def test_store_success_erases_key_from_config(self):
        provider, app = self._provider_with_ui_value("NEW-KEY")
        with mock.patch.object(aiprovider, "_store_api_key", return_value=True):
            provider.save_config()
        self.assertNotIn("api_key", app.config["providers"]["Gemini (Recommended)"])

    def test_store_failure_keeps_key_in_config(self):
        provider, app = self._provider_with_ui_value("NEW-KEY")
        with mock.patch.object(aiprovider, "_store_api_key", return_value=False):
            provider.save_config()
        self.assertEqual(
            app.config["providers"]["Gemini (Recommended)"]["api_key"], "NEW-KEY"
        )

    def test_empty_field_preserves_existing_config_key(self):
        provider, app = self._provider_with_ui_value(
            "", existing_config={"api_key": "OLD-CONFIG-KEY"}
        )
        with mock.patch.object(aiprovider, "_store_api_key", return_value=False) as m:
            provider.save_config()
        # Empty field + keyring down must not destroy the surviving config key.
        self.assertEqual(
            app.config["providers"]["Gemini (Recommended)"]["api_key"],
            "OLD-CONFIG-KEY",
        )
        m.assert_not_called()

    def test_empty_field_and_no_config_key_leaves_nothing(self):
        provider, app = self._provider_with_ui_value("", existing_config={})
        with mock.patch.object(aiprovider, "_store_api_key", return_value=True) as m:
            provider.save_config()
        self.assertNotIn("api_key", app.config["providers"]["Gemini (Recommended)"])
        m.assert_not_called()


class TestClearStoredApiKey(unittest.TestCase):
    """Revocation: the only supported path for removing a stored key."""

    def _provider_with_config(self, config):
        app = FakeApp(config)
        provider = GeminiProvider(app)
        provider.api_key = "STORE-KEY"
        return provider, app

    def _provider_with_rendered_ui(self, ui_value, existing_config=None):
        """Full render path: widgets are real QLineEdits, Save goes through
        ``save_config()`` and reads them back with ``get_value()``."""
        config = {
            "providers": {
                "Gemini (Recommended)": existing_config
                if existing_config is not None
                else {}
            }
        }
        app = FakeApp(config)
        provider = GeminiProvider(app)
        for setting in provider.settings:
            if setting.name == "api_key":
                setting.set_value(ui_value)
            elif setting.name == "model_name":
                setting.set_value("gemini-flash-latest")
            setting.render_to_layout(QtWidgets.QVBoxLayout())
        return provider, app

    def test_clear_removes_keyring_entry_and_config_fallback(self):
        config = {
            "providers": {
                "Gemini (Recommended)": {
                    "api_key": "CONFIG-KEY",
                    "model_name": "gemini-flash-latest",
                }
            }
        }
        provider, app = self._provider_with_config(config)
        with mock.patch.object(aiprovider, "_delete_api_key", return_value=True) as m:
            provider.clear_stored_api_key()
        m.assert_called_once_with(GEMINI_KEYRING_ID)
        self.assertEqual(provider.api_key, "")
        self.assertNotIn("api_key", app.config["providers"]["Gemini (Recommended)"])
        # model_name must survive the clear
        self.assertEqual(
            app.config["providers"]["Gemini (Recommended)"]["model_name"],
            "gemini-flash-latest",
        )

    def test_clear_when_keyring_unavailable_still_removes_config_key(self):
        config = {
            "providers": {
                "Gemini (Recommended)": {
                    "api_key": "CONFIG-KEY",
                    "model_name": "gemini-flash-latest",
                }
            }
        }
        provider, app = self._provider_with_config(config)
        with mock.patch.object(aiprovider, "_delete_api_key", return_value=False):
            provider.clear_stored_api_key()
        self.assertNotIn("api_key", app.config["providers"]["Gemini (Recommended)"])
        self.assertEqual(provider.api_key, "")

    def test_clear_without_config_key_is_noop(self):
        config = {
            "providers": {"Gemini (Recommended)": {"model_name": "gemini-flash-latest"}}
        }
        provider, app = self._provider_with_config(config)
        with mock.patch.object(aiprovider, "_delete_api_key", return_value=True) as m:
            provider.clear_stored_api_key()
        m.assert_called_once_with(GEMINI_KEYRING_ID)
        self.assertEqual(provider.api_key, "")
        self.assertEqual(
            app.config["providers"]["Gemini (Recommended)"]["model_name"],
            "gemini-flash-latest",
        )

    def test_clear_then_save_preserves_nothing(self):
        """Clear → Save regression: after revocation, Save must not restore the key."""
        config = {
            "providers": {
                "Gemini (Recommended)": {
                    "api_key": "CONFIG-KEY",
                    "model_name": "gemini-flash-latest",
                }
            }
        }
        # Render the full UI so Save reads the widget text, not the in-memory value
        provider, app = self._provider_with_rendered_ui("CONFIG-KEY", config)
        # Clear the key from both store and rendered widget
        provider.clear_stored_api_key()
        # Verify the store is empty
        self.assertNotIn("api_key", app.config["providers"]["Gemini (Recommended)"])
        # Now Save – this is the regression test
        provider.save_config()
        # The store must remain empty; no key should be written back
        self.assertNotIn("api_key", app.config["providers"]["Gemini (Recommended)"])


class TestKeyringIdSingleSourceOfTruth(unittest.TestCase):
    def test_provider_ids_match_map(self):
        app = FakeApp({"providers": {}})
        self.assertEqual(
            GeminiProvider(app)._keyring_id,
            PROVIDER_KEYRING_IDS["Gemini (Recommended)"],
        )
        self.assertEqual(GEMINI_KEYRING_ID, PROVIDER_KEYRING_IDS["Gemini (Recommended)"])
        self.assertEqual(
            OPENAI_COMPATIBLE_KEYRING_ID,
            PROVIDER_KEYRING_IDS["OpenAI Compatible (For Experts)"],
        )


if __name__ == "__main__":
    unittest.main()
