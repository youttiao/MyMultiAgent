"""
Agent Profiles

定义和管理 Agent 配置，包括模型、技能和工作目录。
"""

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ModelConfig:
    """模型配置"""
    name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    extra_args: dict = field(default_factory=dict)


@dataclass
class AgentProfile:
    """
    Agent Profile 定义

    描述一个 Agent 的完整配置
    """
    name: str
    role: str  # "lead", "coder", "reviewer", etc.
    program: str
    model: str
    model_config: Optional[ModelConfig] = None
    skills: list[str] = field(default_factory=list)
    claude_md: Optional[str] = None  # 自定义 CLAUDE.md 内容
    work_dir: Optional[str] = None
    env: dict = field(default_factory=dict)
    auto_yes: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "AgentProfile":
        """从字典创建"""
        model_config = None
        if "model_config" in data:
            mc = data["model_config"]
            model_config = ModelConfig(
                name=data.get("model", ""),
                api_key=mc.get("api_key"),
                base_url=mc.get("base_url"),
                extra_args=mc.get("extra_args", {})
            )

        return cls(
            name=data["name"],
            role=data.get("role", "worker"),
            program=data.get("program", "claude"),
            model=data.get("model", "inherit"),
            model_config=model_config,
            skills=data.get("skills", []),
            claude_md=data.get("claude_md"),
            work_dir=data.get("work_dir"),
            env=data.get("env", {}),
            auto_yes=data.get("auto_yes", False),
        )

    def to_dict(self) -> dict:
        """转换为字典"""
        result = {
            "name": self.name,
            "role": self.role,
            "program": self.program,
            "model": self.model,
            "skills": self.skills,
            "env": self.env,
            "auto_yes": self.auto_yes,
        }

        if self.claude_md:
            result["claude_md"] = self.claude_md
        if self.work_dir:
            result["work_dir"] = self.work_dir
        if self.model_config:
            result["model_config"] = {
                k: v for k, v in {
                    "api_key": self.model_config.api_key,
                    "base_url": self.model_config.base_url,
                    "extra_args": self.model_config.extra_args,
                }.items() if v
            }

        return result


class ProfileManager:
    """
    Profile 管理器

    加载和管理所有 Agent Profile
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self._profiles: dict[str, AgentProfile] = {}
        self._lead_profile: Optional[AgentProfile] = None

        if config_path and Path(config_path).exists():
            self.load_from_file(config_path)

    def load_from_file(self, path: str) -> None:
        """从 YAML 文件加载"""
        path = os.path.expanduser(path)
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        if not data:
            return

        # 加载 profiles
        profiles = data.get("profiles", {})
        for name, profile_data in profiles.items():
            profile = AgentProfile.from_dict(profile_data)
            self._profiles[name] = profile

            if profile_data.get("is_lead"):
                self._lead_profile = profile

        # 如果没有标记 lead，取第一个
        if not self._lead_profile and self._profiles:
            self._lead_profile = next(iter(self._profiles.values()))

    def get_profile(self, name: str) -> Optional[AgentProfile]:
        """获取 Profile"""
        return self._profiles.get(name)

    def list_profiles(self) -> list[AgentProfile]:
        """列出所有 Profile"""
        return list(self._profiles.values())

    def get_lead_profile(self) -> Optional[AgentProfile]:
        """获取 Lead Profile"""
        return self._lead_profile

    def add_profile(self, name: str, profile: AgentProfile) -> None:
        """添加 Profile"""
        self._profiles[name] = profile

    def remove_profile(self, name: str) -> bool:
        """移除 Profile"""
        return self._profiles.pop(name, None) is not None

    def save_to_file(self, path: Optional[str] = None) -> None:
        """保存到文件"""
        path = path or self.config_path
        if not path:
            raise ValueError("No config path specified")

        path = os.path.expanduser(path)
        data = {
            "profiles": {
                name: profile.to_dict()
                for name, profile in self._profiles.items()
            }
        }

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)


# =============================================================================
# 默认 Profile 配置
# =============================================================================

DEFAULT_PROFILES_YAML = """
# MyMultiAgent Profile Configuration
#
# 定义多智能体系统的 Agent 配置

profiles:
  lead:
    name: "Lead Agent"
    role: lead
    program: claude
    model: minimax
    model_config:
      # 从环境变量读取 API Key
      api_key: "${MINIMAX_API_KEY}"
      base_url: "https://api.minimax.chat"
    skills:
      - orchestration
      - planning
      - coordination
    is_lead: true

  frontend:
    name: "Frontend Developer"
    role: coder
    program: claude
    model: haiku
    skills:
      - ui-design
      - react
      - css
      - tailwind
    work_dir: ./workspace/frontend

  backend:
    name: "Backend Developer"
    role: coder
    program: claude
    model: haiku
    skills:
      - api-design
      - database
      - python
      - security
    work_dir: ./workspace/backend

  reviewer:
    name: "Code Reviewer"
    role: reviewer
    program: claude
    model: sonnet
    skills:
      - code-review
      - security
      - performance
    work_dir: ./workspace/review

  researcher:
    name: "Research Agent"
    role: researcher
    program: claude
    model: haiku
    skills:
      - research
      - documentation
      - analysis
"""


def create_default_profiles(path: str = "./profiles.yaml") -> None:
    """创建默认 Profile 配置文件"""
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    with open(path, "w") as f:
        f.write(DEFAULT_PROFILES_YAML)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MyMultiAgent Profile Manager")
    parser.add_argument("--init", action="store_true", help="Create default profiles.yaml")
    parser.add_argument("--path", type=str, default="./profiles.yaml", help="Profile file path")
    args = parser.parse_args()

    if args.init:
        create_default_profiles(args.path)
        print(f"Created default profiles at {args.path}")
