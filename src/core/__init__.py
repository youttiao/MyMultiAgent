"""
core 模块

核心功能模块，包括 tmux 会话管理和消息总线
"""

from .tmux import (
    TmuxSession,
    TmuxSessionConfig,
    TmuxPool,
    SessionStatus,
    get_global_pool,
)

from .session import (
    SessionManager,
    AgentSession,
    AgentProfile,
    GitWorktreeManager,
    init_global_manager,
    get_global_manager,
)

from .message_bus import (
    MessageBus,
    Message,
    MessageType,
    get_message_bus,
    shutdown_message_bus,
)

__all__ = [
    # tmux
    'TmuxSession',
    'TmuxSessionConfig',
    'TmuxPool',
    'SessionStatus',
    'get_global_pool',

    # session
    'SessionManager',
    'AgentSession',
    'AgentProfile',
    'GitWorktreeManager',
    'init_global_manager',
    'get_global_manager',

    # message_bus
    'MessageBus',
    'Message',
    'MessageType',
    'get_message_bus',
    'shutdown_message_bus',
]
