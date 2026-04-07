"""
tmux Session Manager

借鉴自 claude-squad (https://github.com/smtg-ai/claude-squad)
TMUX 会话管理核心模块，提供进程隔离和终端交互能力。

核心功能:
- 创建/销毁 tmux 会话
- PTY 附加和分离
- 面板内容捕获
- 按键发送
- 状态监控
"""

import os
import sys
import time
import hashlib
import re
import subprocess
import threading
import contextlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable
from pathlib import Path


# tmux 会话名称前缀
TMUX_PREFIX = "mma_"

# 支持的程序类型
PROGRAM_CLAUDE = "claude"
PROGRAM_AIDER = "aider"
PROGRAM_GEMINI = "gemini"


class SessionStatus(Enum):
    """会话状态枚举"""
    RUNNING = "running"      # 运行中，工作状态
    READY = "ready"         # 就绪，等待输入
    LOADING = "loading"      # 加载中
    PAUSED = "paused"       # 已暂停
    UNKNOWN = "unknown"      # 未知


@dataclass
class TmuxSessionConfig:
    """tmux 会话配置"""
    name: str
    program: str
    work_dir: str = "."
    env: dict = field(default_factory=dict)
    detached: bool = True  # 是否在后台创建


class TmuxSession:
    """
    tmux 会话管理器

    提供完整的 tmux 会话生命周期管理，包括：
    - 会话创建和销毁
    - PTY 附加和窗口大小调整
    - 面板内容捕获
    - 按键注入
    - 状态监控
    """

    def __init__(self, config: TmuxSessionConfig):
        self.config = config
        self._sanitized_name = self._sanitize_name(config.name)
        self._program = config.program
        self._work_dir = config.work_dir
        self._env = config.env

        # PTY 文件描述符
        self._ptmx: Optional[object] = None

        # 状态监控
        self._prev_output_hash: Optional[bytes] = None
        self._lock = threading.Lock()

        # 回调函数
        self._on_update: Optional[Callable[[str], None]] = None
        self._on_prompt: Optional[Callable[[], None]] = None

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """
        清理会话名称，tmux 对特殊字符有限制

        - 替换空白字符为下划线
        - 替换点号为下划线（tmux 会把点号作为会话分隔符）
        """
        name = re.sub(r'\s+', '_', name)
        name = name.replace('.', '_')
        return f"{TMUX_PREFIX}{name}"

    @property
    def name(self) -> str:
        """获取原始名称"""
        return self.config.name

    @property
    def sanitized_name(self) -> str:
        """获取 tmux 安全的名称"""
        return self._sanitized_name

    @property
    def program(self) -> str:
        """获取程序名称"""
        return self._program

    def does_session_exist(self) -> bool:
        """检查会话是否存在"""
        result = subprocess.run(
            ["tmux", "has-session", f"-t={self._sanitized_name}"],
            capture_output=True
        )
        return result.returncode == 0

    def start(self, detached: bool = True) -> bool:
        """
        创建并启动新会话

        Args:
            detached: 是否立即分离

        Returns:
            bool: 启动是否成功
        """
        if self.does_session_exist():
            raise RuntimeError(f"tmux session already exists: {self._sanitized_name}")

        # 构建环境变量
        env = os.environ.copy()
        env.update(self._env)

        # 创建新会话
        cmd = [
            "tmux", "new-session",
            "-d",  # 分离模式
            "-s", self._sanitized_name,
            "-c", self._work_dir,
        ]

        # 添加程序和参数
        if self._program:
            cmd.append(self._program)

        try:
            subprocess.run(cmd, check=True, env=env)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"failed to start tmux session: {e}")

        # 等待会话真正创建（tmux 需要一点时间）
        if not self._wait_for_session():
            self.close()
            raise RuntimeError(f"timed out waiting for tmux session: {self._sanitized_name}")

        # 设置历史记录限制
        self._set_history_limit(10000)

        # 设置鼠标模式
        self._set_mouse_mode(True)

        return True

    def _wait_for_session(self, timeout: float = 2.0) -> bool:
        """等待会话创建成功"""
        deadline = time.time() + timeout
        interval = 0.005

        while time.time() < deadline:
            if self.does_session_exist():
                return True
            time.sleep(interval)
            interval = min(interval * 2, 0.05)  # 指数退避，最多 50ms

        return False

    def _set_history_limit(self, limit: int) -> None:
        """设置历史记录限制"""
        subprocess.run(
            ["tmux", "set-option", "-t", self._sanitized_name, "history-limit", str(limit)],
            capture_output=True
        )

    def _set_mouse_mode(self, enabled: bool) -> None:
        """设置鼠标模式"""
        mode = "on" if enabled else "off"
        subprocess.run(
            ["tmux", "set-option", "-t", self._sanitized_name, "mouse", mode],
            capture_output=True
        )

    def restore(self) -> None:
        """
        恢复会话附加

        用于重新附加到已存在的会话
        """
        # 在当前进程创建新的 PTY
        self._ptmx = None  # 简化实现：暂不使用 PTY 直接附加

    def attach(self) -> None:
        """
        附加到会话

        这是一个简化实现，实际使用时建议直接用 tmux attach
        """
        subprocess.run(
            ["tmux", "attach-session", "-t", self._sanitized_name],
            check=True
        )

    def detach(self) -> None:
        """
        分离会话

        发送 detach 信号到 tmux 会话
        """
        subprocess.run(
            ["tmux", "detach-client", "-s", self._sanitized_name],
            capture_output=True
        )

    def close(self) -> None:
        """
        关闭会话

        杀死 tmux 会话并清理资源
        """
        if self._ptmx:
            with contextlib.suppress(Exception):
                self._ptmx.close()
            self._ptmx = None

        subprocess.run(
            ["tmux", "kill-session", "-t", self._sanitized_name],
            capture_output=True
        )

    def send_keys(self, keys: str) -> None:
        """
        发送按键到会话

        Args:
            keys: 要发送的按键序列
        """
        # 编码按键序列
        encoded = keys.encode('utf-8')

        # 使用 tmux send-keys 发送
        # 注意：这不会处理需要特殊转义的字符
        subprocess.run(
            ["tmux", "send-keys", "-t", self._sanitized_name, "-l"] + keys.split(),
            capture_output=True
        )

    def send_text(self, text: str) -> None:
        """
        发送文本到会话（逐字符）

        用于发送包含特殊字符的文本
        """
        for char in text:
            subprocess.run(
                ["tmux", "send-keys", "-t", self._sanitized_name, char],
                capture_output=True
            )
            time.sleep(0.01)  # 避免发送过快

    def send_enter(self) -> None:
        """发送回车键"""
        subprocess.run(
            ["tmux", "send-keys", "-t", self._sanitized_name, "Enter"],
            capture_output=True
        )

    def capture_pane(self, start: int = "-", end: int = "-") -> str:
        """
        捕获面板内容

        Args:
            start: 起始行 ("-" 表示历史开头)
            end: 结束行 ("-" 表示当前行)

        Returns:
            str: 面板内容
        """
        cmd = [
            "tmux", "capture-pane",
            "-p",        # 输出到 stdout
            "-e",        # 保留转义序列（颜色等）
            "-J",        # 连接被折行的行
            "-t", self._sanitized_name,
            "-S", start,
            "-E", end,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"failed to capture pane: {result.stderr}")

        return result.stdout

    def has_updated(self) -> tuple[bool, bool]:
        """
        检查面板是否更新，并检测是否处于 prompt 状态

        Returns:
            tuple: (是否更新, 是否处于 prompt)
        """
        content = self.capture_pane()

        # 检测 prompt 状态
        has_prompt = self._detect_prompt(content)

        # 检查内容哈希
        content_hash = hashlib.sha256(content.encode()).digest()
        if content_hash != self._prev_output_hash:
            self._prev_output_hash = content_hash
            return True, has_prompt

        return False, has_prompt

    def _detect_prompt(self, content: str) -> bool:
        """
        检测是否为等待输入状态

        Args:
            content: 面板内容

        Returns:
            bool: 是否处于 prompt 状态
        """
        if self._program == PROGRAM_CLAUDE:
            # Claude Code 特有的 prompt 检测
            patterns = [
                "No, and tell Claude what to do differently",
                "Do you want to proceed?",
                "Enter to confirm",
            ]
            return any(p in content for p in patterns)

        elif self._program.startswith(PROGRAM_AIDER):
            # Aider 的 prompt
            return "(Y)es/(N)o/(D)on't ask again" in content

        elif self._program.startswith(PROGRAM_GEMINI):
            # Gemini CLI 的 prompt
            return "Yes, allow once" in content

        return False

    def check_and_handle_trust_prompt(self) -> bool:
        """
        检查并处理信任提示

        Returns:
            bool: 是否处理了提示
        """
        content = self.capture_pane()

        if self._program == PROGRAM_CLAUDE:
            if "Do you trust the files in this folder?" in content:
                self.send_enter()
                return True
            if "new MCP server" in content:
                self.send_enter()
                return True

        elif self._program.startswith(PROGRAM_AIDER):
            if "Open documentation url for more info" in content:
                self.send_keys("d")
                self.send_enter()
                return True

        return False

    def set_window_size(self, width: int, height: int) -> None:
        """
        设置窗口大小

        Args:
            width: 宽度（字符数）
            height: 高度（行数）
        """
        # tmux 无法直接设置窗口大小，但可以调整客户端
        # 这个功能需要通过 resize-window 命令实现
        subprocess.run(
            ["tmux", "resize-window", "-t", self._sanitized_name,
             "-x", str(width), "-y", str(height)],
            capture_output=True
        )

    def rename(self, new_name: str) -> str:
        """
        重命名会话

        Args:
            new_name: 新名称

        Returns:
            str: 新的 tmux 会话名称
        """
        old_name = self._sanitized_name
        self._sanitized_name = self._sanitize_name(new_name)
        self.config.name = new_name

        subprocess.run(
            ["tmux", "rename-session", "-t", old_name, self._sanitized_name],
            check=True
        )

        return self._sanitized_name


