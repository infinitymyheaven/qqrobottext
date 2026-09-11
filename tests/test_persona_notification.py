"""人格草稿通知与无 shell 启动器测试。"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import src.persona_notification as notification


class PersonaNotificationTest(unittest.TestCase):
    def setUp(self):
        notification._notifier = None
        notification._started = False
        notification._callbacks.clear()

    def test_evaluation_command_validates_version_and_uses_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            args, root = notification.evaluation_command(7, Path(directory))
        self.assertEqual(args[:3], ["powershell.exe", "-NoExit", "-Command"])
        self.assertIn("evaluate 7", args[3])
        self.assertTrue(root.is_absolute())
        with self.assertRaises(ValueError):
            notification.evaluation_command(0)

    def test_launcher_never_uses_shell(self):
        with patch.object(notification.subprocess, "Popen") as popen:
            notification.launch_evaluation(2)
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertTrue(Path(kwargs["cwd"]).is_absolute())

    def test_disabled_and_missing_dependency_fall_back_without_sensitive_text(self):
        with patch("builtins.print") as output:
            self.assertFalse(notification.notify_persona_draft(3, 12, enabled=False))
        rendered = " ".join(str(value) for call in output.call_args_list for value in call.args)
        self.assertIn("evaluate 3", rendered)
        self.assertNotIn("123456789", rendered)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winotify": None}),
            patch("builtins.print"),
            self.assertLogs("qqrobot", level="ERROR"),
        ):
            self.assertFalse(notification.notify_persona_draft(4, 10))

    def test_success_registers_click_callback_and_launch_failure_is_contained(self):
        created = []

        class FakeToast:
            def __init__(self, title, msg):
                self.title = title
                self.msg = msg
                self.callback = None

            def add_actions(self, _label, callback):
                self.callback = callback

            def show(self):
                return None

        class FakeNotifier:
            def __init__(self, _registry):
                self.started = False

            def start(self):
                self.started = True

            def register_callback(self, callback):
                return callback

            def create_notification(self, *, title, msg):
                toast = FakeToast(title, msg)
                created.append(toast)
                return toast

        fake_module = types.SimpleNamespace(
            Notifier=FakeNotifier,
            PYW_EXE="pythonw.exe",
            Registry=lambda *_args, **_kwargs: object(),
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winotify": fake_module}),
            patch.object(notification, "launch_evaluation", side_effect=OSError("mock")) as launch,
            patch("builtins.print"),
        ):
            self.assertTrue(notification.notify_persona_draft(5, 11))
            self.assertEqual(len(created), 1)
            self.assertIn("v5", created[0].title)
            self.assertIn("11", created[0].msg)
            self.assertNotIn("账号", created[0].msg)
            with self.assertLogs("qqrobot", level="ERROR"):
                created[0].callback()
        launch.assert_called_once_with(5, None)


if __name__ == "__main__":
    unittest.main()
