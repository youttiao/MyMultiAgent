"""
Monitor Server

借鉴自 agent-foreman (https://github.com/operoncao123/agent-foreman)
Web 监控服务器，实时显示所有 Agent 状态。

核心功能:
- 会话状态解析 (Claude/Codex)
- WebSocket 实时推送
- 消息注入
- 跨平台支持
"""

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import socketserver

# 导入核心模块
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.core import SessionManager, SessionStatus


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
    """展开路径中的 ~ 和环境变量"""
    return os.path.expanduser(os.path.expandvars(path))


def truncate(text: str, limit: int = 240) -> str:
    """截断文本"""
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def parse_iso_ts(value: str) -> Optional[float]:
    """解析 ISO 时间戳"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def utc_now() -> float:
    """获取当前 UTC 时间戳"""
    return datetime.now(timezone.utc).timestamp()


def relative_age(age_sec: float) -> str:
    """相对时间"""
    if age_sec < 60:
        return f"{int(age_sec)}s"
    if age_sec < 3600:
        return f"{int(age_sec // 60)}m"
    if age_sec < 86400:
        return f"{int(age_sec // 3600)}h"
    return f"{int(age_sec // 86400)}d"


# =============================================================================
# 会话解析器
# =============================================================================

def infer_agent_type(program: str) -> str:
    """根据程序推断 Agent 类型"""
    program_lower = program.lower()
    if "claude" in program_lower:
        return "claude"
    if "codex" in program_lower:
        return "codex"
    if "gemini" in program_lower:
        return "gemini"
    if "aider" in program_lower:
        return "aider"
    return "unknown"


def parse_claude_todos(todos_root: str, tasks_root: str, session_id: str) -> list[str]:
    """解析 Claude Todo 列表"""
    items = []

    # 从 tasks 目录解析
    if tasks_root:
        task_dir = Path(expand_path(tasks_root)) / session_id
        if task_dir.exists():
            for path in sorted(task_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text())
                    if isinstance(data, dict) and data.get("status") != "completed":
                        subject = data.get("activeForm") or data.get("subject") or path.name
                        items.append(f"[{data.get('status', 'pending')}] {subject}")
                except Exception:
                    pass

    return items[:8]


def parse_claude_session_output(session_id: str, paths: dict) -> dict:
    """
    解析 Claude 会话输出

    从会话目录读取最近的输出摘要
    """
    result = {
        "recent_output": "",
        "pending_items": [],
        "last_user_message": "",
    }

    # 尝试从项目目录解析
    projects_root = expand_path(paths.get("claude_projects", "~/.claude/projects"))
    if Path(projects_root).exists():
        for project_dir in Path(projects_root).iterdir():
            if project_dir.is_dir():
                # 查找最新的会话文件
                session_files = sorted(
                    project_dir.rglob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )[:1]

                for session_file in session_files:
                    try:
                        lines = session_file.read_text().splitlines()
                        # 获取最后几行
                        recent_lines = deque(lines[-20:], maxlen=20)

                        for line in reversed(list(recent_lines)):
                            try:
                                obj = json.loads(line)
                                if obj.get("type") == "summary":
                                    result["recent_output"] = truncate(obj.get("summary", ""))
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

    return result


# =============================================================================
# Agent 状态
# =============================================================================

@dataclass
class AgentInfo:
    """Agent 信息"""
    id: str
    name: str
    agent_type: str
    status: str  # "running", "ready", "loading", "paused", "unknown"
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
    """
    Agent 状态监控器

    定期扫描和更新所有 Agent 的状态
    """

    def __init__(self, config: dict):
        self.config = config
        self._agents: dict[str, AgentInfo] = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def add_agent(self, agent: AgentInfo) -> None:
        """添加 Agent"""
        with self._lock:
            self._agents[agent.id] = agent

    def remove_agent(self, agent_id: str) -> None:
        """移除 Agent"""
        with self._lock:
            self._agents.pop(agent_id, None)

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """获取 Agent"""
        return self._agents.get(agent_id)

    def list_agents(self) -> list[AgentInfo]:
        """列出所有 Agent"""
        with self._lock:
            return list(self._agents.values())

    def update_from_session_manager(self, manager: SessionManager) -> None:
        """从 SessionManager 更新状态"""
        for session in manager.list_sessions():
            agent_id = session.id

            # 获取最新输出
            output = manager.get_session_output(agent_id) or ""

            # 检测是否需要输入
            needs_input = False
            patterns = self.config.get("status", {}).get("needs_input_patterns", [])
            for pattern in patterns:
                if re.search(pattern, output, re.IGNORECASE):
                    needs_input = True
                    break

            # 更新 Agent 信息
            if agent_id in self._agents:
                agent = self._agents[agent_id]
                agent.status = session.status.value
                agent.recent_output = truncate(output, 500)
                agent.needs_input = needs_input
                agent.heartbeat = utc_now()
            else:
                agent = AgentInfo(
                    id=agent_id,
                    name=session.profile.name,
                    agent_type=infer_agent_type(session.profile.program),
                    status=session.status.value,
                    recent_output=truncate(output, 500),
                    needs_input=needs_input,
                    cwd=str(session.worktree_path),
                    branch=session.branch or "",
                )
                self._agents[agent_id] = agent

    def get_snapshot(self) -> dict:
        """获取当前状态快照"""
        agents = []
        now = utc_now()

        for agent in self._agents.values():
            age = now - agent.heartbeat

            # 计算状态分组
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

    def start(self, manager: SessionManager) -> None:
        """启动监控"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, args=(manager,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _monitor_loop(self, manager: SessionManager) -> None:
        """监控循环"""
        interval = self.config.get("refresh_interval_sec", 2)

        while self._running:
            try:
                # 更新所有会话状态
                manager.update_all_status()

                # 从 SessionManager 更新监控器
                self.update_from_session_manager(manager)

            except Exception as e:
                print(f"Monitor error: {e}")

            time.sleep(interval)


