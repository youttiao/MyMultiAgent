"""
CLI Interface

简单的命令行界面用于管理多 Agent 系统
"""

import sys
import argparse
from pathlib import Path

# 添加 src 目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core import (
    init_global_manager,
    get_global_manager,
    SessionManager,
    AgentProfile,
)
from src.agents import ProfileManager, create_default_profiles
from src.monitor import run_server


def cmd_init(args):
    """初始化项目"""
    # 创建默认配置
    create_default_profiles(args.profiles)
    print(f"Created default profiles at {args.profiles}")

    # 创建工作目录
    Path("./workspace").mkdir(exist_ok=True)
    Path("./workspace/frontend").mkdir(exist_ok=True)
    Path("./workspace/backend").mkdir(exist_ok=True)
    Path("./workspace/review").mkdir(exist_ok=True)
    print("Created workspace directories")


def cmd_list(args):
    """列出所有 Agent"""
    manager = get_global_manager()
    if not manager:
        print("Session manager not initialized. Run 'start' first.")
        return

    sessions = manager.list_sessions()
    if not sessions:
        print("No active sessions")
        return

    print(f"\nActive Sessions ({len(sessions)}):")
    print("-" * 60)
    for session in sessions:
        print(f"  [{session.status.value}] {session.id}")
        print(f"    Profile: {session.profile.name}")
        print(f"    Program: {session.profile.program}")
        print(f"    Branch: {session.branch}")
        print(f"    Worktree: {session.worktree_path}")
        print()


def cmd_start(args):
    """启动 Agent"""
    manager = get_global_manager()
    if not manager:
        print("Initializing session manager...")
        manager = init_global_manager(args.repo)

    # 加载 profiles
    profile_manager = ProfileManager(args.profiles)

    # 启动指定的 profiles
    for profile_name in args.profiles_to_start:
        profile = profile_manager.get_profile(profile_name)
        if not profile:
            print(f"Profile not found: {profile_name}")
            continue

        print(f"Starting agent: {profile.name}")
        session = manager.create_agent_session(
            profile=profile,
            initial_prompt=args.prompt
        )
        print(f"  Created session: {session.id}")
        print(f"  Branch: {session.branch}")
        print(f"  Worktree: {session.worktree_path}")


def cmd_stop(args):
    """停止 Agent"""
    manager = get_global_manager()
    if not manager:
        print("Session manager not initialized.")
        return

    for name in args.agents:
        if manager.close_session(name):
            print(f"Stopped agent: {name}")
        else:
            print(f"Agent not found: {name}")


def cmd_monitor(args):
    """启动监控服务器"""
    print(f"Starting monitor on http://{args.host}:{args.port}")
    run_server(port=args.port, host=args.host)


def main():
    parser = argparse.ArgumentParser(
        description="MyMultiAgent - Multi-Agent Management System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize project")
    init_parser.add_argument(
        "--profiles", default="./profiles.yaml", help="Profile config path"
    )

    # list
    list_parser = subparsers.add_parser("list", help="List all agents")

    # start
    start_parser = subparsers.add_parser("start", help="Start agents")
    start_parser.add_argument(
        "--repo", default=".", help="Git repository path"
    )
    start_parser.add_argument(
        "--profiles", default="./profiles.yaml", help="Profile config path"
    )
    start_parser.add_argument(
        "profiles_to_start", nargs="*", default=["lead"],
        help="Profiles to start"
    )
    start_parser.add_argument(
        "--prompt", help="Initial prompt"
    )

    # stop
    stop_parser = subparsers.add_parser("stop", help="Stop agents")
    stop_parser.add_argument("agents", nargs="+", help="Agent names to stop")

    # monitor
    monitor_parser = subparsers.add_parser("monitor", help="Start monitor server")
    monitor_parser.add_argument("--port", type=int, default=8787, help="Port")
    monitor_parser.add_argument("--host", default="0.0.0.0", help="Host")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "monitor":
        cmd_monitor(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
