"""Settings credential-failure regressions.

Run with: QT_QPA_PLATFORM=offscreen uv run python -m unittest test_settings_credentials -v
"""

import _test_support  # noqa: F401 - configure headless Qt before importing PySide6

import types
import sys
import unittest
from unittest import mock

from PySide6 import QtWidgets

from aiprovider import (
    CredentialStoreError,
    CredentialStoreUncertainError,
    GeminiProvider,
    TextSetting,
)
from ui.SettingsWindow import SettingsWindow


_qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Provider:
    provider_name = "New"
    client = object()

    def save_config(self, config, *, defer_api_key_failure=False):
        raise CredentialStoreError()


class _UncertainProvider(_Provider):
    def save_config(self, config, *, defer_api_key_failure=False):
        raise CredentialStoreUncertainError()


class _SavedKeyProvider(_Provider):
    def save_config(self, config, *, defer_api_key_failure=False):
        config.setdefault("providers", {})[self.provider_name] = {"model_name": "x"}


class _App:
    def __init__(self):
        self.config = {"provider": "Old", "providers": {}}
        self.providers = [_Provider()]
        self.current_provider = object()
        self.saved = mock.Mock()
        self.create_tray_icon = mock.Mock()
        self.register_hotkey = mock.Mock()

    def save_config(self, config):
        self.saved(config)


class TestSettingsCredentialFailure(unittest.TestCase):
    def _window(self, app, providers_only):
        window = types.SimpleNamespace(
            app=app,
            providers_only=providers_only,
            provider_dropdown=types.SimpleNamespace(
                currentIndex=lambda: 0, currentText=lambda: "New"
            ),
            close=mock.Mock(),
        )
        if not providers_only:
            window.shortcut_input = types.SimpleNamespace(text=lambda: "ctrl+space")
            window.gradient_radio = types.SimpleNamespace(isChecked=lambda: True)
        return window

    def test_failed_key_save_defers_key_but_saves_non_secret_settings(self):
        for providers_only in (True, False):
            with self.subTest(providers_only=providers_only):
                app = _App()
                original_provider = app.current_provider
                window = self._window(app, providers_only)

                with mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
                    SettingsWindow.save_settings(window)

                # Deferred key failure: non-secret settings are still persisted,
                # the key is not written, and the window is kept open.
                expected = {
                    "provider": "New",
                    "is_config_file_updated_for_v8": True,
                    "locale": "en",
                    "streaming": False,
                    "providers": {},
                }
                if not providers_only:
                    expected["shortcut"] = "ctrl+space"
                    expected["theme"] = "gradient"
                self.assertEqual(app.config, expected)
                self.assertIs(app.current_provider, original_provider)
                app.saved.assert_called_once_with(expected)
                app.create_tray_icon.assert_not_called()
                app.register_hotkey.assert_not_called()
                window.close.assert_not_called()
                warning.assert_called_once()

    def test_uncertain_key_save_warns_that_key_may_have_changed(self):
        app = _App()
        app.providers = [_UncertainProvider()]
        window = self._window(app, providers_only=True)

        with mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
            SettingsWindow.save_settings(window)

        self.assertEqual(app.config, {"provider": "Old", "providers": {}})
        app.saved.assert_not_called()
        self.assertIn("may have changed", warning.call_args.args[2])

    def test_config_file_failure_keeps_dialog_open_after_key_save(self):
        app = _App()
        app.providers = [_SavedKeyProvider()]
        app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))
        original_provider = app.current_provider
        window = self._window(app, providers_only=True)

        with mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
            SettingsWindow.save_settings(window)

        self.assertEqual(app.config, {"provider": "Old", "providers": {}})
        self.assertIs(app.current_provider, original_provider)
        app.create_tray_icon.assert_not_called()
        app.register_hotkey.assert_not_called()
        window.close.assert_not_called()
        self.assertIn("credential state was preserved or restored", warning.call_args.args[2])

    def test_successful_gemini_save_updates_vault_and_runtime(self):
        keyring = types.ModuleType("keyring")
        keyring.store = {}
        keyring.set_password = lambda service, account, value: keyring.store.__setitem__(
            account, value
        )
        keyring.get_password = lambda service, account: keyring.store.get(account)

        def delete_password(service, account):
            if account not in keyring.store:
                raise type("PasswordDeleteError", (Exception,), {})("missing")
            del keyring.store[account]

        keyring.delete_password = delete_password

        with mock.patch.dict(sys.modules, {"keyring": keyring}):
            app = _App()
            app.config = {"provider": "Gemini (Recommended)", "providers": {}}
            provider = GeminiProvider(app)
            app.providers = [provider]
            window = SettingsWindow(app, providers_only=True)
            api_setting = next(s for s in provider.settings if s.name == "api_key")
            api_setting.input.setText("NEW-KEY")

            with mock.patch.object(SettingsWindow, "close") as close:
                SettingsWindow.save_settings(window)

            self.assertEqual(keyring.store[provider._keyring_id], "NEW-KEY")
            self.assertEqual(provider.api_key, "NEW-KEY")
            self.assertNotIn(
                "api_key", app.config["providers"]["Gemini (Recommended)"]
            )
            self.assertEqual(app.saved.call_count, 1)
            app.create_tray_icon.assert_called_once()
            app.register_hotkey.assert_called_once()
            close.assert_called_once()

        window.close()
        window.deleteLater()

    def test_failed_clear_keeps_rendered_key_and_client(self):
        app = _App()
        setting = TextSetting("api_key")
        provider = types.SimpleNamespace(
            provider_name="New",
            description="",
            logo=None,
            button_text=None,
            button_action=None,
            settings=[setting],
            _keyring_id="new",
            client=object(),
            get_api_key=lambda: "OLD-KEY",
            clear_stored_api_key=lambda: False,
        )
        app.config = {"provider": "New", "providers": {"New": {}}}
        app.providers = [provider]
        window = SettingsWindow(app, providers_only=True)
        original_client = provider.client
        setting.input.setText("OLD-KEY")

        clear_button = next(
            button
            for button in window.findChildren(QtWidgets.QPushButton)
            if button.text() == "Clear stored API key"
        )
        with (
            mock.patch.object(
                QtWidgets.QMessageBox,
                "question",
                return_value=QtWidgets.QMessageBox.StandardButton.Yes,
            ),
            mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
        ):
            clear_button.click()

        self.assertEqual(setting.input.text(), "OLD-KEY")
        self.assertIs(provider.client, original_client)
        self.assertEqual(app.config, {"provider": "New", "providers": {"New": {}}})
        warning.assert_called_once()
        window.close()


