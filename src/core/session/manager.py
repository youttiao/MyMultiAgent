"""
Session Manager

借鉴自 claude-squad (https://github.com/smtg-ai/claude-squad)
会话管理器，负责 Agent 的生命周期和 git worktree 隔离。

核心功能:
- 创建/销毁 Agent 会话
- Git worktree 隔离，防止文件冲突
- 会话状态跟踪
- 多会话协调
"""

import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Callable
import threading

from ..tmux import TmuxSession, TmuxSessionConfig, TmuxPool, SessionStatus


class AgentProgram(Enum):
    """支持的 AI 程序"""
    CLAUDE = "claude"
    AIDER = "aider"
    GEMINI = "gemini"
    OLLAMA = "ollama"


@dataclass
class AgentProfile:
    """
    Agent 配置描述

    定义每个 Agent 的角色、技能和模型配置
    """
    name: str
    program: str
    model: str
    skills: list[str] = field(default_factory=list)
    claude_md_path: Optional[str] = None  # 自定义 CLAUDE.md 路径
    env: dict = field(default_factory=dict)
    auto_yes: bool = False


@dataclass
class AgentSession:
    """
    Agent 会话

    代表一个运行中的 Agent 实例
    """
    id: str
    profile: AgentProfile
    worktree_path: Path
    tmux_session: TmuxSession
    status: SessionStatus = SessionStatus.LOADING
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    branch: Optional[str] = None
    prompt: Optional[str] = None

    # Git 相关信息
    repo_path: Optional[Path] = None
    base_commit: Optional[str] = None


