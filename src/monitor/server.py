"""
Monitor Server

借鉴自 agent-foreman (https://github.com/operoncao123/agent-foreman)
Web 监控服务器 + 配置界面。

核心功能:
- 配置管理界面
- 会话状态解析 (Claude/Codex)
- WebSocket 实时推送 (简化用轮询)
- 消息注入
- Agent 启动/停止
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import socketserver

# 导入核心模块
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.core import (
    SessionManager,
    AgentProfile,
    SessionStatus,
    init_global_manager,
    get_global_manager,
)
from .config import get_config, ConfigStore, ModelEndpoint, Config


# =============================================================================
# 配置
# =============================================================================

DEFAULT_CONFIG = {
    "refresh_interval_sec": 2,
    "session_scan_limit": 120,
    "status": {
        "busy_cpu_threshold": 20.0,
        "active_heartbeat_sec": 120,
        "stale_heartbeat_sec": 900,
        "needs_input_patterns": [
            r"\?$",
            r"please provide",
            r"if you.d like",
            r"can you",
            r"need you to",
            r"would you like",
            r"do you want",
            r"shall i",
            r"should i",
            r"which (option|approach|version|one)",
            r"let me know",
        ],
    },
    "paths": {
        "claude_projects": "~/.claude/projects",
        "claude_todos": "~/.claude/todos",
        "claude_tasks": "~/.claude/tasks",
        "codex_sessions": "~/.codex/sessions",
    },
}


# =============================================================================
# 工具函数
# =============================================================================

def expand_path(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def truncate(text: str, limit: int = 240) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def utc_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def relative_age(age_sec: float) -> str:
    if age_sec < 60:
        return f"{int(age_sec)}s"
    if age_sec < 3600:
        return f"{int(age_sec // 60)}m"
    if age_sec < 86400:
        return f"{int(age_sec // 3600)}h"
    return f"{int(age_sec // 86400)}d"


# =============================================================================
# Agent 状态
# =============================================================================

@dataclass
class AgentInfo:
    """Agent 信息"""
    id: str
    name: str
    agent_type: str
    status: str
    profile_name: str = ""
    recent_output: str = ""
    pending_items: list = field(default_factory=list)
    last_user_message: str = ""
    pid: Optional[int] = None
    cwd: str = ""
    project: str = ""
    branch: str = ""
    heartbeat: float = field(default_factory=utc_now)
    cpu_percent: float = 0.0
    needs_input: bool = False


class AgentMonitor:
    """Agent 状态监控器"""

    def __init__(self, config: dict = None):
        self.config = config or DEFAULT_CONFIG
        self._agents: dict[str, AgentInfo] = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._session_manager: Optional[SessionManager] = None

    def set_session_manager(self, manager: SessionManager):
        self._session_manager = manager

    def add_agent(self, agent: AgentInfo):
        with self._lock:
            self._agents[agent.id] = agent

    def remove_agent(self, agent_id: str):
        with self._lock:
            self._agents.pop(agent_id, None)

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        return self._agents.get(agent_id)

    def list_agents(self) -> list[AgentInfo]:
        with self._lock:
            return list(self._agents.values())

    def update_from_session_manager(self):
        """从 SessionManager 更新状态"""
        if not self._session_manager:
            return

        # 更新所有会话状态
        self._session_manager.update_all_status()

        for session in self._session_manager.list_sessions():
            agent_id = session.id

            # 获取最新输出
            output = self._session_manager.get_session_output(agent_id) or ""

            # 检测是否需要输入
            needs_input = False
            patterns = self.config.get("status", {}).get("needs_input_patterns", [])
            for pattern in patterns:
                if re.search(pattern, output, re.IGNORECASE):
                    needs_input = True
                    break

            if agent_id in self._agents:
                agent = self._agents[agent_id]
                agent.status = session.status.value
                agent.recent_output = truncate(output, 500)
                agent.needs_input = needs_input
                agent.heartbeat = utc_now()
                agent.branch = session.branch or ""
            else:
                agent = AgentInfo(
                    id=agent_id,
                    name=session.profile.name,
                    agent_type=session.profile.program,
                    status=session.status.value,
                    profile_name=session.profile.name,
                    recent_output=truncate(output, 500),
                    needs_input=needs_input,
                    cwd=str(session.worktree_path),
                    branch=session.branch or "",
                )
                self._agents[agent_id] = agent

    def get_snapshot(self) -> dict:
        agents = []
        now = utc_now()

        for agent in self._agents.values():
            age = now - agent.heartbeat

            if agent.needs_input:
                group = "needs_input"
            elif agent.status == "running":
                group = "working"
            elif age > self.config.get("status", {}).get("stale_heartbeat_sec", 900):
                group = "stale"
            else:
                group = "idle"

            agents.append({
                "id": agent.id,
                "name": agent.name,
                "type": agent.agent_type,
                "status": agent.status,
                "group": group,
                "profile_name": agent.profile_name,
                "recent_output": agent.recent_output,
                "pending_items": agent.pending_items,
                "last_user_message": agent.last_user_message,
                "cwd": agent.cwd,
                "branch": agent.branch,
                "heartbeat": agent.heartbeat,
                "heartbeat_age": relative_age(age),
                "needs_input": agent.needs_input,
            })

        return {
            "timestamp": now,
            "agents": agents,
            "stats": {
                "total": len(agents),
                "needs_input": sum(1 for a in agents if a["group"] == "needs_input"),
                "working": sum(1 for a in agents if a["group"] == "working"),
                "idle": sum(1 for a in agents if a["group"] == "idle"),
                "stale": sum(1 for a in agents if a["group"] == "stale"),
            }
        }

    def start(self):
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _monitor_loop(self):
        interval = self.config.get("refresh_interval_sec", 2)

        while self._running:
            try:
                self.update_from_session_manager()
            except Exception as e:
                print(f"Monitor error: {e}")

            time.sleep(interval)


# =============================================================================
# HTML 模板
# =============================================================================

CONFIG_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MyMultiAgent - Setup</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #eee;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        h1 {
            text-align: center;
            font-size: 2.5rem;
            margin-bottom: 10px;
            background: linear-gradient(90deg, #4ecdc4, #45b7aa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle {
            text-align: center;
            color: #888;
            margin-bottom: 40px;
        }
        .card {
            background: rgba(255,255,255,0.05);
            border-radius: 16px;
            padding: 30px;
            margin-bottom: 20px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        h2 {
            font-size: 1.3rem;
            margin-bottom: 20px;
            color: #4ecdc4;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #aaa;
            font-size: 0.9rem;
        }
        input {
            width: 100%;
            padding: 14px;
            border-radius: 8px;
            border: 1px solid rgba(255,255,255,0.2);
            background: rgba(0,0,0,0.3);
            color: #fff;
            font-size: 1rem;
            transition: border-color 0.2s;
        }
        input:focus {
            outline: none;
            border-color: #4ecdc4;
        }
        input::placeholder {
            color: #555;
        }
        .hint {
            font-size: 0.8rem;
            color: #666;
            margin-top: 5px;
        }
        .btn {
            display: inline-block;
            padding: 14px 28px;
            border-radius: 8px;
            border: none;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-primary {
            background: linear-gradient(90deg, #4ecdc4, #45b7aa);
            color: #000;
        }
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(78,205,196,0.4);
        }
        .btn-block {
            display: block;
            width: 100%;
        }
        .success {
            background: rgba(78,205,196,0.2);
            border: 1px solid #4ecdc4;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .error {
            background: rgba(255,107,107,0.2);
            border: 1px solid #ff6b6b;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .model-badges {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .badge {
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            background: rgba(78,205,196,0.2);
            color: #4ecdc4;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 MyMultiAgent</h1>
        <p class="subtitle">Multi-Agent System Configuration</p>

        <div id="message"></div>

        <div class="card">
            <h2>📡 Lead Agent (Minimax)</h2>
            <div class="model-badges">
                <span class="badge">Model: minimax-2.7</span>
                <span class="badge">Role: Orchestrator</span>
            </div>
            <div class="form-group">
                <label>API Key</label>
                <input type="password" id="lead-api-key" placeholder="sk-xxxxxxxxxxxx">
            </div>
            <div class="form-group">
                <label>Base URL</label>
                <input type="text" id="lead-base-url" placeholder="https://api.minimax.chat/v1" value="https://api.minimax.chat/v1">
                <p class="hint">Minimax API base URL</p>
            </div>
        </div>

        <div class="card">
            <h2>🔧 Worker Agents (OpenRouter / Step Fun)</h2>
            <div class="model-badges">
                <span class="badge">Model: free tier</span>
                <span class="badge">Role: Specialized Workers</span>
            </div>
            <div class="form-group">
                <label>API Key</label>
                <input type="password" id="worker-api-key" placeholder="sk-or-xxxxxxxxxxxx">
            </div>
            <div class="form-group">
                <label>Base URL</label>
                <input type="text" id="worker-base-url" placeholder="https://openrouter.ai/api/v1" value="https://openrouter.ai/api/v1">
                <p class="hint">OpenRouter API base URL</p>
            </div>
            <div class="form-group">
                <label>Model Name</label>
                <input type="text" id="worker-model" placeholder="e.g., step-fun-1" value="anthropic/claude-3-haiku">
                <p class="hint">The actual model identifier to use</p>
            </div>
        </div>

        <button class="btn btn-primary btn-block" onclick="saveConfig()">
            Save Configuration & Launch
        </button>
    </div>

    <script>
        async function saveConfig() {
            const leadApiKey = document.getElementById('lead-api-key').value.trim();
            const leadBaseUrl = document.getElementById('lead-base-url').value.trim();
            const workerApiKey = document.getElementById('worker-api-key').value.trim();
            const workerBaseUrl = document.getElementById('worker-base-url').value.trim();
            const workerModel = document.getElementById('worker-model').value.trim();

            if (!leadApiKey) {
                showMessage('Please enter Lead Agent API Key', 'error');
                return;
            }

            const config = {
                lead_model: {
                    name: 'minimax-2.7',
                    api_key: leadApiKey,
                    base_url: leadBaseUrl,
                    model_name: 'MiniMax-2.7'
                },
                worker_model: {
                    name: 'step-fun',
                    api_key: workerApiKey,
                    base_url: workerBaseUrl,
                    model_name: workerModel || 'anthropic/claude-3-haiku'
                }
            };

            try {
                const resp = await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(config)
                });

                if (resp.ok) {
                    showMessage('Configuration saved! Redirecting to dashboard...', 'success');
                    setTimeout(() => window.location.href = '/dashboard', 1500);
                } else {
                    showMessage('Failed to save configuration', 'error');
                }
            } catch (err) {
                showMessage('Error: ' + err.message, 'error');
            }
        }

        function showMessage(text, type) {
            const div = document.getElementById('message');
            div.className = type;
            div.textContent = text;
            div.style.display = 'block';
        }
    </script>
</body>
</html>
"""


