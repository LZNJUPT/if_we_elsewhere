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


def persist_import_config(source: str, smap: dict[str, str]) -> bool:
    """导入成功后把 chat.source 与 people.A/B.match 回填进 config.yaml。

    smap 是 {原始账号名: "A"/"B"}；source 相对/绝对路径均可（相对路径统一 / 分隔）。
    优先对文件文本做最小替换（保留注释与手改内容）；替换后校验不通过（结构对不上）
    再退回「YAML 解析 → 更新字段 → 整体重写」兜底（值保证正确，注释会丢失）。
    返回 True 表示文件已更新；False 表示无需更新或回填失败（不影响导入结果）。
    """
    import json
    import re
    import yaml  # 延迟导入：仅在存在配置文件时需要 PyYAML

    cfg_path = ROOT / "config.yaml"
    if not cfg_path.is_file():
        return False
    orig_text = cfg_path.read_text(encoding="utf-8")
    inv = {v: (k or "").strip() for k, v in smap.items()}      # A/B -> 原始账号名

    def _values_ok(d: dict) -> bool:
        if source and ((d.get("chat") or {}).get("source") or "") != source:
            return False
        people = d.get("people") or {}
        for key in ("A", "B"):
            name = inv.get(key, "")
            if name and ((people.get(key) or {}).get("match") or "") != name:
                return False
        return True

    try:    # 值本来就对：直接跳过，不做任何写入
        current = yaml.safe_load(orig_text)
        if isinstance(current, dict) and _values_ok(current):
            return False
    except Exception:
        pass

    def _scalar(v: str) -> str:
        # JSON 字符串语法是 YAML 双引号标量的子集，可安全承载任意账号名/路径
        return json.dumps(v, ensure_ascii=False)

    text = orig_text
    for key in ("A", "B"):
        name = inv.get(key, "")
        if not name:
            continue
        text = re.sub(
            rf"(?m)^([ \t]*{key}:[^\n{{}}]*\{{[^\n{{}}]*?match:[ \t]*)"
            rf"(\"[^\"\n]*\"|'[^'\n]*'|[^,{{}}\n]+)",
            lambda m: m.group(1) + _scalar(name), text, count=1)
    if source:
        text = re.sub(
            r"(?m)^([ \t]*source:[ \t]*)(\"[^\"\n]*\"|'[^'\n]*'|[^,#\n]+)",
            lambda m: m.group(1) + _scalar(source), text, count=1)

    try:
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict) and _values_ok(parsed):
            if text != orig_text:
                cfg_path.write_text(text, encoding="utf-8")
                return True
            return False                                   # 值本来就对，无需写
    except Exception:
        pass

    # 兜底：模板结构对不上/解析失败 → 解析原文件整体重写（注释会丢失）
    try:
        data = yaml.safe_load(orig_text)
        if not isinstance(data, dict):
            raise ValueError("config.yaml 顶层不是键值映射")
    except Exception as e:
        print(f"[config] config.yaml 无法解析（{e}），跳过回填；"
              "请手动填写 chat.source 与 people.A.match / people.B.match")
        return False
    if source:
        data.setdefault("chat", {})["source"] = source
    people = data.setdefault("people", {})
    for key in ("A", "B"):
        name = inv.get(key, "")
        if name and isinstance(people.get(key), dict):
            people[key]["match"] = name
    cfg_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")
    return True


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
