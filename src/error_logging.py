"""只在发生错误时落盘的轻量日志处理器。"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic


class ErrorContextHandler(logging.Handler):
    """缓存近期状态，并在 ERROR 出现时保存错误前后的有限上下文。"""

    def __init__(
        self,
        path: str | Path,
        *,
        before_records: int = 30,
        after_records: int = 10,
        max_bytes: int = 1_048_576,
        backup_count: int = 2,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.path = Path(path)
        self.before_records = before_records
        self.after_records = after_records
        self._recent: deque[str] = deque(maxlen=before_records)
        self._after_remaining = 0
        self._started_at = monotonic()
        self._writer = RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        # 让轮换文件仍保持 .txt 后缀，例如 error_context.1.txt。
        self._writer.namer = self._rotated_name
        self._writer.setFormatter(logging.Formatter("%(message)s"))

    @staticmethod
    def _rotated_name(default_name: str) -> str:
        path = Path(default_name)
        # RotatingFileHandler 默认生成 name.txt.1。
        if len(path.suffixes) >= 2 and path.suffixes[-2] == ".txt":
            number = path.suffixes[-1].lstrip(".")
            base = path.name[: -len(".txt." + number)]
            return str(path.with_name(f"{base}.{number}.txt"))
        return default_name

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = logging.LogRecord(
            name="qqrobot.error_context",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=text,
            args=(),
            exc_info=None,
        )
        self._writer.emit(record)

    def _incident_header(self, record: logging.LogRecord) -> str:
        happened_at = datetime.fromtimestamp(record.created).astimezone()
        return (
            "\n"
            "==================== 错误现场 ====================\n"
            f"时间: {happened_at.isoformat(timespec='seconds')}\n"
            f"级别: {record.levelname}\n"
            f"日志器: {record.name}\n"
            f"进程: {os.getpid()}  线程: {threading.current_thread().name}\n"
            f"Python: {sys.version.split()[0]}  运行时长: {monotonic() - self._started_at:.1f} 秒\n"
            f"工作目录: {Path.cwd()}\n"
            "-------------------- 错误前状态 --------------------"
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            rendered = self.format(record)
            is_error = record.levelno >= logging.ERROR

            if is_error:
                if self._after_remaining == 0:
                    self._write(self._incident_header(record))
                    if self._recent:
                        for line in self._recent:
                            self._write(line)
                    else:
                        self._write("（没有可用的错误前日志）")
                    self._write("---------------------- 错误 ----------------------")
                else:
                    self._write("-------------------- 后续错误 --------------------")
                self._write(rendered)
                self._write("-------------------- 错误后状态 --------------------")
                self._after_remaining = self.after_records
                if self._after_remaining == 0:
                    self._write("================== 错误现场结束 ==================\n")
                return

            if self._after_remaining > 0:
                self._write(rendered)
                self._after_remaining -= 1
                if self._after_remaining == 0:
                    self._write("================== 错误现场结束 ==================\n")

            if self.before_records:
                self._recent.append(rendered)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self._writer.close()
        finally:
            super().close()


def install_error_context_handler(
    path: str | Path,
    *,
    before_records: int,
    after_records: int,
    max_bytes: int,
    backup_count: int,
) -> ErrorContextHandler:
    """在根日志器安装单个错误现场处理器，重复调用不会叠加。"""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, ErrorContextHandler):
            root.removeHandler(existing)
            existing.close()
    handler = ErrorContextHandler(
        path,
        before_records=before_records,
        after_records=after_records,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    return handler