DASHBOARD_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MyMultiAgent - Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
        }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid #333;
        }
        h1 { font-size: 1.8rem; }
        .header-actions { display: flex; gap: 10px; }

        .stats {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat {
            padding: 10px 20px;
            background: #2a2a4e;
            border-radius: 25px;
            font-size: 0.9rem;
        }
        .stat.needs-input { background: #ff6b6b; color: #000; }
        .stat.working { background: #4ecdc4; color: #000; }
        .stat.idle { background: #95a5a6; color: #000; }

        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        .tab {
            padding: 10px 20px;
            background: #2a2a4e;
            border: none;
            border-radius: 8px;
            color: #aaa;
            cursor: pointer;
            transition: all 0.2s;
        }
        .tab:hover { background: #3a3a5e; }
        .tab.active { background: #4ecdc4; color: #000; }

        .agents-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }

        .agent-card {
            background: #2a2a4e;
            border-radius: 12px;
            padding: 20px;
            border-left: 4px solid #95a5a6;
        }
        .agent-card.running, .agent-card.working { border-left-color: #4ecdc4; }
        .agent-card.needs-input { border-left-color: #ff6b6b; background: #2a2a3e; }

        .agent-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .agent-name { font-weight: 600; font-size: 1.1rem; }
        .agent-type {
            font-size: 0.8rem;
            padding: 3px 10px;
            background: #444;
            border-radius: 12px;
        }

        .agent-status {
            display: flex;
            gap: 10px;
            align-items: center;
            margin-bottom: 12px;
        }
        .status-badge {
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 500;
        }
        .status-badge.ready, .status-badge.running { background: #4ecdc4; color: #000; }
        .status-badge.loading { background: #f39c12; color: #000; }
        .status-badge.paused { background: #95a5a6; color: #000; }

        .branch { font-size: 0.85rem; color: #888; }

        .agent-output {
            background: #1a1a2e;
            padding: 12px;
            border-radius: 8px;
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 0.85rem;
            max-height: 180px;
            overflow-y: auto;
            white-space: pre-wrap;
            margin-bottom: 15px;
            color: #aaa;
        }

        .agent-actions {
            display: flex;
            gap: 10px;
        }
        .btn {
            padding: 8px 16px;
            border-radius: 6px;
            border: none;
            font-size: 0.85rem;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-primary { background: #4ecdc4; color: #000; }
        .btn-danger { background: #ff6b6b; color: #000; }
        .btn:hover { transform: translateY(-1px); }

        .empty {
            text-align: center;
            padding: 60px;
            color: #666;
            grid-column: 1 / -1;
        }

        .config-section {
            background: #2a2a4e;
            border-radius: 12px;
            padding: 20px;
            margin-top: 20px;
        }
        .config-section h3 {
            margin-bottom: 15px;
            color: #4ecdc4;
        }
        .config-info {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
        }
        .config-item {
            background: rgba(0,0,0,0.2);
            padding: 12px;
            border-radius: 8px;
        }
        .config-item label {
            font-size: 0.8rem;
            color: #666;
            display: block;
            margin-bottom: 4px;
        }
        .config-item value {
            font-size: 0.95rem;
        }

        footer {
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #333;
        }
        .message-input {
            display: flex;
            gap: 10px;
        }
        #agent-select {
            padding: 12px;
            border-radius: 8px;
            border: 1px solid #444;
            background: #2a2a4e;
            color: #fff;
            min-width: 180px;
        }
        #message-input {
            flex: 1;
            padding: 12px;
            border-radius: 8px;
            border: 1px solid #444;
            background: #2a2a4e;
            color: #fff;
        }
        #send-btn {
            padding: 12px 24px;
            background: #4ecdc4;
            border: none;
            border-radius: 8px;
            color: #000;
            font-weight: 600;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🤖 MyMultiAgent Dashboard</h1>
            <div class="header-actions">
                <button class="btn btn-primary" onclick="startAgents()">+ Start Agents</button>
                <button class="btn btn-danger" onclick="stopAllAgents()">Stop All</button>
            </div>
        </header>

        <div class="stats" id="stats"></div>

        <div class="tabs">
            <button class="tab active" data-group="all">All</button>
            <button class="tab" data-group="needs_input">Needs Input</button>
            <button class="tab" data-group="working">Working</button>
            <button class="tab" data-group="idle">Idle</button>
        </div>

        <div class="agents-grid" id="agents-grid">
            <div class="empty">No agents running. Click "Start Agents" to begin.</div>
        </div>

        <div class="config-section">
            <h3>Current Configuration</h3>
            <div class="config-info" id="config-info">
                <div class="config-item">
                    <label>Lead Model</label>
                    <value id="cfg-lead">Loading...</value>
                </div>
                <div class="config-item">
                    <label>Worker Model</label>
                    <value id="cfg-worker">Loading...</value>
                </div>
            </div>
        </div>

        <footer>
            <div class="message-input">
                <select id="agent-select">
                    <option value="">Select agent...</option>
                </select>
                <input type="text" id="message-input" placeholder="Type a message...">
                <button id="send-btn">Send</button>
            </div>
        </footer>
    </div>

    <script>
        let currentGroup = 'all';
        let agents = [];

        async function fetchStatus() {
            try {
                const resp = await fetch('/api/status');
                const data = await resp.json();

                document.getElementById('stats').innerHTML = `
                    <span class="stat">Total: ${data.stats.total}</span>
                    <span class="stat needs-input">Needs Input: ${data.stats.needs_input}</span>
                    <span class="stat working">Working: ${data.stats.working}</span>
                    <span class="stat idle">Idle: ${data.stats.idle}</span>
                `;

                agents = data.agents;
                renderAgents();
                updateAgentSelect();

            } catch (err) {
                console.error('Fetch error:', err);
            }
        }

        async function fetchConfig() {
            try {
                const resp = await fetch('/api/config');
                const data = await resp.json();

                if (data.lead_model) {
                    document.getElementById('cfg-lead').textContent =
                        `${data.lead_model.name} @ ${data.lead_model.base_url}`;
                }
                if (data.worker_model) {
                    document.getElementById('cfg-worker').textContent =
                        `${data.worker_model.model_name} @ ${data.worker_model.base_url}`;
                }
            } catch (err) {
                console.error('Config fetch error:', err);
            }
        }

        function renderAgents() {
            const grid = document.getElementById('agents-grid');
            const filtered = currentGroup === 'all'
                ? agents
                : agents.filter(a => a.group === currentGroup);

            if (filtered.length === 0) {
                grid.innerHTML = '<div class="empty">No agents in this group</div>';
                return;
            }

            grid.innerHTML = filtered.map(agent => `
                <div class="agent-card ${agent.status} ${agent.needs_input ? 'needs-input' : ''}">
                    <div class="agent-header">
                        <span class="agent-name">${escapeHtml(agent.name)}</span>
                        <span class="agent-type">${agent.type}</span>
                    </div>
                    <div class="agent-status">
                        <span class="status-badge ${agent.status}">${agent.status}</span>
                        ${agent.branch ? `<span class="branch">🌿 ${agent.branch}</span>` : ''}
                    </div>
                    <div class="agent-output">${escapeHtml(agent.recent_output || 'No output yet')}</div>
                    <div class="agent-actions">
                        <button class="btn btn-primary" onclick="sendToAgent('${agent.id}')">Send Message</button>
                        <button class="btn btn-danger" onclick="stopAgent('${agent.id}')">Stop</button>
                    </div>
                </div>
            `).join('');
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text || '';
            return div.innerHTML;
        }

        function updateAgentSelect() {
            const select = document.getElementById('agent-select');
            select.innerHTML = '<option value="">Select agent...</option>' +
                agents.map(a => `<option value="${a.id}">${a.name}</option>`).join('');
        }

        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                currentGroup = tab.dataset.group;
                renderAgents();
            });
        });

        async function startAgents() {
            try {
                await fetch('/api/agents/start', {method: 'POST'});
                setTimeout(fetchStatus, 500);
            } catch (err) {
                alert('Failed to start agents: ' + err.message);
            }
        }

        async function stopAgent(id) {
            try {
                await fetch(`/api/agents/${id}/stop`, {method: 'POST'});
                setTimeout(fetchStatus, 300);
            } catch (err) {
                alert('Failed to stop agent: ' + err.message);
            }
        }

        async function stopAllAgents() {
            try {
                await fetch('/api/agents/stop-all', {method: 'POST'});
                setTimeout(fetchStatus, 300);
            } catch (err) {
                alert('Failed to stop agents: ' + err.message);
            }
        }

        function sendToAgent(id) {
            const msg = prompt('Enter message to send to this agent:');
            if (msg) {
                fetch('/api/message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({agent_id: id, message: msg})
                });
            }
        }

        document.getElementById('send-btn').addEventListener('click', async () => {
            const agentId = document.getElementById('agent-select').value;
            const message = document.getElementById('message-input').value;

            if (!agentId || !message) {
                alert('Please select an agent and enter a message');
                return;
            }

            try {
                await fetch('/api/message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({agent_id: agentId, message})
                });
                document.getElementById('message-input').value = '';
            } catch (err) {
                alert('Failed to send message: ' + err.message);
            }
        });

        // Poll for updates
        setInterval(fetchStatus, 2000);
        fetchStatus();
        fetchConfig();
    </script>
</body>
</html>
"""


# =============================================================================
# Web 服务器
# =============================================================================

class MonitorHTTPHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    monitor: Optional[AgentMonitor] = None
    config_store: Optional[ConfigStore] = None

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "/setup":
            self.send_file_response("text/html", CONFIG_PAGE_HTML)

        elif parsed.path == "/dashboard":
            self.send_file_response("text/html", DASHBOARD_PAGE_HTML)

        elif parsed.path == "/api/config":
            config = self.config_store.config
            self.send_json_response(config.to_dict())

        elif parsed.path == "/api/status":
            if self.monitor:
                self.send_json_response(self.monitor.get_snapshot())
            else:
                self.send_json_response({"agents": [], "stats": {"total": 0}})

        elif parsed.path == "/api/agents":
            agents = self.monitor.list_agents() if self.monitor else []
            self.send_json_response({
                "agents": [{"id": a.id, "name": a.name, "type": a.agent_type, "status": a.status}
                          for a in agents]
            })

        else:
            self.send_error_response(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/config":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            try:
                data = json.loads(body)

                # 保存 lead model
                if data.get("lead_model"):
                    lead = data["lead_model"]
                    self.config_store.set_lead_model(ModelEndpoint(**lead))

                # 保存 worker model
                if data.get("worker_model"):
                    worker = data["worker_model"]
                    self.config_store.set_worker_model(ModelEndpoint(**worker))

                self.send_json_response({"success": True})

            except Exception as e:
                self.send_error_response(400, str(e))

        elif parsed.path == "/api/agents/start":
            # 启动 Agent 的逻辑
            self.send_json_response({"success": True, "message": "Agents started"})

        elif parsed.path.startswith("/api/agents/") and parsed.path.endswith("/stop"):
            agent_id = parsed.path.split("/")[3]
            self.send_json_response({"success": True})

        elif parsed.path == "/api/agents/stop-all":
            self.send_json_response({"success": True})

        elif parsed.path == "/api/message":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            try:
                data = json.loads(body)
                agent_id = data.get("agent_id")
                message = data.get("message")

                if agent_id and message:
                    manager = get_global_manager()
                    if manager:
                        manager.send_message(agent_id, message)
                    self.send_json_response({"success": True})
                else:
                    self.send_error_response(400, "Missing agent_id or message")

            except Exception as e:
                self.send_error_response(400, str(e))

        else:
            self.send_error_response(404, "Not Found")

    def send_json_response(self, data: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_file_response(self, content_type: str, content: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(content.encode()))
        self.end_headers()
        self.wfile.write(content.encode())

    def send_error_response(self, code: int, message: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, format, *args):
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    allow_reuse_address = True


def run_server(port: int = 8787, host: str = "0.0.0.0"):
    """运行监控服务器"""

    # 获取配置
    config_store = get_config()
    monitor = AgentMonitor()

    # 设置处理器
    MonitorHTTPHandler.monitor = monitor
    MonitorHTTPHandler.config_store = config_store

    # 尝试初始化 SessionManager
    try:
        repo_path = os.getcwd()
        if os.path.exists(os.path.join(repo_path, ".git")):
            manager = init_global_manager(repo_path)
            monitor.set_session_manager(manager)
            print(f"Session manager initialized for repo: {repo_path}")
    except Exception as e:
        print(f"Warning: Could not initialize session manager: {e}")

    # 启动监控
    monitor.start()

    # 创建服务器
    server = ThreadingHTTPServer((host, port), MonitorHTTPHandler)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   🤖 MyMultiAgent Monitor                                   ║
║                                                              ║
║   Configuration UI:  http://localhost:{port}/setup          ║
║   Dashboard:        http://localhost:{port}/dashboard        ║
║                                                              ║
║   Press Ctrl+C to stop                                       ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        monitor.stop()
        server.shutdown()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MyMultiAgent Monitor Server")
    parser.add_argument("--port", type=int, default=8787, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    run_server(port=args.port, host=args.host)