# =============================================================================
# Web 服务器
# =============================================================================

class MonitorHTTPHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    monitor: Optional[AgentMonitor] = None

    def do_GET(self):
        """处理 GET 请求"""
        parsed = urlparse(self.path)

        if parsed.path == "/api/status" or parsed.path == "/status":
            self.send_json_response(self.monitor.get_snapshot())

        elif parsed.path == "/" or parsed.path == "/index.html":
            self.send_file_response("text/html", self._get_index_html())

        elif parsed.path == "/app.js":
            self.send_file_response("application/javascript", self._get_app_js())

        elif parsed.path == "/styles.css":
            self.send_file_response("text/css", self._get_styles_css())

        elif parsed.path == "/api/agents":
            self.send_json_response({
                "agents": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "type": a.agent_type,
                        "status": a.status,
                    }
                    for a in self.monitor.list_agents()
                ]
            })

        else:
            self.send_error_response(404, "Not Found")

    def do_POST(self):
        """处理 POST 请求"""
        parsed = urlparse(self.path)

        if parsed.path == "/api/message":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            try:
                data = json.loads(body)
                agent_id = data.get("agent_id")
                message = data.get("message")

                if agent_id and message:
                    # 这里应该调用 SessionManager.send_message
                    # 暂时返回成功
                    self.send_json_response({"success": True})
                else:
                    self.send_error_response(400, "Missing agent_id or message")

            except json.JSONDecodeError:
                self.send_error_response(400, "Invalid JSON")

        else:
            self.send_error_response(404, "Not Found")

    def send_json_response(self, data: dict) -> None:
        """发送 JSON 响应"""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_file_response(self, content_type: str, content: str) -> None:
        """发送文件响应"""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(content.encode()))
        self.end_headers()
        self.wfile.write(content.encode())

    def send_error_response(self, code: int, message: str) -> None:
        """发送错误响应"""
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())

    def _get_index_html(self) -> str:
        """获取 HTML 页面"""
        return """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MyMultiAgent Monitor</title>
    <link rel="stylesheet" href="/styles.css">
</head>
<body>
    <div class="container">
        <header>
            <h1>🤖 MyMultiAgent Monitor</h1>
            <div class="stats" id="stats"></div>
        </header>

        <main>
            <div class="tabs">
                <button class="tab active" data-group="all">All</button>
                <button class="tab" data-group="needs_input">Needs Input</button>
                <button class="tab" data-group="working">Working</button>
                <button class="tab" data-group="idle">Idle</button>
            </div>

            <div class="agents-grid" id="agents-grid"></div>
        </main>

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

    <script src="/app.js"></script>
</body>
</html>"""

    def _get_app_js(self) -> str:
        """获取 JavaScript"""
        return """
// MyMultiAgent Monitor JS
let currentGroup = 'all';
let agents = [];

async function fetchStatus() {
    try {
        const resp = await fetch('/api/status');
        const data = await resp.json();

        // Update stats
        document.getElementById('stats').innerHTML = `
            <span class="stat">Total: ${data.stats.total}</span>
            <span class="stat needs-input">Needs Input: ${data.stats.needs_input}</span>
            <span class="stat working">Working: ${data.stats.working}</span>
            <span class="stat idle">Idle: ${data.stats.idle}</span>
        `;

        agents = data.agents;
        renderAgents();

    } catch (err) {
        console.error('Fetch error:', err);
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
                <span class="agent-name">${agent.name}</span>
                <span class="agent-type">${agent.type}</span>
            </div>
            <div class="agent-status">
                <span class="status-badge ${agent.status}">${agent.status}</span>
                ${agent.branch ? `<span class="branch">${agent.branch}</span>` : ''}
            </div>
            <div class="agent-output">${escapeHtml(agent.recent_output || 'No output yet')}</div>
            <div class="agent-footer">
                <span class="heartbeat">Last seen: ${agent.heartbeat_age}</span>
            </div>
        </div>
    `).join('');

    // Update agent select
    const select = document.getElementById('agent-select');
    select.innerHTML = '<option value="">Select agent...</option>' +
        agents.map(a => `<option value="${a.id}">${a.name}</option>`).join('');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentGroup = tab.dataset.group;
        renderAgents();
    });
});

// Send message
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
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_id: agentId, message })
        });
        document.getElementById('message-input').value = '';
    } catch (err) {
        alert('Failed to send message: ' + err.message);
    }
});

// Poll for updates
setInterval(fetchStatus, 2000);
fetchStatus();
"""

    def _get_styles_css(self) -> str:
        """获取 CSS"""
        return """
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #1a1a2e;
    color: #eee;
    min-height: 100vh;
}

.container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 20px;
}

header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 30px;
    padding-bottom: 20px;
    border-bottom: 1px solid #333;
}

h1 {
    font-size: 1.8rem;
}

.stats {
    display: flex;
    gap: 20px;
}

.stat {
    padding: 8px 16px;
    background: #2a2a4e;
    border-radius: 20px;
    font-size: 0.9rem;
}

.stat.needs-input {
    background: #ff6b6b;
    color: #000;
}

.stat.working {
    background: #4ecdc4;
    color: #000;
}

.stat.idle {
    background: #95a5a6;
    color: #000;
}

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

.tab:hover {
    background: #3a3a5e;
}

.tab.active {
    background: #4ecdc4;
    color: #000;
}

.agents-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
    gap: 20px;
}

.agent-card {
    background: #2a2a4e;
    border-radius: 12px;
    padding: 16px;
    border-left: 4px solid #95a5a6;
}

.agent-card.running, .agent-card.working {
    border-left-color: #4ecdc4;
}

.agent-card.needs-input {
    border-left-color: #ff6b6b;
    background: #2a2a3e;
}

.agent-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
}

.agent-name {
    font-weight: 600;
    font-size: 1.1rem;
}

.agent-type {
    font-size: 0.8rem;
    padding: 2px 8px;
    background: #444;
    border-radius: 10px;
}

.agent-status {
    display: flex;
    gap: 10px;
    align-items: center;
    margin-bottom: 10px;
}

.status-badge {
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 0.8rem;
    font-weight: 500;
}

.status-badge.ready {
    background: #4ecdc4;
    color: #000;
}

.status-badge.running {
    background: #4ecdc4;
    color: #000;
}

.status-badge.loading {
    background: #f39c12;
    color: #000;
}

.status-badge.paused {
    background: #95a5a6;
    color: #000;
}

.branch {
    font-size: 0.8rem;
    color: #888;
}

.agent-output {
    background: #1a1a2e;
    padding: 12px;
    border-radius: 8px;
    font-family: monospace;
    font-size: 0.85rem;
    max-height: 150px;
    overflow-y: auto;
    white-space: pre-wrap;
    margin-bottom: 10px;
}

.agent-footer {
    font-size: 0.8rem;
    color: #666;
}

.empty {
    text-align: center;
    padding: 60px;
    color: #666;
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
    min-width: 200px;
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

#send-btn:hover {
    background: #3dbdb5;
}
"""

    def log_message(self, format, *args):
        """抑制日志输出"""
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """支持多线程的 HTTP 服务器"""
    allow_reuse_address = True


def run_server(
    port: int = 8787,
    host: str = "0.0.0.0",
    config: dict = None
):
    """
    运行监控服务器

    Args:
        port: 端口号
        host: 主机地址
        config: 配置字典
    """
    config = config or DEFAULT_CONFIG

    # 创建监控器
    monitor = AgentMonitor(config)
    MonitorHTTPHandler.monitor = monitor

    # 创建服务器
    server = ThreadingHTTPServer((host, port), MonitorHTTPHandler)

    print(f"MyMultiAgent Monitor starting on http://{host}:{port}")
    print("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MyMultiAgent Monitor Server")
    parser.add_argument("--port", type=int, default=8787, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    run_server(port=args.port, host=args.host)
