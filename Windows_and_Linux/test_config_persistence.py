"""Tests for safe, atomic configuration writes."""

import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _test_support  # noqa: F401
import WritingToolApp as app_module


class TestConfigPersistence(unittest.TestCase):
    def _save_config(self, app, config):
        app_module.WritingToolApp.save_config(app, config)

    def _temporary_files(self, directory):
        return list(Path(directory).glob(".config.json.*.tmp"))

    def test_precommit_failures_preserve_existing_config_and_clean_temp(self):
        failures = ["serialization", "replace", "file fsync"]
        if sys.platform != "win32":
            failures.append("directory open")

        for name in failures:
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "config.json"
                original = '{"keep": "existing config"}\n'
                config_path.write_text(original, encoding="utf-8")
                previous_config = {"in_memory": "previous"}
                app = SimpleNamespace(config_path=str(config_path), config=previous_config)
                closed_fds = []

                with ExitStack() as stack:
                    if name == "replace":
                        real_close = app_module.os.close

                        def record_close(fd):
                            closed_fds.append(fd)
                            return real_close(fd)

                        stack.enter_context(mock.patch.object(
                            app_module.os, "replace", side_effect=OSError("disk error")
                        ))
                        stack.enter_context(mock.patch.object(app_module.os, "close", side_effect=record_close))
                    elif name == "file fsync":
                        stack.enter_context(mock.patch.object(
                            app_module.os, "fsync", side_effect=OSError("sync failed")
                        ))
                    elif name == "directory open":
                        real_open = app_module.os.open

                        def fail_directory_open(path, flags, *args):
                            if os.path.abspath(path) == directory:
                                raise OSError("open failed")
                            return real_open(path, flags, *args)

                        stack.enter_context(mock.patch.object(
                            app_module.os, "open", side_effect=fail_directory_open
                        ))

                    config = {"not_serializable": object()} if name == "serialization" else {"updated": True}
                    error = TypeError if name == "serialization" else OSError
                    with self.assertRaises(error):
                        self._save_config(app, config)

                self.assertEqual(config_path.read_text(encoding="utf-8"), original)
                self.assertIs(app.config, previous_config)
                self.assertEqual(self._temporary_files(directory), [])
                if name == "replace":
                    self.assertEqual(len(closed_fds), 0 if sys.platform == "win32" else 1)
                    for fd in closed_fds:
                        with self.assertRaises(OSError):
                            os.fstat(fd)

    def test_success_replaces_config_and_updates_app_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text('{"old": true}\n', encoding="utf-8")
            config = {"providers": {"example": {"enabled": True}}, "locale": "en"}
            app = SimpleNamespace(config_path=str(config_path), config=None)

            self._save_config(app, config)

            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), config)
            self.assertIs(app.config, config)
            self.assertEqual(self._temporary_files(directory), [])

    @unittest.skipIf(os.name == "nt", "Unix file permission bits are not portable to Windows")
    def test_saved_config_has_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            app = SimpleNamespace(config_path=str(config_path), config=None)

            self._save_config(app, {"private": "value"})

            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(sys.platform == "win32", "Windows does not support directory fsync")
    def test_fsync_happens_before_replacement_and_directory_afterward(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            app = SimpleNamespace(config_path=str(config_path), config=None)
            events = []
            real_fsync = app_module.os.fsync
            real_replace = app_module.os.replace
            real_open = app_module.os.open
            real_close = app_module.os.close
            opened_fds = []
            closed_fds = []

            def record_fsync(fd):
                kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                events.append(f"fsync-{kind}")
                return real_fsync(fd)

            def record_replace(source, destination):
                events.append("replace")
                return real_replace(source, destination)

            def record_open(path, flags, *args):
                fd = real_open(path, flags, *args)
                if os.path.abspath(path) == directory:
                    opened_fds.append(fd)
                    events.append("open-directory")
                return fd

            def record_close(fd):
                closed_fds.append(fd)
                return real_close(fd)

            with mock.patch.object(app_module.os, "fsync", side_effect=record_fsync), mock.patch.object(
                app_module.os, "replace", side_effect=record_replace
            ), mock.patch.object(app_module.os, "open", side_effect=record_open), mock.patch.object(
                app_module.os, "close", side_effect=record_close
            ):
                self._save_config(app, {"saved": True})

            self.assertEqual(
                events,
                ["fsync-file", "open-directory", "replace", "fsync-directory"],
            )
            self.assertEqual(closed_fds, opened_fds)

    @unittest.skipIf(sys.platform == "win32", "Windows does not sync config directories")
    def test_directory_fsync_failure_keeps_committed_config_and_logs_sanitized_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config = {"updated": True}
            app = SimpleNamespace(config_path=str(config_path), config=None)
            real_fsync = app_module.os.fsync
            real_close = app_module.os.close
            closed_fds = []

            def fail_directory_fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("sensitive filesystem detail")
                return real_fsync(fd)

            def record_close(fd):
                closed_fds.append(fd)
                return real_close(fd)

            with mock.patch.object(app_module.os, "fsync", side_effect=fail_directory_fsync), mock.patch.object(
                app_module.os, "close", side_effect=record_close
            ), self.assertLogs(level="WARNING") as logs:
                self._save_config(app, config)

            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), config)
            self.assertIs(app.config, config)
            self.assertEqual(len(closed_fds), 1)
            self.assertIn("directory durability is unconfirmed", logs.output[0])
            self.assertNotIn("sensitive filesystem detail", logs.output[0])

    @unittest.skipIf(sys.platform == "win32", "Windows does not open config directories")
    def test_directory_close_failure_after_replacement_does_not_undo_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config = {"updated": True}
            app = SimpleNamespace(config_path=str(config_path), config=None)
            real_close = app_module.os.close

            def fail_directory_close(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    real_close(fd)
                    raise OSError("sensitive close detail")
                return real_close(fd)

            with mock.patch.object(app_module.os, "close", side_effect=fail_directory_close), self.assertLogs(
                level="WARNING"
            ) as logs:
                self._save_config(app, config)

            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), config)
            self.assertIs(app.config, config)
            self.assertNotIn("sensitive close detail", logs.output[0])

    def test_windows_skips_directory_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            app = SimpleNamespace(config_path=str(config_path), config=None)
            real_fsync = app_module.os.fsync
            real_open = app_module.os.open
            fsync_calls = []

            def record_fsync(fd):
                fsync_calls.append(fd)
                return real_fsync(fd)

            # Patch sys.platform rather than os.name so pathlib/tempfile keep
            # their host-platform behavior while the branch is exercised.
            def reject_directory_open(path, flags, *args):
                if os.path.abspath(path) == directory:
                    raise AssertionError("directory open")
                return real_open(path, flags, *args)

            with mock.patch.object(app_module.sys, "platform", "win32"), mock.patch.object(
                app_module.os, "open", side_effect=reject_directory_open
            ), mock.patch.object(app_module.os, "fsync", side_effect=record_fsync):
                self._save_config(app, {"saved": True})

            self.assertEqual(len(fsync_calls), 1)

if __name__ == "__main__":
    unittest.main()