class GitWorktreeManager:
    """
    Git Worktree 管理器

    负责创建和管理 git worktree，为每个 Agent 提供隔离的工作目录
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()

        if not self.repo_path.exists():
            raise ValueError(f"Repository path does not exist: {self.repo_path}")

        if not (self.repo_path / ".git").exists():
            raise ValueError(f"Not a git repository: {self.repo_path}")

    def create_worktree(
        self,
        name: str,
        branch: Optional[str] = None,
        create_branch: bool = True
    ) -> tuple[Path, str]:
        """
        创建新的 worktree

        Args:
            name: worktree 名称
            branch: 分支名称（None 则从 HEAD 创建）
            create_branch: 是否创建新分支

        Returns:
            tuple: (worktree_path, branch_name)
        """
        worktree_path = self.repo_path.parent / f"{self.repo_path.name}-{name}"

        # 获取基础提交
        base_commit = self._get_current_commit()

        # 确定分支名
        if branch is None:
            branch = f"agent/{name}"

        # 如果 worktree 已存在，先删除
        if worktree_path.exists():
            self._remove_worktree(name)

        # 创建 worktree
        cmd = [
            "git", "-C", str(self.repo_path),
            "worktree", "add",
        ]

        if create_branch:
            cmd.extend(["-b", branch])
        else:
            cmd.append(branch)

        cmd.extend([str(worktree_path), branch])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {result.stderr}")

        return worktree_path, branch

    def _get_current_commit(self) -> str:
        """获取当前 HEAD 提交"""
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()

    def _remove_worktree(self, name: str) -> None:
        """删除 worktree"""
        worktree_path = self.repo_path.parent / f"{self.repo_path.name}-{name}"

        if worktree_path.exists():
            subprocess.run(
                ["git", "-C", str(self.repo_path), "worktree", "remove",
                 str(worktree_path), "--force"],
                capture_output=True
            )

    def list_worktrees(self) -> list[dict]:
        """列出所有 worktree"""
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return []

        worktrees = []
        current = {}

        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line:
                continue

            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[9:]}
            elif line.startswith("branch "):
                current["branch"] = line[8:]
            elif line == "detached":
                current["detached"] = True

        if current:
            worktrees.append(current)

        return worktrees

    def cleanup_all_worktrees(self) -> None:
        """清理所有 agent 相关的 worktree"""
        worktrees = self.list_worktrees()

        for wt in worktrees:
            path = wt.get("path", "")
            branch = wt.get("branch", "")

            # 只清理 agent/ 开头的分支
            if branch and branch.startswith("agent/"):
                subprocess.run(
                    ["git", "-C", str(self.repo_path), "worktree", "remove", path, "--force"],
                    capture_output=True
                )


class SessionManager:
    """
    会话管理器

    核心管理器，协调 tmux 会话、git worktree 和 Agent 生命周期
    """

    def __init__(
        self,
        repo_path: str,
        worktrees_base: Optional[str] = None
    ):
        self.repo_path = Path(repo_path).resolve()
        self.worktrees_base = Path(worktrees_base) if worktrees_base else self.repo_path.parent

        self._worktree_manager = GitWorktreeManager(str(self.repo_path))
        self._tmux_pool = TmuxPool()

        self._sessions: dict[str, AgentSession] = {}
        self._lock = threading.Lock()

        # 回调函数
        self._on_session_update: Optional[Callable[[AgentSession], None]] = None
        self._on_session_prompt: Optional[Callable[[AgentSession], None]] = None

    def create_agent_session(
        self,
        profile: AgentProfile,
        branch: Optional[str] = None,
        initial_prompt: Optional[str] = None
    ) -> AgentSession:
        """
        创建新的 Agent 会话

        Args:
            profile: Agent 配置
            branch: git 分支（None 则自动创建）
            initial_prompt: 初始提示

        Returns:
            AgentSession: 创建的会话
        """
        with self._lock:
            if profile.name in self._sessions:
                raise ValueError(f"Session already exists: {profile.name}")

            # 创建 git worktree
            worktree_path, actual_branch = self._worktree_manager.create_worktree(
                name=profile.name,
                branch=branch
            )

            # 配置环境变量
            env = profile.env.copy()
            if profile.claude_md_path:
                env["CLAUDE_MD_PATH"] = profile.claude_md_path

            # 创建 tmux 会话
            tmux_config = TmuxSessionConfig(
                name=profile.name,
                program=self._build_command(profile),
                work_dir=str(worktree_path),
                env=env
            )

            tmux_session = TmuxSession(tmux_config)
            tmux_session.start()

            # 创建会话对象
            session = AgentSession(
                id=profile.name,
                profile=profile,
                worktree_path=worktree_path,
                tmux_session=tmux_session,
                status=SessionStatus.LOADING,
                branch=actual_branch,
                prompt=initial_prompt,
                repo_path=self.repo_path,
                base_commit=self._worktree_manager._get_current_commit()
            )

            self._sessions[profile.name] = session

            # 如果有初始提示，发送
            if initial_prompt:
                self._send_initial_prompt(session, initial_prompt)

            return session

    def _build_command(self, profile: AgentProfile) -> str:
        """
        构建 Agent 启动命令

        Args:
            profile: Agent 配置

        Returns:
            str: 完整的启动命令
        """
        cmd_parts = [profile.program]

        # 添加模型参数（如果程序支持）
        if profile.model:
            if profile.program == "claude":
                cmd_parts.extend(["--model", profile.model])
            elif profile.program == "aider":
                cmd_parts.extend(["--model", profile.model])
            elif profile.program == "gemini":
                cmd_parts.extend(["--model", profile.model])

        # 添加 auto-yes 参数
        if profile.auto_yes:
            cmd_parts.append("--yes")

        return " ".join(cmd_parts)

    def _send_initial_prompt(self, session: AgentSession, prompt: str) -> None:
        """发送初始提示"""
        # 等待会话就绪
        time.sleep(2)

        # 发送提示
        session.tmux_session.send_text(prompt)
        session.tmux_session.send_enter()

    def get_session(self, name: str) -> Optional[AgentSession]:
        """获取会话"""
        return self._sessions.get(name)

    def list_sessions(self) -> list[AgentSession]:
        """列出所有会话"""
        return list(self._sessions.values())

    def update_session_status(self, name: str) -> SessionStatus:
        """
        更新会话状态

        Returns:
            SessionStatus: 更新后的状态
        """
        session = self._sessions.get(name)
        if not session:
            return SessionStatus.UNKNOWN

        # 检查 tmux 会话是否存在
        if not session.tmux_session.does_session_exist():
            session.status = SessionStatus.UNKNOWN
            return session.status

        # 检查是否有更新和 prompt
        updated, has_prompt = session.tmux_session.has_updated()

        if has_prompt:
            session.status = SessionStatus.READY
            if self._on_session_prompt:
                self._on_session_prompt(session)
        elif updated:
            session.status = SessionStatus.RUNNING

        session.updated_at = datetime.now()

        if self._on_session_update:
            self._on_session_update(session)

        return session.status

    def update_all_status(self) -> dict[str, SessionStatus]:
        """更新所有会话状态"""
        return {
            name: self.update_session_status(name)
            for name in self._sessions
        }

    def send_message(self, name: str, message: str) -> bool:
        """
        向会话发送消息

        Args:
            name: 会话名称
            message: 消息内容

        Returns:
            bool: 是否发送成功
        """
        session = self._sessions.get(name)
        if not session:
            return False

        session.tmux_session.send_text(message)
        session.tmux_session.send_enter()
        return True

    def pause_session(self, name: str) -> bool:
        """
        暂停会话（暂停 worktree）

        Returns:
            bool: 是否成功暂停
        """
        session = self._sessions.get(name)
        if not session:
            return False

        session.status = SessionStatus.PAUSED
        session.tmux_session.detach()
        return True

    def resume_session(self, name: str) -> bool:
        """
        恢复会话

        Returns:
            bool: 是否成功恢复
        """
        session = self._sessions.get(name)
        if not session:
            return False

        session.tmux_session.attach()
        session.status = SessionStatus.READY
        return True

    def close_session(self, name: str) -> bool:
        """
        关闭会话

        Args:
            name: 会话名称

        Returns:
            bool: 是否成功关闭
        """
        with self._lock:
            session = self._sessions.pop(name, None)
            if not session:
                return False

            # 关闭 tmux 会话
            session.tmux_session.close()

            # 清理 worktree（可选，保留以便恢复）
            # self._worktree_manager._remove_worktree(name)

            return True

    def close_all(self) -> None:
        """关闭所有会话"""
        with self._lock:
            for session in list(self._sessions.values()):
                session.tmux_session.close()
            self._sessions.clear()

    def cleanup_stale(self) -> int:
        """
        清理孤立资源

        Returns:
            int: 清理的会话数量
        """
        count = self._tmux_pool.cleanup_stale_sessions()

        # 清理不在管理器中但仍然存在的 tmux 会话
        self._tmux_pool.cleanup_stale_sessions()

        return count

    def get_session_output(self, name: str) -> Optional[str]:
        """
        获取会话的最近输出

        Args:
            name: 会话名称

        Returns:
            str: 最近的面板输出
        """
        session = self._sessions.get(name)
        if not session:
            return None

        try:
            return session.tmux_session.capture_pane()
        except Exception:
            return None


# 全局会话管理器实例
_global_manager: Optional[SessionManager] = None


def get_global_manager() -> Optional[SessionManager]:
    """获取全局会话管理器"""
    return _global_manager


def init_global_manager(repo_path: str, **kwargs) -> SessionManager:
    """初始化全局会话管理器"""
    global _global_manager
    _global_manager = SessionManager(repo_path, **kwargs)
    return _global_manager
