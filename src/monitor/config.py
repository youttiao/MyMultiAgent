"""
Config Store

简单的配置存储，用于 API Keys 和 Profiles
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class ModelEndpoint:
    """模型端点配置"""
    name: str
    api_key: str
    base_url: str
    model_name: str  # 实际模型名


@dataclass
class Config:
    """全局配置"""
    lead_model: Optional[ModelEndpoint] = None
    worker_model: Optional[ModelEndpoint] = None
    profiles: dict = None

    def __post_init__(self):
        if self.profiles is None:
            self.profiles = {}

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigStore:
    """
    配置存储

    将配置保存到 JSON 文件
    """

    def __init__(self, config_path: str = "./config.json"):
        self.config_path = Path(config_path).expanduser().resolve()
        self._config: Optional[Config] = None

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = self._load()
        return self._config

    def _load(self) -> Config:
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text())
                return Config(
                    lead_model=ModelEndpoint(**data["lead_model"]) if data.get("lead_model") else None,
                    worker_model=ModelEndpoint(**data["worker_model"]) if data.get("worker_model") else None,
                    profiles=data.get("profiles", {}),
                )
            except Exception:
                pass
        return Config()

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config.to_dict(), indent=2))

    def set_lead_model(self, endpoint: ModelEndpoint) -> None:
        self.config.lead_model = endpoint
        self.save()

    def set_worker_model(self, endpoint: ModelEndpoint) -> None:
        self.config.worker_model = endpoint
        self.save()

    def is_configured(self) -> bool:
        return self.config.lead_model is not None


# 全局配置实例
_global_config: Optional[ConfigStore] = None


def get_config(path: str = "./config.json") -> ConfigStore:
    global _global_config
    if _global_config is None:
        _global_config = ConfigStore(path)
    return _global_config