class TestRealSettingsCredentialTransactions(unittest.TestCase):
    """Regression coverage through real Gemini widgets and the clicked Finish button."""

    @staticmethod
    def _keyring(store):
        keyring = types.ModuleType("keyring")
        keyring.get_password = lambda service, account: store.get(account)
        keyring.set_password = lambda service, account, value: store.__setitem__(account, value)

        def delete_password(service, account):
            if account not in store:
                raise type("PasswordDeleteError", (Exception,), {})("missing")
            del store[account]

        keyring.delete_password = delete_password
        return keyring

    def _real_window(self, app):
        provider = GeminiProvider(app)
        app.providers = [provider]
        window = SettingsWindow(app, providers_only=True)
        api_setting = next(s for s in provider.settings if s.name == "api_key")
        finish = next(
            button for button in window.findChildren(QtWidgets.QPushButton)
            if button.text() == "Finish AI Setup"
        )
        return window, provider, api_setting, finish

    def _real_window_mode(self, app, providers_only):
        provider = GeminiProvider(app)
        app.providers = [provider]
        window = SettingsWindow(app, providers_only=providers_only)
        api_setting = next(s for s in provider.settings if s.name == "api_key")
        button_text = "Finish AI Setup" if providers_only else "Save"
        button = next(
            item for item in window.findChildren(QtWidgets.QPushButton)
            if item.text() == button_text
        )
        return window, provider, api_setting, button

    def test_real_clicked_save_and_finish_keep_runtime_on_ordinary_vault_failure(self):
        for providers_only in (False, True):
            with self.subTest(providers_only=providers_only):
                store = {"gemini": "OLD-KEY"}
                keyring = self._keyring(store)
                keyring.set_password = mock.Mock(side_effect=RuntimeError("backend down"))
                with mock.patch.dict(sys.modules, {"keyring": keyring}):
                    app = _App()
                    app.config = {"provider": "Gemini (Recommended)", "providers": {}}
                    window, provider, setting, button = self._real_window_mode(app, providers_only)
                    provider.client = object()
                    original_client, original_provider = provider.client, app.current_provider
                    setting.input.setText("NEW-KEY")
                    window.show()
                    with (
                        mock.patch.object(SettingsWindow, "close") as close,
                        mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
                    ):
                        button.click()

                    self.assertTrue(window.isVisible())
                    self.assertEqual(store["gemini"], "OLD-KEY")
                    expected_config = {
                        "provider": "Gemini (Recommended)",
                        "locale": "en",
                        "streaming": False,
                        "is_config_file_updated_for_v8": True,
                        "providers": {
                            "Gemini (Recommended)": {
                                "model_name": "gemini-flash-latest"
                            }
                        },
                    }
                    if not providers_only:
                        expected_config["shortcut"] = "ctrl+space"
                        expected_config["theme"] = "gradient"
                    self.assertEqual(app.config, expected_config)
                    self.assertIs(app.current_provider, original_provider)
                    self.assertIs(provider.client, original_client)
                    self.assertIsNone(provider._staged_credential_snapshot)
                    app.create_tray_icon.assert_not_called()
                    app.register_hotkey.assert_not_called()
                    close.assert_not_called()
                    self.assertIn("credential store is unavailable", warning.call_args.args[2])
                    window.close()
                    window.deleteLater()

    # NEW TEST: Case A - read fails (locked wallet), defer is enabled.
    # The provider entry must still be written with the existing config key.
    def test_real_clicked_finish_defers_key_when_vault_read_fails(self):
        for providers_only in (True, False):
            with self.subTest(providers_only=providers_only):
                store = {"gemini": "OLD-KEY"}
                keyring = self._keyring(store)
                keyring.get_password = mock.Mock(side_effect=RuntimeError("backend down"))
                with mock.patch.dict(sys.modules, {"keyring": keyring}):
                    app = _App()
                    app.config = {"provider": "Gemini (Recommended)", "providers": {}}
                    window, provider, setting, button = self._real_window_mode(app, providers_only)
                    provider.client = object()
                    original_client, original_provider = provider.client, app.current_provider
                    setting.input.setText("NEW-KEY")
                    window.show()
                    with (
                        mock.patch.object(SettingsWindow, "close") as close,
                        mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
                    ):
                        button.click()

                    self.assertTrue(window.isVisible())
                    # Vault was never written to
                    self.assertEqual(store["gemini"], "OLD-KEY")
                    expected_config = {
                        "provider": "Gemini (Recommended)",
                        "locale": "en",
                        "streaming": False,
                        "is_config_file_updated_for_v8": True,
                        "providers": {
                            "Gemini (Recommended)": {
                                "model_name": "gemini-flash-latest"
                            }
                        },
                    }
                    if not providers_only:
                        expected_config["shortcut"] = "ctrl+space"
                        expected_config["theme"] = "gradient"
                    self.assertEqual(app.config, expected_config)
                    self.assertIs(app.current_provider, original_provider)
                    self.assertIs(provider.client, original_client)
                    self.assertIsNone(provider._staged_credential_snapshot)
                    app.create_tray_icon.assert_not_called()
                    app.register_hotkey.assert_not_called()
                    close.assert_not_called()
                    self.assertIn("credential store is unavailable", warning.call_args.args[2])
                    window.close()
                    window.deleteLater()

    # NEW TEST: Case C - write silently no-ops.
    def test_real_clicked_finish_defers_key_when_write_silently_noops(self):
        for providers_only in (True, False):
            with self.subTest(providers_only=providers_only):
                store = {"gemini": "OLD-KEY"}
                keyring = self._keyring(store)
                # set_password appears to succeed but get_password returns
                # the old value (silent no-op).
                def set_then_noops(service, account, value):
                    pass  # do nothing
                keyring.set_password = set_then_noops
                with mock.patch.dict(sys.modules, {"keyring": keyring}):
                    app = _App()
                    app.config = {"provider": "Gemini (Recommended)", "providers": {}}
                    window, provider, setting, button = self._real_window_mode(app, providers_only)
                    provider.client = object()
                    original_client, original_provider = provider.client, app.current_provider
                    setting.input.setText("NEW-KEY")
                    window.show()
                    with (
                        mock.patch.object(SettingsWindow, "close") as close,
                        mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
                    ):
                        button.click()

                    self.assertTrue(window.isVisible())
                    # Vault was never written to (silent no-op)
                    self.assertEqual(store["gemini"], "OLD-KEY")
                    expected_config = {
                        "provider": "Gemini (Recommended)",
                        "locale": "en",
                        "streaming": False,
                        "is_config_file_updated_for_v8": True,
                        "providers": {
                            "Gemini (Recommended)": {
                                "model_name": "gemini-flash-latest"
                            }
                        },
                    }
                    if not providers_only:
                        expected_config["shortcut"] = "ctrl+space"
                        expected_config["theme"] = "gradient"
                    self.assertEqual(app.config, expected_config)
                    self.assertIs(app.current_provider, original_provider)
                    self.assertIs(provider.client, original_client)
                    self.assertIsNone(provider._staged_credential_snapshot)
                    app.create_tray_icon.assert_not_called()
                    app.register_hotkey.assert_not_called()
                    close.assert_not_called()
                    self.assertIn("credential store is unavailable", warning.call_args.args[2])
                    window.close()
                    window.deleteLater()

    def test_real_clicked_finish_reports_uncertain_vault_write(self):
        store = {"gemini": "OLD-KEY"}
        keyring = self._keyring(store)
        set_calls = 0

        def persist_then_fail(service, account, value):
            nonlocal set_calls
            set_calls += 1
            if set_calls == 1:
                store[account] = value
            raise RuntimeError("backend down")

        keyring.set_password = persist_then_fail
        with mock.patch.dict(sys.modules, {"keyring": keyring}):
            app = _App()
            app.config = {"provider": "Gemini (Recommended)", "providers": {}}
            window, provider, setting, finish = self._real_window_mode(app, True)
            original_provider = app.current_provider
            setting.input.setText("NEW-KEY")
            window.show()
            with (
                mock.patch.object(SettingsWindow, "close") as close,
                mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
            ):
                finish.click()

            self.assertTrue(window.isVisible())
            self.assertEqual(store["gemini"], "NEW-KEY")
            self.assertEqual(app.config, {"provider": "Gemini (Recommended)", "providers": {}})
            self.assertIs(app.current_provider, original_provider)
            self.assertIn("may have changed", warning.call_args.args[2])
            close.assert_not_called()
            window.close()
            window.deleteLater()

    def test_real_finish_config_failure_restores_existing_key_and_runtime(self):
        store = {"gemini": "OLD-KEY"}
        with mock.patch.dict(sys.modules, {"keyring": self._keyring(store)}):
            app = _App()
            app.config = {"provider": "Gemini (Recommended)", "providers": {}}
            app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))
            window, provider, setting, finish = self._real_window(app)
            provider.client = object()
            original_client, original_provider = provider.client, app.current_provider
            setting.input.setText("NEW-KEY")
            with mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
                finish.click()

            self.assertEqual(store["gemini"], "OLD-KEY")
            self.assertEqual(app.config, {"provider": "Gemini (Recommended)", "providers": {}})
            self.assertIs(provider.client, original_client)
            self.assertIs(app.current_provider, original_provider)
            self.assertIn("credential state was preserved or restored", warning.call_args.args[2])
            self.assertIsNone(provider._staged_credential_snapshot)
            window.deleteLater()

    def test_real_finish_restores_confirmed_missing_key(self):
        store = {}
        with mock.patch.dict(sys.modules, {"keyring": self._keyring(store)}):
            app = _App()
            app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))
            window, provider, setting, finish = self._real_window(app)
            setting.input.setText("NEW-KEY")
            with mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
                finish.click()

            self.assertNotIn(provider._keyring_id, store)
            self.assertIn("credential state was preserved or restored", warning.call_args.args[2])
            window.deleteLater()

    def test_real_finish_rollback_failure_is_uncertain_without_secret_log(self):
        store = {"gemini": "OLD-KEY"}
        keyring = self._keyring(store)
        original_set, set_calls = keyring.set_password, 0

        def set_then_fail_restore(service, account, value):
            nonlocal set_calls
            set_calls += 1
            if set_calls == 1:
                original_set(service, account, value)
            else:
                raise RuntimeError("OLD-KEY NEW-KEY")

        keyring.set_password = set_then_fail_restore
        with mock.patch.dict(sys.modules, {"keyring": keyring}):
            app = _App()
            app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))
            window, provider, setting, finish = self._real_window(app)
            original_provider = app.current_provider
            setting.input.setText("NEW-KEY")
            with self.assertLogs(level="WARNING") as logs, mock.patch.object(
                QtWidgets.QMessageBox, "warning"
            ) as warning:
                finish.click()

            self.assertEqual(store["gemini"], "NEW-KEY")
            self.assertIs(app.current_provider, original_provider)
            self.assertIn("may have changed", warning.call_args.args[2])
            diagnostics = "\n".join(logs.output)
            self.assertIn("RuntimeError", diagnostics)
            self.assertNotIn("NEW-KEY", diagnostics)
            self.assertNotIn("OLD-KEY", diagnostics)
            window.deleteLater()

    def test_real_successive_finishes_replace_key_without_stale_rollback_token(self):
        store = {}
        with mock.patch.dict(sys.modules, {"keyring": self._keyring(store)}):
            app = _App()
            app.config = {"provider": "Gemini (Recommended)", "providers": {}}
            window, provider, setting, finish = self._real_window(app)
            setting.input.setText("FIRST-KEY")
            with mock.patch.object(SettingsWindow, "close"):
                finish.click()
                self.assertIsNone(provider._staged_credential_snapshot)
                setting.input.setText("SECOND-KEY")
                window.providers_only = True
                finish.click()

            self.assertEqual(store[provider._keyring_id], "SECOND-KEY")
            self.assertIsNone(provider._staged_credential_snapshot)
            window.deleteLater()

    def test_real_clear_config_failure_restores_vault_and_keeps_runtime(self):
        store = {"gemini": "OLD-KEY"}
        with mock.patch.dict(sys.modules, {"keyring": self._keyring(store)}):
            app = _App()
            app.config = {
                "provider": "Gemini (Recommended)",
                "providers": {"Gemini (Recommended)": {"api_key": "LEGACY-KEY"}},
            }
            app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))
            window, provider, setting, _ = self._real_window(app)
            provider.client = object()
            old_client = provider.client
            clear = next(
                button for button in window.findChildren(QtWidgets.QPushButton)
                if button.text() == "Clear stored API key"
            )
            with (
                mock.patch.object(
                    QtWidgets.QMessageBox, "question",
                    return_value=QtWidgets.QMessageBox.StandardButton.Yes,
                ),
                mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
            ):
                clear.click()

            self.assertEqual(store["gemini"], "OLD-KEY")
            self.assertEqual(setting.input.text(), "OLD-KEY")
            self.assertIs(provider.client, old_client)
            self.assertEqual(app.config["providers"][provider.provider_name]["api_key"], "LEGACY-KEY")
            self.assertIn("could not be fully removed", warning.call_args.args[2])
            window.deleteLater()

    def test_real_clear_restore_failure_warns_uncertain_and_keeps_ui(self):
        store = {"gemini": "OLD-KEY"}
        keyring = self._keyring(store)

        def fail_restore(service, account, value):
            raise RuntimeError("backend down")

        keyring.set_password = fail_restore
        with mock.patch.dict(sys.modules, {"keyring": keyring}):
            app = _App()
            app.config = {
                "provider": "Gemini (Recommended)",
                "providers": {"Gemini (Recommended)": {"api_key": "LEGACY-KEY"}},
            }
            app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))
            window, provider, setting, _ = self._real_window(app)
            clear = next(
                button for button in window.findChildren(QtWidgets.QPushButton)
                if button.text() == "Clear stored API key"
            )
            with (
                mock.patch.object(
                    QtWidgets.QMessageBox, "question",
                    return_value=QtWidgets.QMessageBox.StandardButton.Yes,
                ),
                mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
            ):
                clear.click()

            self.assertNotIn("gemini", store)
            self.assertEqual(setting.input.text(), "OLD-KEY")
            self.assertIn("removal status is uncertain", warning.call_args.args[2])
            self.assertEqual(app.config["providers"][provider.provider_name]["api_key"], "LEGACY-KEY")
            window.deleteLater()

    def test_real_clear_missing_snapshot_is_not_uncertain(self):
        store = {}
        with mock.patch.dict(sys.modules, {"keyring": self._keyring(store)}):
            app = _App()
            app.config = {
                "provider": "Gemini (Recommended)",
                "providers": {"Gemini (Recommended)": {"api_key": "LEGACY-KEY"}},
            }
            app.save_config = mock.Mock(side_effect=OSError("disk unavailable"))
            window, provider, setting, _ = self._real_window(app)
            clear = next(
                button for button in window.findChildren(QtWidgets.QPushButton)
                if button.text() == "Clear stored API key"
            )
            with (
                mock.patch.object(
                    QtWidgets.QMessageBox, "question",
                    return_value=QtWidgets.QMessageBox.StandardButton.Yes,
                ),
                mock.patch.object(QtWidgets.QMessageBox, "warning") as warning,
            ):
                clear.click()

            self.assertNotIn("gemini", store)
            self.assertEqual(app.config["providers"][provider.provider_name]["api_key"], "LEGACY-KEY")
            self.assertNotIn("uncertain", warning.call_args.args[2])
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
