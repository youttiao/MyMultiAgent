"""
session 模块

会话管理核心模块，包括 tmux 会话和 git worktree 隔离
"""

from .manager import (
    SessionManager,
    AgentSession,
    AgentProfile,
    GitWorktreeManager,
    SessionStatus,
    AgentProgram,
    get_global_manager,
    init_global_manager,
)

__all__ = [
    'SessionManager',
    'AgentSession',
    'AgentProfile',
    'GitWorktreeManager',
    'SessionStatus',
    'AgentProgram',
    'get_global_manager',
    'init_global_manager',
]
