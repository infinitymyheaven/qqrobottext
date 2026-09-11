"""人格草稿的 Windows Toast 通知与安全评测启动器。"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path


logger = logging.getLogger("qqrobot")
APP_ID = "QQ Robot Persona"
_notifier = None
_started = False
_callbacks: list[object] = []


def evaluation_command(version: int, repo_root: Path | None = None) -> tuple[list[str], Path]:
    """返回无 shell 的可见 PowerShell 评测命令。"""
    version = int(version)
    if version <= 0:
        raise ValueError("人格版本必须是正整数")
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    python_path = (root / ".venv" / "Scripts" / "python.exe").resolve()
    collector_path = (root / "src" / "persona_collector.py").resolve()

    def quote(value: Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    command = f"& {quote(python_path)} {quote(collector_path)} evaluate {version}"
    return ["powershell.exe", "-NoExit", "-Command", command], root


def launch_evaluation(version: int, repo_root: Path | None = None) -> subprocess.Popen:
    args, root = evaluation_command(version, repo_root)
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(args, cwd=root, shell=False, creationflags=creationflags)


def notify_persona_draft(
    version: int,
    sample_count: int,
    *,
    enabled: bool = True,
    repo_root: Path | None = None,
) -> bool:
    """弹出可点击通知；任何平台错误都只降级为安全终端提示。"""
    version = int(version)
    sample_count = max(0, int(sample_count))
    args, _root = evaluation_command(version, repo_root)
    fallback = " ".join(args)
    if not enabled:
        print(f"人格草稿 v{version} 待评测：{fallback}")
        return False
    if sys.platform != "win32":
        logger.warning("当前平台不支持 Windows 人格草稿通知")
        print(f"人格草稿 v{version} 待评测：{fallback}")
        return False
    try:
        from winotify import Notifier, PYW_EXE, Registry

        global _notifier, _started
        if _notifier is None:
            registry = Registry(APP_ID, executable=PYW_EXE, script_path=str(Path(__file__).resolve()))
            _notifier = Notifier(registry)
        if not _started:
            _notifier.start()
            _started = True

        def open_evaluation() -> None:
            try:
                launch_evaluation(version, repo_root)
            except Exception:  # noqa: BLE001 - 点击失败不得影响机器人。
                logger.exception("人格草稿评测窗口启动失败")

        open_evaluation.__name__ = f"evaluate_persona_v{version}"
        callback = _notifier.register_callback(open_evaluation)
        _callbacks.append(callback)
        notification = _notifier.create_notification(
            title=f"QQ 机器人人格草稿 v{version}",
            msg=f"已提炼 {sample_count} 条新样本，需要留出评测后才能激活。",
        )
        notification.add_actions("开始评测", callback)
        notification.show()
        print(f"人格草稿 v{version} 已生成；若通知不可用，请运行：{fallback}")
        return True
    except Exception:  # noqa: BLE001 - 通知是最佳努力辅助能力。
        logger.exception("Windows 人格草稿通知不可用")
        print(f"人格草稿 v{version} 待评测：{fallback}")
        return False


if __name__ == "__main__":
    # winotify 通过本脚本的协议启动把点击事件送回仍在运行的机器人进程。
    try:
        from winotify import Notifier, PYW_EXE, Registry

        manager = Notifier(
            Registry(APP_ID, executable=PYW_EXE, script_path=str(Path(__file__).resolve()))
        )
        manager.start()
    except Exception:
        raise SystemExit(1)
