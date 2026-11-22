"""Settings-window credential failure regressions."""

from _test_support import fake_keyring

import copy
import sys
import unittest
from unittest import mock

from PySide6 import QtWidgets

import aiprovider
from aiprovider import GeminiProvider, OllamaProvider, OpenAICompatibleProvider
from ui.SettingsWindow import SettingsWindow


_qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class App:
    def __init__(self, provider_class=GeminiProvider):
        self.config = {"provider": "Gemini (Recommended)", "providers": {}}
        self.providers = [provider_class(self)]
        self.config["provider"] = self.providers[0].provider_name
        self.current_provider = self.providers[0]
        self.create_tray_icon = mock.Mock()
        self.register_hotkey = mock.Mock()

    def save_config(self, config):
        self.written = copy.deepcopy(config)


class SettingsCredentialTest(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.keyring_patch = mock.patch.dict(sys.modules, {"keyring": fake_keyring(self.store)})
        self.keyring_patch.start()
        self.app = App()
        self.window = SettingsWindow(self.app, providers_only=True)
        self.provider = self.app.providers[0]
        self.api_setting = next(s for s in self.provider.settings if s.name == "api_key")

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.keyring_patch.stop()

    def _click_save(self):
        button = next(button for button in self.window.findChildren(QtWidgets.QPushButton) if button.text() in {"Finish AI Setup", "Save"})
        button.click()

    def test_vault_failures_keep_masked_key_and_save_onboarding_settings(self):
        for failure in ("write", "readback"):
            with self.subTest(failure=failure):
                app = App()
                window = SettingsWindow(app, providers_only=True)
                provider = app.providers[0]
                api_setting = next(s for s in provider.settings if s.name == "api_key")
                api_setting.input.setText("new-secret")
                keyring = sys.modules["keyring"]
                button = next(button for button in window.findChildren(QtWidgets.QPushButton) if button.text() == "Finish AI Setup")
                target = "set_password" if failure == "write" else "get_password"
                with (
                    mock.patch.object(keyring, target, side_effect=RuntimeError("backend down")),
                    mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
                    mock.patch.object(window, "close") as close,
                ):
                    button.click()

                self.assertEqual(provider.api_key_save_outcome, "failed")
                self.assertEqual(api_setting.input.text(), "new-secret")
                self.assertNotEqual(api_setting.input.echoMode(), QtWidgets.QLineEdit.EchoMode.Normal)
                self.assertEqual(app.config["provider"], "Gemini (Recommended)")
                self.assertIn("Gemini (Recommended)", app.config["providers"])
                self.assertNotIn("api_key", app.config["providers"]["Gemini (Recommended)"])
                self.assertIn("other settings were saved", warning.call_args.args[2])
                app.create_tray_icon.assert_not_called()
                self.assertEqual(app.written, app.config)
                close.assert_not_called()
                window.close()
                window.deleteLater()

    def test_config_failure_after_verified_key_keeps_old_config_and_warns_key_saved(self):
        self.api_setting.input.setText("new-secret")
        original = copy.deepcopy(self.app.config)
        self.app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))

        with (
            mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
            mock.patch.object(self.window, "close") as close,
        ):
            self._click_save()

        self.assertEqual(self.store[self.provider._keyring_id], "new-secret")
        self.assertEqual(self.app.config, original)
        self.assertIn("API key was saved", warning.call_args.args[2])
        self.app.create_tray_icon.assert_not_called()
        close.assert_not_called()

    def test_config_failure_reloads_saved_runtime_without_overwriting_draft_widgets(self):
        for provider_class in (GeminiProvider, OpenAICompatibleProvider):
            with self.subTest(provider_class=provider_class.__name__):
                app = App()
                if provider_class is OpenAICompatibleProvider:
                    app.providers.append(provider_class(app))
                provider = next(
                    candidate
                    for candidate in app.providers
                    if isinstance(candidate, provider_class)
                )
                old_config = {
                    "provider": app.current_provider.provider_name,
                    "providers": {
                        provider.provider_name: {
                            "model_name" if provider_class is GeminiProvider else "api_model": "old-model",
                        }
                    },
                }
                if provider_class is OpenAICompatibleProvider:
                    old_config["providers"][provider.provider_name]["api_base"] = "https://old.example/v1"
                app.config = copy.deepcopy(old_config)
                provider.load_config(old_config["providers"][provider.provider_name])
                old_client = mock.Mock()
                provider.client = old_client
                window = SettingsWindow(app, providers_only=True)
                window.provider_dropdown.setCurrentIndex(app.providers.index(provider))
                api_setting = next(setting for setting in provider.settings if setting.name == "api_key")
                model_setting = next(
                    setting
                    for setting in provider.settings
                    if setting.name == ("model_name" if provider_class is GeminiProvider else "api_model")
                )
                api_setting.input.setText("new-secret")
                if provider_class is GeminiProvider:
                    model_setting.dropdown.setCurrentIndex(
                        model_setting.dropdown.findData(model_setting._CUSTOM_SENTINEL)
                    )
                    model_setting.custom_input.setText("draft-model")
                else:
                    model_setting.input.setText("draft-model")
                    next(setting for setting in provider.settings if setting.name == "api_base").input.setText(
                        "https://draft.example/v1"
                    )
                app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))

                with mock.patch.object(QtWidgets.QMessageBox, "warning"):
                    button = next(
                        button
                        for button in window.findChildren(QtWidgets.QPushButton)
                        if button.text() == "Finish AI Setup"
                    )
                    button.click()

                self.assertEqual(self.store[provider._keyring_id], "new-secret")
                self.assertEqual(app.config, old_config)
                self.assertIs(app.current_provider, app.providers[0])
                app.create_tray_icon.assert_not_called()
                app.register_hotkey.assert_not_called()
                old_client.close.assert_called_once()
                self.assertIsNone(provider.client)
                self.assertEqual(provider.api_key, "new-secret")
                self.assertEqual(
                    getattr(provider, "model_name" if provider_class is GeminiProvider else "api_model"),
                    "old-model",
                )
                self.assertEqual(model_setting.get_value(), "draft-model")
                if provider_class is OpenAICompatibleProvider:
                    self.assertEqual(provider.api_base, "https://old.example/v1")
                    self.assertEqual(
                        next(setting for setting in provider.settings if setting.name == "api_base").input.text(),
                        "https://draft.example/v1",
                    )
                window.close()
                window.deleteLater()

    def test_clear_then_save_does_not_store_or_restore_key_after_config_failure(self):
        name = self.provider.provider_name
        self.store[self.provider._keyring_id] = "stored-secret"
        self.app.config["providers"] = {name: {"api_key": "legacy-secret"}}
        self.app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))

        self.assertFalse(self.provider.clear_stored_api_key())
        self.assertEqual(self.api_setting.input.text(), "")
        self.assertNotIn(self.provider._keyring_id, self.store)
        self.assertNotIn("api_key", self.app.config["providers"][name])
        self.app.save_config = mock.Mock()
        with mock.patch.object(aiprovider, "_store_api_key", wraps=aiprovider._store_api_key) as store_key:
            self._click_save()

        store_key.assert_not_called()
        self.assertNotIn("api_key", self.app.config["providers"][name])

    def test_successful_finish_click_updates_runtime_and_closes(self):
        self.api_setting.input.setText("new-secret")
        with mock.patch.object(self.window, "close") as close:
            self._click_save()

        self.assertEqual(self.store[self.provider._keyring_id], "new-secret")
        self.assertEqual(self.app.current_provider, self.provider)
        self.assertEqual(self.provider.api_key, "new-secret")
        self.app.create_tray_icon.assert_called_once()
        self.app.register_hotkey.assert_called_once()
        close.assert_called_once()

    def test_normal_window_vault_fail_saves_shortcut_theme_and_custom_model(self):
        self.window.close()
        self.window.deleteLater()
        self.window = SettingsWindow(self.app, providers_only=False)
        self.provider = self.app.providers[0]
        self.api_setting = next(s for s in self.provider.settings if s.name == "api_key")
        model_setting = next(s for s in self.provider.settings if s.name == "model_name")
        self.api_setting.input.setText("new-secret")
        self.window.shortcut_input.setText("ctrl+alt+w")
        self.window.plain_radio.setChecked(True)
        model_setting.dropdown.setCurrentIndex(model_setting.dropdown.findData(model_setting._CUSTOM_SENTINEL))
        model_setting.custom_input.setText("custom-model")
        sys.modules["keyring"].set_password = mock.Mock(side_effect=RuntimeError("backend down"))

        with (
            mock.patch.object(QtWidgets.QMessageBox, "warning"),
            mock.patch.object(self.window, "close") as close,
        ):
            self._click_save()

        config = self.app.config["providers"][self.provider.provider_name]
        self.assertEqual(self.app.config["shortcut"], "ctrl+alt+w")
        self.assertEqual(self.app.config["theme"], "plain")
        self.assertEqual(config["model_name"], "custom-model")
        self.assertEqual(self.api_setting.input.text(), "new-secret")
        self.assertEqual(self.app.written, self.app.config)
        close.assert_not_called()

    def test_config_failure_without_key_does_not_claim_key_was_saved(self):
        self.window.close()
        self.window.deleteLater()
        self.app = App(OllamaProvider)
        self.window = SettingsWindow(self.app, providers_only=True)
        self.provider = self.app.providers[0]
        self.app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))

        with (
            mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
            mock.patch.object(self.window, "close") as close,
        ):
            self._click_save()

        self.assertNotIn("API key was saved", warning.call_args.args[2])
        self.app.create_tray_icon.assert_not_called()
        close.assert_not_called()

    def test_clear_confirmation_no_keeps_state_and_yes_warns_on_failure(self):
        self.api_setting.input.setText("visible-only-here")
        clear = next(button for button in self.window.findChildren(QtWidgets.QPushButton) if button.text() == "Clear stored API key")
        with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.No):
            clear.click()
        self.assertEqual(self.api_setting.input.text(), "visible-only-here")

        with (
            mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes),
            mock.patch.object(self.provider, "clear_stored_api_key", return_value=False) as clear_key,
            mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
        ):
            clear.click()
        clear_key.assert_called_once()
        warning.assert_called_once()
        self.assertIn("may remain in config.json", warning.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
