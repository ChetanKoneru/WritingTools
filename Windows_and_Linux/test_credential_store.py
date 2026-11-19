"""Credential-store and provider configuration regression tests."""

from _test_support import fake_keyring

import copy
import base64
import sys
import unittest
from unittest import mock

from PySide6 import QtWidgets

from aiprovider import (
    GeminiProvider,
    OpenAICompatibleProvider,
    _delete_api_key,
    _retrieve_api_key,
    _store_api_key,
    decode_legacy_api_key,
)
import WritingToolApp as app_module


_qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeApp:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.saved = []

    def save_config(self, config):
        self.saved.append(copy.deepcopy(config))


class KeyringTestCase(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.keyring = fake_keyring(self.store)
        self.keyring_patch = mock.patch.dict(sys.modules, {"keyring": self.keyring})
        self.keyring_patch.start()

    def tearDown(self):
        self.keyring_patch.stop()


class TestCredentialStoreHelpers(KeyringTestCase):
    def test_legacy_decoding_accepts_plaintext_and_rejects_invalid_encoding(self):
        self.assertEqual(decode_legacy_api_key("plain-key"), "plain-key")
        encoded = base64.b64encode(bytes(b ^ 0x5A for b in b"legacy-key")).decode()
        self.assertEqual(decode_legacy_api_key(f"enc:{encoded}"), "legacy-key")
        self.assertIsNone(decode_legacy_api_key("enc:not-base64!"))

    def test_store_then_readback_succeeds(self):
        self.keyring.set_password = mock.Mock(wraps=self.keyring.set_password)
        self.keyring.get_password = mock.Mock(wraps=self.keyring.get_password)
        self.assertTrue(_store_api_key("provider", "secret"))
        self.keyring.set_password.assert_called_once_with("WritingTools", "provider", "secret")
        self.keyring.get_password.assert_called_once_with("WritingTools", "provider")
        self.assertEqual(_retrieve_api_key("provider"), "secret")

    def test_readback_mismatch_is_failure_and_logs_no_secret(self):
        self.keyring.get_password = lambda service, account: "other"
        with self.assertLogs("root", "WARNING") as logs:
            self.assertFalse(_store_api_key("provider", "secret"))
        self.assertIn("VerificationMismatch", logs.output[0])
        self.assertNotIn("secret", "\n".join(logs.output))

    def test_backend_failure_is_sanitized(self):
        self.keyring.set_password = mock.Mock(side_effect=RuntimeError("secret"))
        with self.assertLogs("root", "WARNING") as logs:
            self.assertFalse(_store_api_key("provider", "secret"))
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("secret", logs.output[0])

    def test_delete_is_verified(self):
        self.store["provider"] = "secret"
        self.assertTrue(_delete_api_key("provider"))
        self.assertNotIn("provider", self.store)

    def test_missing_delete_is_a_verified_noop(self):
        self.assertTrue(_delete_api_key("missing"))

    def test_delete_failure_readback_failure_and_silent_noop_are_false(self):
        for method in ("delete_password", "get_password", "silent_noop"):
            with self.subTest(method=method):
                self.store["provider"] = "secret"
                if method == "silent_noop":
                    replacement = mock.Mock()
                    target = "delete_password"
                else:
                    replacement = mock.Mock(side_effect=RuntimeError("backend down"))
                    target = method
                with mock.patch.object(self.keyring, target, replacement):
                    self.assertFalse(_delete_api_key("provider"))


class TestProviderCredentialSaving(KeyringTestCase):
    def _provider(self, config):
        app = FakeApp(config)
        provider = GeminiProvider(app)
        layout = QtWidgets.QVBoxLayout()
        for setting in provider.settings:
            setting.render_to_layout(layout)
        return app, provider

    @staticmethod
    def _setting(provider, name):
        return next(setting for setting in provider.settings if setting.name == name)

    def test_successful_save_migrates_legacy_key_without_config_plaintext(self):
        name = "Gemini (Recommended)"
        app, provider = self._provider({"providers": {name: {"api_key": "old", "model_name": "old-model"}}})
        self._setting(provider, "api_key").input.setText("new-secret")
        model = self._setting(provider, "model_name")
        model.dropdown.setCurrentIndex(model.dropdown.findData(model._CUSTOM_SENTINEL))
        model.custom_input.setText("edited-model")
        config = copy.deepcopy(app.config)

        provider.save_config(config)

        self.assertEqual(provider.api_key_save_outcome, "saved")
        self.assertEqual(self.store[provider._keyring_id], "new-secret")
        self.assertNotIn("api_key", config["providers"][name])
        self.assertEqual(config["providers"][name]["model_name"], "edited-model")

    def test_vault_failure_preserves_legacy_key_and_nonsecret_settings(self):
        name = "Gemini (Recommended)"
        app, provider = self._provider({"providers": {name: {"api_key": "old-secret", "model_name": "old-model"}}})
        self._setting(provider, "api_key").input.setText("new-secret")
        self.keyring.set_password = mock.Mock(side_effect=RuntimeError("backend unavailable"))
        model = self._setting(provider, "model_name")
        model.dropdown.setCurrentIndex(model.dropdown.findData(model._CUSTOM_SENTINEL))
        model.custom_input.setText("edited-model")
        config = copy.deepcopy(app.config)

        provider.save_config(config)

        self.assertEqual(provider.api_key_save_outcome, "failed")
        self.assertEqual(config["providers"][name]["api_key"], "old-secret")
        self.assertEqual(config["providers"][name]["model_name"], "edited-model")
        self.assertNotIn("new-secret", repr(config))

    def test_blank_api_key_preserves_legacy_key_without_vault_operations(self):
        name = "Gemini (Recommended)"
        app, provider = self._provider({"providers": {name: {"api_key": "legacy-secret", "model_name": "old-model"}}})
        self.keyring.set_password = mock.Mock(wraps=self.keyring.set_password)
        self.keyring.delete_password = mock.Mock(wraps=self.keyring.delete_password)
        config = copy.deepcopy(app.config)

        provider.save_config(config)

        self.keyring.set_password.assert_not_called()
        self.keyring.delete_password.assert_not_called()
        self.assertEqual(config["providers"][name]["api_key"], "legacy-secret")

    def test_vault_key_takes_precedence_over_legacy_fallback_on_load(self):
        name = "Gemini (Recommended)"
        app, provider = self._provider({"api_key": "legacy-secret"})
        self.store[provider._keyring_id] = "vault-secret"

        provider.load_config(app.config)

        self.assertEqual(provider.api_key, "vault-secret")

    def test_legacy_key_is_used_when_vault_is_empty(self):
        app, provider = self._provider({"api_key": "legacy-secret"})

        provider.load_config(app.config)

        self.assertEqual(provider.api_key, "legacy-secret")

    def test_gemini_and_openai_api_fields_are_masked(self):
        app = FakeApp({})
        for provider in (GeminiProvider(app), OpenAICompatibleProvider(app)):
            api_setting = self._setting(provider, "api_key")
            api_setting.render_to_layout(QtWidgets.QVBoxLayout())
            self.assertEqual(api_setting.input.echoMode(), QtWidgets.QLineEdit.EchoMode.Password)

    def test_clear_removes_legacy_key_and_blanks_widget(self):
        name = "Gemini (Recommended)"
        app, provider = self._provider({"providers": {name: {"api_key": "old-secret"}}})
        self.store[provider._keyring_id] = "stored-secret"
        api_setting = self._setting(provider, "api_key")
        api_setting.render_to_layout(QtWidgets.QVBoxLayout())
        api_setting.input.setText("stored-secret")
        provider.api_key = "stored-secret"

        self.assertTrue(provider.clear_stored_api_key())

        self.assertNotIn(provider._keyring_id, self.store)
        self.assertNotIn("api_key", app.config["providers"][name])
        self.assertEqual(api_setting.input.text(), "")
        self.assertEqual(provider.api_key, "")

    def test_clear_config_failure_does_not_restore_vault_or_stale_input(self):
        name = "Gemini (Recommended)"
        app, provider = self._provider({"providers": {name: {"api_key": "old-secret"}}})
        self.store[provider._keyring_id] = "stored-secret"
        api_setting = self._setting(provider, "api_key")
        api_setting.render_to_layout(QtWidgets.QVBoxLayout())
        api_setting.input.setText("stored-secret")
        app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))

        self.assertFalse(provider.clear_stored_api_key())

        self.assertNotIn(provider._keyring_id, self.store)
        self.assertEqual(api_setting.input.text(), "")
        self.assertNotIn("api_key", app.config["providers"][name])


