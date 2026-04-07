# MyMultiAgent

> 基于 Claude Code 的多智能体管理系统，支持多模型路由和 Skill 隔离

## 核心特性

- **多 Agent 协作**: 基于 tmux 的隔离会话管理，支持 Claude/Codex/Gemini 等多种 AI 程序
- **多模型路由**: Lead Agent 使用 Minimax，Worker Agent 使用 Haiku 等免费模型
- **Skill 隔离**: 每个 Agent 拥有独立的 CLAUDE.md 和技能配置
- **实时监控**: Web UI 实时查看所有 Agent 状态
- **Git Worktree 隔离**: 多个 Agent 并行工作，零冲突

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│                     MyMultiAgent                            │
│  ┌─────────────────┐  ┌─────────────────────────────────┐ │
│  │  CLI / TUI     │  │  Monitor Server (Web UI)        │ │
│  └────────┬────────┘  └──────────────┬────────────────────┘ │
│           │         tmux 隔离          │                       │
│           ▼                           ▼                       │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │                  tmux Session Pool                      │ │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐             │ │
│  │  │ Lead     │  │ Coder    │  │ Reviewer  │             │ │
│  │  │(Minimax) │  │ (Haiku)  │  │ (Haiku)   │             │ │
│  │  └──────────┘  └──────────┘  └──────────┘             │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

## 快速开始

### 安装依赖

```bash
# macOS
brew install tmux python3

# Python 依赖
pip install -r requirements.txt
```

### 配置 Agent Profiles

编辑 `profiles.yaml`:

```yaml
lead:
  name: "Lead Agent"
  model: minimax
  model_config:
    api_key: "${MINIMAX_API_KEY}"
    base_url: "https://api.minimax.chat"
  skills:
    - orchestration
    - coordination

frontend:
  name: "Frontend Developer"
  model: haiku
  skills:
    - ui-design
    - react
    - css

backend:
  name: "Backend Developer"
  model: haiku
  skills:
    - api-design
    - database
    - security
```

### 启动

```bash
# 启动 TUI 界面
python -m src.cli.main

# 启动 Web 监控
python -m src.monitor.server --port 8787
```

## 项目结构

```
MyMultiAgent/
├── src/
│   ├── core/           # 核心模块
│   │   ├── tmux/       # tmux 会话管理
│   │   └── session/    # 会话管理 + git worktree
│   ├── agents/         # Agent 核心
│   ├── profiles/      # Agent Profile 配置
│   └── monitor/       # 监控模块
├── profiles/          # 每个 Agent 的 CLAUDE.md
├── tests/
└── docs/
```

## 致谢

本项目借鉴了以下开源项目：

- [claude-squad](https://github.com/smtg-ai/claude-squad) - tmux 会话管理和 TUI 界面
- [agent-foreman](https://github.com/operoncao123/agent-foreman) - 监控面板和会话解析
- [claude-agents](https://github.com/wshobson/agents) - Skill 插件系统
- [ruflo](https://github.com/ruvnet/claude-flow) - 多智能体协调架构

## License

MIT
