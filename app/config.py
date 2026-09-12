# -*- coding: utf-8 -*-
"""
IfWe 配置加载器（统一入口）

优先级：环境变量覆盖 > config.yaml > config.example.yaml > 内置默认值
- 所有模块经此读取配置，禁止在业务代码里硬编码个人数据（昵称/日期/路径）
- 密钥只从环境变量读取，绝不落盘
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# ---- 内置默认值（与 config.example.yaml 一致） ----
_DEFAULTS: dict[str, Any] = {
    "people": {
        "A": {"key": "A", "display": "你", "name": ""},
        "B": {"key": "B", "display": "TA", "name": ""},
    },
    "chat": {
        "source": "data/raw/chat.jsonl",
        "format": "chatlab",
        "timezone": "Asia/Shanghai",
        "session_gap_minutes": 30,
    },
    "llm": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key_env": "LLM_API_KEY",
        "base_url": "https://api.deepseek.com",
        "max_tokens": 8192,
    },
    "media": {"emojis_dir": ""},
    "privacy": {"sanitize": "medium", "allow_llm_send": True},
    "defaults": {
        "start_after_last": True,
        "auto_day_turns": 20,
        "seed": 42,
        "port": 8015,
    },
    "paths": {
        "data_dir": "data",
        "persona_dir": "",     # 留空 = data_dir/persona（随 IFWE_DATA_DIR 一起切换）
        "cache_dir": "",       # 留空 = data_dir/cache
    },
}

# 环境变量覆盖表：环境变量名 -> (配置节, 配置键)
_ENV_OVERRIDES = {
    "IFWE_DATA_DIR": ("paths", "data_dir"),
    "IFWE_CHAT_SOURCE": ("chat", "source"),
    "IFWE_LLM_MODEL": ("llm", "model"),
    "IFWE_LLM_BASE_URL": ("llm", "base_url"),
    "IFWE_LLM_API_KEY_ENV": ("llm", "api_key_env"),
    "IFWE_MEDIA_DIR": ("media", "emojis_dir"),
    "IFWE_PORT": ("defaults", "port"),
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml_file(path: Path) -> dict:
    import yaml  # 延迟导入：仅在存在配置文件时需要 PyYAML
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _find_config_file() -> Path | None:
    for name in ("config.yaml", "config.example.yaml"):
        p = ROOT / name
        if p.is_file():
            return p
    return None


def load() -> dict:
    """加载配置（每次调用重读，成本可忽略；方便运行期改配置）"""
    cfg = _DEFAULTS
    f = _find_config_file()
    if f is not None:
        try:
            cfg = _deep_merge(cfg, _load_yaml_file(f))
        except Exception as e:  # 配置文件损坏时退回默认值，不让服务起不来
            print(f"[config] 读取 {f.name} 失败（{e}），使用内置默认值")
    for env, (sec, key) in _ENV_OVERRIDES.items():
        v = os.environ.get(env)
        if v:
            try:
                v = int(v) if isinstance(cfg[sec][key], int) else v
            except (ValueError, TypeError, KeyError):
                pass
            cfg.setdefault(sec, {})[key] = v
    return cfg


def root() -> Path:
    return ROOT


def data_dir() -> Path:
    p = Path(load()["paths"]["data_dir"])
    return p if p.is_absolute() else ROOT / p


def persona_dir() -> Path:
    v = load()["paths"]["persona_dir"]
    p = Path(v) if v else data_dir() / "persona"
    return p if p.is_absolute() else ROOT / p


def cache_dir() -> Path:
    v = load()["paths"]["cache_dir"]
    p = Path(v) if v else data_dir() / "cache"
    return p if p.is_absolute() else ROOT / p


def db_path() -> Path:
    return data_dir() / "ifwe_v1.db"


def emojis_dir() -> Path | None:
    v = load()["media"]["emojis_dir"]
    if not v:
        return None
    p = Path(v)
    return p if p.is_absolute() else ROOT / p


def api_key() -> str | None:
    """按配置读取 LLM 密钥；兼容旧环境变量名 DEEPSEEK_API_KEY"""
    env_name = load()["llm"]["api_key_env"] or "LLM_API_KEY"
    return os.environ.get(env_name) or os.environ.get("DEEPSEEK_API_KEY")


def sender_names() -> dict[str, str]:
    """A/B -> 界面显示名（空则回退代号）；内部逻辑一律用 A/B"""
    people = load()["people"]
    out = {}
    for k in ("A", "B"):
        disp = (people.get(k, {}) or {}).get("display") or ""
        out[k] = disp.strip() or k
    return out


def person_display_name(person: str) -> str:
    """persona 展示名：优先 people.{key}.name，缺省用 display，最后用代号"""
    people = load()["people"]
    p = people.get(person, {}) or {}
    return (p.get("name") or p.get("display") or person).strip() or person
