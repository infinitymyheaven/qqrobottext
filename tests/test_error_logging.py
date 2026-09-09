"""错误现场日志的落盘范围与轮换配置测试。"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from src.error_logging import ErrorContextHandler


class ErrorContextHandlerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "nested" / "errors.txt"
        self.logger = logging.getLogger(f"test.error-context.{id(self)}")
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)

    def tearDown(self):
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            handler.close()
        self.temp_dir.cleanup()

    def _install(self, *, before=2, after=2):
        handler = ErrorContextHandler(
            self.path,
            before_records=before,
            after_records=after,
            max_bytes=1024 * 1024,
            backup_count=1,
        )
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger.addHandler(handler)
        return handler

    def test_file_is_created_only_after_error_and_contains_limited_context(self):
        self._install(before=2, after=2)
        self.logger.info("太早的状态")
        self.logger.info("错误前状态一")
        self.logger.warning("错误前状态二")
        self.assertFalse(self.path.exists())

        try:
            raise RuntimeError("测试故障")
        except RuntimeError:
            self.logger.exception("处理失败")
        self.logger.info("错误后状态一")
        self.logger.info("错误后状态二")
        self.logger.info("不会立即写入的状态")

        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn("太早的状态", content)
        self.assertIn("错误前状态一", content)
        self.assertIn("错误前状态二", content)
        self.assertIn("RuntimeError: 测试故障", content)
        self.assertIn("错误后状态一", content)
        self.assertIn("错误后状态二", content)
        self.assertNotIn("不会立即写入的状态", content)
        self.assertIn("错误现场结束", content)

    def test_zero_context_still_records_error(self):
        self._install(before=0, after=0)
        self.logger.error("唯一错误")
        content = self.path.read_text(encoding="utf-8")
        self.assertIn("唯一错误", content)
        self.assertIn("没有可用的错误前日志", content)

    def test_rotation_keeps_txt_extension(self):
        handler = ErrorContextHandler(
            self.path,
            before_records=0,
            after_records=0,
            max_bytes=256,
            backup_count=1,
        )
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger.addHandler(handler)
        self.logger.error("需要轮换的错误内容" * 30)
        self.logger.error("第二个错误" * 30)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.path.with_name("errors.1.txt").exists())


if __name__ == "__main__":
    unittest.main()
