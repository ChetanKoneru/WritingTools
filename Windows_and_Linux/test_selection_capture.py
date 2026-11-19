"""Deterministic tests for selected-text clipboard capture."""

import _test_support  # noqa: F401  Must precede Qt/pynput application imports.

import threading
import unittest
from unittest import mock

import WritingToolApp as app_module
from PySide6 import QtCore, QtWidgets


class CaptureSignal:
    def emit(self, holder, primary_text):
        holder.ready.set()


class InlineThread:
    def __init__(self, *, target, args, **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class FakeCaptureApp:
    def __init__(self):
        self._capture_lock = threading.Lock()
        self.clipboard = "previous clipboard"
        self.injected = False
        self.capture_finished_signal = CaptureSignal()

    def _read_clipboard(self, timeout=1.5):
        return self.clipboard

    def _copy_shortcut(self, *args, **kwargs):
        self.injected = True
        self.clipboard = "previous clipboard"
        return True

    def _press_ctrl(self, *args, **kwargs):
        return self._copy_shortcut(*args, **kwargs)

    def _inject_ctrl(self, *args, **kwargs):
        return self._copy_shortcut(*args, **kwargs)


class TestSelectionCapture(unittest.TestCase):
    def run_capture(self, platform, *, injection=None, clipboard_write=None):
        app = FakeCaptureApp()
        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        def write_clipboard(text):
            if clipboard_write:
                clipboard_write(app, text)
            else:
                app.clipboard = text

        injector = (
            (lambda *args, **kwargs: injection(app, *args, **kwargs))
            if injection
            else app._copy_shortcut
        )

        with (
            mock.patch.object(app_module.pyperclip, "copy", side_effect=write_clipboard),
            mock.patch.object(app_module.sys, "platform", platform),
            mock.patch.object(app, "_press_ctrl", side_effect=injector),
            mock.patch.object(app, "_inject_ctrl", side_effect=injector),
            mock.patch.object(app_module.time, "monotonic", side_effect=monotonic),
            mock.patch.object(app_module.time, "sleep", side_effect=sleep),
            mock.patch.object(app_module.threading, "Thread", InlineThread),
        ):
            holder = app_module._SelectedTextHolder()
            app_module.WritingToolApp._fire_ctrl_c_and_capture_async(app, holder)
            self.assertTrue(holder.ready.wait(1))
        self.assertFalse(app._capture_lock.locked())
        return app, holder

    def test_identical_selection_is_accepted_on_windows_and_linux(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform):
                app, holder = self.run_capture(platform)
                self.assertEqual(holder.text, "previous clipboard")
                self.assertEqual(app.clipboard, "previous clipboard")
                self.assertTrue(app.injected)

    def test_fresh_different_copy_is_captured_and_clipboard_restored(self):
        def copy_fresh_text(app, *args, **kwargs):
            app.injected = True
            app.clipboard = "fresh selected text"
            return True

        app, holder = self.run_capture("win32", injection=copy_fresh_text)
        self.assertEqual(holder.text, "fresh selected text")
        self.assertEqual(app.clipboard, "previous clipboard")
        self.assertTrue(app.injected)

    def test_successful_injection_without_copy_rejects_marker_and_restores_backup(self):
        def no_selection(app, *args, **kwargs):
            app.injected = True
            return True

        app, holder = self.run_capture("win32", injection=no_selection)
        self.assertEqual(holder.text, "")
        self.assertTrue(holder.error)
        self.assertEqual(app.clipboard, "previous clipboard")
        self.assertTrue(app.injected)

    def test_sticky_primary_text_does_not_bypass_copy(self):
        app = FakeCaptureApp()
        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        def copy_without_data(*args, **kwargs):
            app.injected = True
            return True

        with (
            mock.patch.object(app_module.pyperclip, "copy", side_effect=lambda value: setattr(app, "clipboard", value)),
            mock.patch.object(app_module.sys, "platform", "win32"),
            mock.patch.object(app, "_press_ctrl", side_effect=copy_without_data),
            mock.patch.object(app_module.WritingToolApp, "_read_primary_selection", return_value="old selection"),
            mock.patch.object(app_module.time, "monotonic", side_effect=monotonic),
            mock.patch.object(app_module.time, "sleep", side_effect=sleep),
            mock.patch.object(app_module.threading, "Thread", InlineThread),
        ):
            holder = app_module._SelectedTextHolder()
            app_module.WritingToolApp._fire_ctrl_c_and_capture_async(app, holder)
            self.assertTrue(holder.ready.wait(1))
        self.assertTrue(app.injected)
        self.assertEqual(holder.text, "")

    def test_failed_copy_that_leaves_marker_does_not_submit_old_clipboard(self):
        calls = []

        def failed_copy(app, *args, **kwargs):
            calls.append(args)
            return False

        app, holder = self.run_capture("win32", injection=failed_copy)
        self.assertEqual(holder.text, "")
        self.assertTrue(holder.error)
        self.assertEqual(app.clipboard, "previous clipboard")
        self.assertFalse(app.injected)
        self.assertEqual(calls, [("c",)])

    def test_injection_exception_restores_clipboard_and_releases_lock(self):
        calls = []

        def fail(app, *args, **kwargs):
            calls.append(args)
            raise RuntimeError("injection failed")

        app, holder = self.run_capture("win32", injection=fail)
        self.assertEqual(holder.text, "")
        self.assertTrue(holder.error)
        self.assertEqual(app.clipboard, "previous clipboard")
        self.assertFalse(app._capture_lock.locked())
        self.assertEqual(calls, [("c",)])

    def test_unverified_marker_does_not_inject(self):
        def ignore_marker(app, value):
            if value != app.clipboard:
                return

        app, holder = self.run_capture("win32", clipboard_write=ignore_marker)
        self.assertEqual(holder.text, "")
        self.assertFalse(app.injected)
        self.assertEqual(app.clipboard, "previous clipboard")

    def test_busy_capture_does_not_touch_clipboard(self):
        app = FakeCaptureApp()
        app._capture_lock.acquire()
        holder = app_module._SelectedTextHolder()
        app_module.WritingToolApp._fire_ctrl_c_and_capture_async(app, holder)
        self.assertTrue(holder.ready.is_set())
        self.assertTrue(holder.error)
        self.assertEqual(app.clipboard, "previous clipboard")
        self.assertFalse(app.injected)
        app._capture_lock.release()

    def test_worker_start_failure_restores_clipboard_and_releases_lock(self):
        app = FakeCaptureApp()

        class BrokenThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread unavailable")

        with (
            mock.patch.object(app_module.pyperclip, "copy", side_effect=lambda value: setattr(app, "clipboard", value)),
            mock.patch.object(app_module.sys, "platform", "win32"),
            mock.patch.object(app, "_press_ctrl", side_effect=app._copy_shortcut),
            mock.patch.object(app_module.threading, "Thread", BrokenThread),
        ):
            holder = app_module._SelectedTextHolder()
            app_module.WritingToolApp._fire_ctrl_c_and_capture_async(app, holder)
        self.assertTrue(holder.ready.is_set())
        self.assertEqual(app.clipboard, "previous clipboard")
        self.assertFalse(app._capture_lock.locked())

    def test_unrelated_clipboard_change_is_preserved_on_capture_failure(self):
        app = FakeCaptureApp()

        calls = []

        def failed_injection_with_user_copy(*args, **kwargs):
            calls.append(args)
            app.clipboard = "new user clipboard"
            return False

        with (
            mock.patch.object(app_module.pyperclip, "copy", side_effect=lambda value: setattr(app, "clipboard", value)),
            mock.patch.object(app_module.sys, "platform", "win32"),
            mock.patch.object(app, "_press_ctrl", side_effect=failed_injection_with_user_copy),
            mock.patch.object(app_module.threading, "Thread", InlineThread),
        ):
            holder = app_module._SelectedTextHolder()
            app_module.WritingToolApp._fire_ctrl_c_and_capture_async(app, holder)
            self.assertTrue(holder.ready.wait(1))
        self.assertEqual(holder.text, "")
        self.assertEqual(app.clipboard, "new user clipboard")
        self.assertFalse(app._capture_lock.locked())
        self.assertEqual(calls, [("c",)])

    def test_user_clearing_clipboard_is_not_overwritten_after_copy(self):
        def copy_then_user_clears(app, *args, **kwargs):
            app.injected = True
            app.clipboard = ""
            return True

        app, holder = self.run_capture("win32", injection=copy_then_user_clears)
        self.assertEqual(holder.text, "")
        self.assertTrue(holder.error)
        self.assertEqual(app.clipboard, "")


class GuiHarness:
    def __init__(self, holder):
        self.current_text_holder = holder
        self._source_window_id = "unchanged"
        self.dispatched = []
        self.popup_calls = 0

    def activeWindow(self):
        return None

    def process_option(self, option):
        self.dispatched.append(option)

    def _open_popup(self):
        self.popup_calls += 1


class TestCaptureGuiFinalization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _finish_with_click(self, holder, *, accept):
        holder.error = "copy failed"
        holder.direct_option = "Rewrite"
        holder.source_window_id = "source-window"
        harness = GuiHarness(holder)
        observed = {}

        def click_modal():
            box = self.qt_app.activeModalWidget()
            observed["format"] = box.textFormat()
            observed["preview"] = box.informativeText()
            observed["details"] = box.detailedText()
            observed["default"] = box.defaultButton().text()
            observed["dispatched_before_answer"] = list(harness.dispatched)
            if accept:
                next(button for button in box.buttons() if button.text() == "Use this text").click()
            else:
                box.defaultButton().click()

        QtCore.QTimer.singleShot(0, click_modal)
        with mock.patch.object(QtWidgets.QMessageBox, "warning"):
            app_module.WritingToolApp._on_capture_finished(harness, holder, "<stale & primary>")
        return harness, observed

    def test_primary_accept_requires_modal_answer_and_clears_copy_error(self):
        holder = app_module._SelectedTextHolder()
        harness, observed = self._finish_with_click(holder, accept=True)
        self.assertEqual(holder.text, "<stale & primary>")
        self.assertEqual(holder.error, "")
        self.assertTrue(holder.ready.is_set())
        self.assertEqual(harness.dispatched, ["Rewrite"])
        self.assertEqual(observed["dispatched_before_answer"], [])
        self.assertEqual(observed["format"], QtCore.Qt.TextFormat.PlainText)
        self.assertEqual(observed["preview"], "<stale & primary>")
        self.assertEqual(observed["details"], "<stale & primary>")
        self.assertEqual(observed["default"], "Cancel")

    def test_primary_cancel_does_not_submit_or_dispatch_before_answer(self):
        holder = app_module._SelectedTextHolder()
        harness, _ = self._finish_with_click(holder, accept=False)
        self.assertEqual(holder.text, "")
        self.assertEqual(holder.error, "No text selected.")
        self.assertEqual(harness.dispatched, [])
        self.assertTrue(holder.ready.is_set())

    def test_stale_completion_during_primary_modal_changes_nothing(self):
        holder = app_module._SelectedTextHolder()
        holder.source_window_id = "old-source"
        holder.direct_option = "Old"
        harness = GuiHarness(holder)
        newer = app_module._SelectedTextHolder()

        def replace_request():
            harness.current_text_holder = newer
            self.qt_app.activeModalWidget().reject()

        QtCore.QTimer.singleShot(0, replace_request)
        app_module.WritingToolApp._on_capture_finished(harness, holder, "old primary")
        self.assertIs(harness.current_text_holder, newer)
        self.assertEqual(harness._source_window_id, "unchanged")
        self.assertEqual(harness.dispatched, [])
        self.assertFalse(holder.ready.is_set())

    def test_rapid_hotkey_keeps_first_capture_and_does_not_open_popup(self):
        first = app_module._SelectedTextHolder()

        class EntrypointHarness:
            def __init__(self):
                self._capture_lock = threading.Lock()
                self._capture_lock.acquire()
                self.current_text_holder = first
                self.popup_window = None

        harness = EntrypointHarness()
        app_module.WritingToolApp._show_popup(harness)
        self.assertIs(harness.current_text_holder, first)
        self.assertIsNone(harness.popup_window)
        harness._capture_lock.release()


class WorkerHarness(QtCore.QObject):
    capture_finished_signal = QtCore.Signal(object, str)

    def __init__(self):
        super().__init__()
        self._capture_lock = threading.Lock()
        self.clipboard = "backup"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.reads = 0
        self.popup_calls = 0
        self.finished_thread = None
        self.current_text_holder = None
        self._source_window_id = None
        self.capture_finished_signal.connect(self.finished)

    def _read_clipboard(self, timeout=1.5):
        self.reads += 1
        if self.reads == 1:
            self.entered.set()
            self.release.wait(1)
        return self.clipboard

    def _press_ctrl(self, key):
        self.clipboard = "worker selection"
        return True

    def _inject_ctrl(self, *args, **kwargs):
        return self._press_ctrl("insert")

    def activeWindow(self):
        return None

    @QtCore.Slot(object, str)
    def finished(self, holder, primary):
        self.finished_thread = threading.get_ident()
        app_module.WritingToolApp._on_capture_finished(self, holder, primary)

    def _open_popup(self):
        self.popup_calls += 1

    def process_option(self, _option):
        raise AssertionError("not a direct capture")


class TestQueuedCaptureWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_worker_keeps_gui_responsive_and_popup_waits_for_completion(self):
        harness = WorkerHarness()
        holder = app_module._SelectedTextHolder()
        holder.show_popup = True
        harness.current_text_holder = holder
        timer_fired = []
        QtCore.QTimer.singleShot(0, lambda: timer_fired.append(True))
        with (
            mock.patch.object(app_module.sys, "platform", "win32"),
            mock.patch.object(app_module.pyperclip, "copy", side_effect=lambda text: setattr(harness, "clipboard", text)),
            mock.patch.object(app_module.WritingToolApp, "_capture_source_window", return_value="source-window"),
        ):
            app_module.WritingToolApp._fire_ctrl_c_and_capture_async(harness, holder)
            self.assertTrue(harness.entered.wait(1))
            loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(20, loop.quit)
            loop.exec()
            self.assertTrue(timer_fired)
            self.assertEqual(harness.popup_calls, 0)
            harness.release.set()
            loop = QtCore.QEventLoop()
            poll = QtCore.QTimer()
            poll.timeout.connect(lambda: loop.quit() if holder.ready.is_set() else None)
            poll.start(1)
            QtCore.QTimer.singleShot(1000, loop.quit)
            loop.exec()
            self.assertTrue(holder.ready.is_set())
        self.assertEqual(harness.popup_calls, 1)
        self.assertEqual(harness.finished_thread, threading.get_ident())
        self.assertEqual(harness._source_window_id, "source-window")


class TestOptionCaptureCorrelation(unittest.TestCase):
    def test_deferred_option_worker_uses_holder_snapshotted_at_dispatch(self):
        first = app_module._SelectedTextHolder()
        first.text = "original selection"
        first.ready.set()
        newer = app_module._SelectedTextHolder()
        newer.text = "newer selection"
        newer.ready.set()
        queued = []
        prompts = []

        class DeferredThread:
            def __init__(self, *, target, args, **_kwargs):
                queued.append((target, args))

            def start(self):
                pass

        class Provider:
            def get_response(self, instruction, prompt):
                prompts.append((instruction, prompt))

        class App:
            def __init__(self):
                self.current_text_holder = first
                self.current_response_window = None
                self.options = {
                    "Rewrite": {
                        "open_in_window": False,
                        "prefix": "Rewrite: ",
                        "instruction": "system instruction",
                    }
                }
                self.current_provider = Provider()
                self.output_queue = ""

            def process_option_thread(self, *args):
                return app_module.WritingToolApp.process_option_thread(self, *args)

        app = App()
        with mock.patch.object(app_module.threading, "Thread", DeferredThread):
            app_module.WritingToolApp.process_option(app, "Rewrite")
        app.current_text_holder = newer
        target, args = queued.pop()
        target(*args)
        self.assertEqual(prompts, [("system instruction", "Rewrite: original selection")])


if __name__ == "__main__":
    unittest.main()
