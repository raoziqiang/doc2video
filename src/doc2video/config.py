"""配置加载:config/default.yaml + .env(密钥不进入配置指纹的对象化输出)。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .cache import canonical_json

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _resolve_paths(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg


def load_config(env: str = "default") -> dict[str, Any]:
    """加载 config/<env>.yaml;密钥从 .env 读取(不进配置对象)。"""
    load_dotenv(REPO_ROOT / ".env", override=False)
    cfg_path = CONFIG_DIR / f"{env}.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置不存在: {cfg_path}")
    with open(cfg_path, encoding="utf-8") as f:
        return _resolve_paths(yaml.safe_load(f))


def config_fingerprint(cfg: dict[str, Any]) -> str:
    """配置指纹(不含密钥);任一配置变更 → 下游指纹失效。"""
    body = canonical_json(cfg)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def workspace_dir(cfg: dict[str, Any]) -> Path:
    """workspace 根目录:环境变量 DOC2VIDEO_WORKSPACE 可覆盖(测试用)。"""
    env_override = os.environ.get("DOC2VIDEO_WORKSPACE")
    if env_override:
        return Path(env_override)
    return REPO_ROOT / cfg["pipeline"]["workspace"]


def env_key(name: str) -> str | None:
    return os.environ.get(name) or None