class TestConfigCredentialMigration(unittest.TestCase):
    @staticmethod
    def _legacy_encoded(value):
        return "enc:" + base64.b64encode(bytes(b ^ 0x5A for b in value.encode())).decode()

    def _app(self, config):
        return type("MigrationApp", (), {"config": config, "save_config": mock.Mock()})()

    def test_plaintext_and_encoded_keys_for_both_known_providers_migrate(self):
        config = {
            "is_config_file_updated_for_v8": True,
            "is_config_file_updated_for_v9": True,
            "providers": {
                "Gemini (Recommended)": {"api_key": "gemini-secret"},
                "OpenAI Compatible (For Experts)": {"api_key": self._legacy_encoded("openai-secret")},
            },
        }
        app = self._app(config)
        with mock.patch.object(app_module, "_store", return_value=True) as store:
            app_module.WritingToolApp._migrate_config(app)

        self.assertEqual(store.call_args_list, [mock.call("gemini", "gemini-secret"), mock.call("openai_compatible", "openai-secret")])
        self.assertNotIn("api_key", config["providers"]["Gemini (Recommended)"])
        self.assertNotIn("api_key", config["providers"]["OpenAI Compatible (For Experts)"])
        self.assertTrue(config["is_config_file_updated_for_v10"])

    def test_failed_migration_keeps_key_and_does_not_stamp_v10(self):
        config = {"providers": {"Gemini (Recommended)": {"api_key": "secret", "model_name": "gemini-flash-latest"}}}
        app = self._app(config)
        with (
            mock.patch.object(app_module, "_store", return_value=False) as store,
            mock.patch.object(app_module.QMessageBox, "warning"),
        ):
            app_module.WritingToolApp._migrate_config(app)

        store.assert_called_once_with("gemini", "secret")
        self.assertEqual(config["providers"]["Gemini (Recommended)"]["api_key"], "secret")
        self.assertNotIn("is_config_file_updated_for_v10", config)

    def test_pre_v8_failure_attempts_each_provider_once(self):
        config = {
            "providers": {
                "Gemini (Recommended)": {"api_key": "gemini-secret", "model_name": "gemini-flash-latest"},
                "OpenAI Compatible (For Experts)": {"api_key": "openai-secret"},
            }
        }
        app = self._app(config)
        with (
            mock.patch.object(app_module, "_store", return_value=False) as store,
            mock.patch.object(app_module.QMessageBox, "warning"),
        ):
            app_module.WritingToolApp._migrate_config(app)

        self.assertEqual(store.call_count, 2)
        self.assertCountEqual(store.call_args_list, [mock.call("gemini", "gemini-secret"), mock.call("openai_compatible", "openai-secret")])


if __name__ == "__main__":
    unittest.main()