class TmuxPool:
    """
    tmux 会话池

    管理多个 tmux 会话的生命周期
    """

    def __init__(self):
        self._sessions: dict[str, TmuxSession] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        name: str,
        program: str,
        work_dir: str = ".",
        env: Optional[dict] = None
    ) -> TmuxSession:
        """
        创建新会话

        Args:
            name: 会话名称
            program: 程序名称
            work_dir: 工作目录
            env: 环境变量

        Returns:
            TmuxSession: 创建的会话对象
        """
        with self._lock:
            if name in self._sessions:
                raise ValueError(f"session already exists: {name}")

            config = TmuxSessionConfig(
                name=name,
                program=program,
                work_dir=work_dir,
                env=env or {}
            )

            session = TmuxSession(config)
            session.start()

            self._sessions[name] = session
            return session

    def get_session(self, name: str) -> Optional[TmuxSession]:
        """获取会话"""
        return self._sessions.get(name)

    def list_sessions(self) -> list[str]:
        """列出所有会话"""
        return list(self._sessions.keys())

    def close_session(self, name: str) -> bool:
        """
        关闭会话

        Returns:
            bool: 是否成功关闭
        """
        with self._lock:
            session = self._sessions.pop(name, None)
            if session:
                session.close()
                return True
            return False

    def close_all(self) -> None:
        """关闭所有会话"""
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()

    def cleanup_stale_sessions(self) -> int:
        """
        清理孤立的 tmux 会话

        Returns:
            int: 清理的会话数量
        """
        count = 0

        # 列出所有以 TMUX_PREFIX 开头的 tmux 会话
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return 0

        existing_sessions = set()
        for line in result.stdout.strip().split('\n'):
            if line:
                existing_sessions.add(line)

        # 清理不在池中但仍然存在的会话
        for session_name in existing_sessions:
            if session_name.startswith(TMUX_PREFIX):
                base_name = session_name[len(TMUX_PREFIX):]
                if base_name not in self._sessions:
                    subprocess.run(
                        ["tmux", "kill-session", "-t", session_name],
                        capture_output=True
                    )
                    count += 1

        return count


# 全局会话池实例
_global_pool: Optional[TmuxPool] = None


def get_global_pool() -> TmuxPool:
    """获取全局会话池"""
    global _global_pool
    if _global_pool is None:
        _global_pool = TmuxPool()
    return _global_pool
