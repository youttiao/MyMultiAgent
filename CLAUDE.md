# MyMultiAgent Development Guide

## 项目概述

这是一个基于 Claude Code 的多智能体管理系统，核心目标是：
- 支持多模型路由（Lead 用 Minimax，Worker 用 Haiku 等免费模型）
- 每个 Agent 拥有独立的 Skill 配置
- 基于 tmux 实现会话隔离和并行执行
- 提供 Web UI 实时监控

## 架构决策

### 为什么选择 tmux 作为隔离层？

1. **进程隔离**: 每个 Agent 运行在独立的 tmux session 中
2. **零冲突**: 使用 git worktree 隔离不同 Agent 的工作目录
3. **成熟稳定**: tmux 是经过验证的终端多路复用器
4. **跨平台**: 支持 Linux/macOS，Windows 通过 WSL

### 为什么用 Python 而不是 Go？

1. **快速原型**: Python 开发效率更高
2. **与 Claude Code 对接**: Claude Code 本身是 Node.js，可以作为外部进程调用
3. **监控面板**: agent-foreman 是 Python，可以直接借鉴
4. **便于集成**: 很多 AI 工具链是 Python

## 核心模块

### src/core/tmux/session.py

tmux 会话管理器，借鉴自 [claude-squad](https://github.com/smtg-ai/claude-squad)。

关键类：`TmuxSession`
- `start()`: 创建新会话
- `attach()`: 附加到会话
- `detach()`: 分离会话
- `send_keys()`: 发送按键
- `capture_pane()`: 捕获面板内容
- `has_updated()`: 检测更新（用于状态监控）

### src/core/session/manager.py

会话管理器，借鉴自 claude-squad。

关键类：`SessionManager`
- 管理多个 tmux 会话
- 协调 Agent 生命周期
- Git worktree 隔离

### src/monitor/server.py

监控服务器，借鉴自 [agent-foreman](https://github.com/operoncao123/agent-foreman)。

关键功能：
- 解析 Claude/Codex 会话状态
- WebSocket 实时推送
- 消息注入（通过 tmux send-keys）

### src/agents/profile.py

Agent Profile 管理器。

关键类：`AgentProfile`
- 每个 Profile 对应一个 Agent 类型
- 包含 CLAUDE.md、技能列表、模型配置

## 开发原则

1. **KISS**: 保持简单，先实现再优化
2. **模块化**: 每个模块职责单一
3. **借鉴成熟方案**: 从 claude-squad/agent-foreman/ruflo 借鉴代码
4. **测试驱动**: 先写测试再写实现

## TODO

- [ ] 实现核心 tmux 会话管理
- [ ] 实现会话解析（Claude/Codex）
- [ ] 实现 Agent Profile 系统
- [ ] 实现 Web 监控界面
- [ ] 实现简单的任务队列
- [ ] 集成多模型 API
