"""
tmux 模块

从 claude-squad 借鉴的 tmux 会话管理核心模块
"""

from .session import (
    TmuxSession,
    TmuxSessionConfig,
    TmuxPool,
    SessionStatus,
    TMUX_PREFIX,
    PROGRAM_CLAUDE,
    PROGRAM_AIDER,
    PROGRAM_GEMINI,
    get_global_pool,
)

__all__ = [
    'TmuxSession',
    'TmuxSessionConfig',
    'TmuxPool',
    'SessionStatus',
    'TMUX_PREFIX',
    'PROGRAM_CLAUDE',
    'PROGRAM_AIDER',
    'PROGRAM_GEMINI',
    'get_global_pool',
]
