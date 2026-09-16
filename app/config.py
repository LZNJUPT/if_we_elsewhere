# -*- coding: utf-8 -*-
"""
IfWe 配置加载器（统一入口）

优先级：profile.yaml（当前好友）> 环境变量覆盖 > config.yaml > config.example.yaml > 内置默认值
- 所有模块经此读取配置，禁止在业务代码里硬编码个人数据（昵称/日期/路径）
- 密钥只从环境变量或系统凭据管理器读取（见 secret_store），绝不落盘明文

多好友（v0.3）：每个好友 = 一个独立数据目录（`data/profiles/<id>/`）。
`set_active_profile(id)` 之后 data_dir()/persona_dir()/cache_dir() 全部随之切换，
与既有的 IFWE_DATA_DIR 机制同效果（本模块不做任何表结构改动）。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _resolve_root() -> Path:
    """项目根：源码运行 = 仓库根；PyInstaller 冻结 = exe 所在目录（data/ 始终相对 exe）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _resolve_root()


class _NullStream:
    """无任何标准句柄（双击启动的窗口程序）时的内存黑洞。

    必须长得像个流：uvicorn 的日志 formatter 会调 sys.stdout.isatty()，
    logging.StreamHandler 会 write/flush——缺一个就崩。
    """

    encoding = "utf-8"
    errors = "replace"
    name = "<ifwe-null>"
    mode = "w"
    closed = False

    def write(self, *_args, **_kwargs):
        return 0

    def writelines(self, *_args, **_kwargs):
        pass

    def flush(self):
        pass

    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _make_sink():
    """冻结 + 窗口模式（双击启动）时的日志去向。

    优先写 `data/logs/desktop.log`（用户反馈问题时可直接附带）；打不开就退回内存黑洞。
    只建 data/logs/，不碰 data/profiles/<id>，因此不会干扰首次迁移。
    """
    try:
        logs = base_data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "desktop.log"
        mode = "w" if path.exists() and path.stat().st_size > 512 * 1024 else "a"
        f = open(path, mode, encoding="utf-8", errors="replace", buffering=1)
        f.write(f"\n===== IfWe {version()} 启动（pid {os.getpid()}）=====\n")
        return f
    except Exception:
        return _NullStream()


def _safe_stdio() -> None:
    """把标准流整理成「永远可用」的状态（幂等，可反复调用）。

    两个真实场景，缺一不可：
    1. 输出被重定向到非 UTF-8 编码的管道/文件（CI runner cp1252、中文系统 cp936）时，
       打印中文会 UnicodeEncodeError，甚至让 FastAPI lifespan 启动失败 → 固定为 UTF-8+replace；
    2. 双击启动的窗口程序没有任何标准句柄，sys.stdout/stderr 是 None →
       uvicorn 日志配置调 sys.stdout.isatty() 直接 AttributeError（2026-09-13 用户实测）→ 装安全接收端。
    交互式控制台在 Windows 上本就走 PEP 528 的 UTF-8 通道，reconfigure 等价于无操作。
    """
    if sys.stdout is None:
        sys.stdout = _make_sink()
    if sys.stderr is None:
        sys.stderr = _make_sink()
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

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
        # 人格保真（persona_fidelity）：F1 回复意愿 / F2 真实原文窗口。
        # 总开关：环境变量 IFWE_PERSONA_FIDELITY=0 可整体关闭（回退改动前行为）。
        "reply_willingness": {
            "enabled": True,
            "windows_days": [7, 14],   # 回复率统计窗口（两窗口取均值）
            "fallback": 0.5,           # 样本不足时的中性值
            "min_samples": 10,         # 条件化意愿层生效所需的最小样本数
        },
        "inject_real_window": True,    # F2（受 privacy.allow_llm_send 门禁）
        "window_turns": 20,            # F2 注入的真实对话条数
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
    """加载配置（每次调用重读，成本可忽略；方便运行期改配置）。

    合并顺序：内置默认 → config.yaml/example → 环境变量 → 当前好友的 profile.yaml。
    好友级配置只覆盖它写了的字段（表情包目录、chat.source、显示名等），
    全局配置（LLM 密钥/端口等）仍以 config.yaml 为准。
    """
    cfg = _global_cfg()
    pf = _active_profile_cfg()
    if pf:
        cfg = _deep_merge(cfg, pf)
    return cfg


_CFG_CACHE: dict[str, Any] = {"key": None, "cfg": {}}


def _global_cfg() -> dict:
    """不含好友级配置的全局配置（data_dir 解析必须以它为准，避免自指）。

    带 mtime 缓存：配置未变时免去重复的 YAML 解析（每个请求会调用很多次）。
    """
    f = _find_config_file()
    env_sig = tuple((e, os.environ.get(e) or "") for e in _ENV_OVERRIDES)
    try:
        mtime = f.stat().st_mtime if f is not None else -1.0
    except OSError:
        mtime = -1.0
    key = (str(f) if f else "", mtime, env_sig)
    if _CFG_CACHE["key"] == key:
        return copy.deepcopy(_CFG_CACHE["cfg"])

    cfg = copy.deepcopy(_DEFAULTS)
    if f is not None:
        try:
            cfg = _deep_merge(cfg, _load_yaml_file(f))
        except Exception as e:  # 配置文件损坏时退回默认值，不让服务起不来
            print(f"[config] 读取 {f.name} 失败（{e}），使用内置默认值")
    for env, (sec, key_) in _ENV_OVERRIDES.items():
        v = os.environ.get(env)
        if v:
            try:
                v = int(v) if isinstance(cfg[sec][key_], int) else v
            except (ValueError, TypeError, KeyError):
                pass
            cfg.setdefault(sec, {})[key_] = v
    _CFG_CACHE.update({"key": key, "cfg": cfg})
    return copy.deepcopy(cfg)


# ---------------------------------------------------------------- 多好友 profile
ACTIVE_PROFILE_ENV = "IFWE_PROFILE"
PROFILE_ID_RE_STR = r"^[a-z0-9][a-z0-9_-]{0,31}$"

# 进程内当前好友（id 为空 = 未启用 profile，按全局 paths.data_dir 走）
_ACTIVE: dict[str, str] = {"id": "", "data_dir": ""}
_REG_CACHE: dict[str, Any] = {"path": None, "mtime": -1.0, "items": []}
_PF_CACHE: dict[str, Any] = {"path": None, "mtime": -1.0, "data": {}}


def profiles_base() -> Path:
    """好友数据目录的父目录（相对「基础数据目录」，即 data/profiles）"""
    return base_data_dir() / "profiles"


def profiles_registry_path() -> Path:
    return base_data_dir() / "profiles.json"


def read_registry() -> list[dict]:
    """读好友注册表（mtime 缓存；文件缺失或损坏返回空列表，绝不抛异常）"""
    p = profiles_registry_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _REG_CACHE.update({"path": str(p), "mtime": -1.0, "items": []})
        return []
    if _REG_CACHE["path"] == str(p) and _REG_CACHE["mtime"] == mtime:
        return list(_REG_CACHE["items"])
    items: list[dict] = []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):                 # 兼容 {profiles:[...]} 写法
            raw = raw.get("profiles") or []
        if isinstance(raw, list):
            items = [it for it in raw if isinstance(it, dict) and it.get("id")]
    except Exception:
        items = []
    _REG_CACHE.update({"path": str(p), "mtime": mtime, "items": items})
    return list(items)


def write_registry(items: list[dict]) -> None:
    p = profiles_registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    _REG_CACHE.update({"path": str(p), "mtime": -1.0, "items": list(items)})


def active_profile() -> str:
    """当前好友 id（显式 set 优先，其次环境变量 IFWE_PROFILE；空串 = 未启用）"""
    return _ACTIVE.get("id") or os.environ.get(ACTIVE_PROFILE_ENV, "") or ""


def profile_entry(pid: str) -> dict | None:
    for it in read_registry():
        if it.get("id") == pid:
            return it
    return None


def profile_data_dir(pid: str) -> Path | None:
    """注册表里的好友数据目录（相对路径按基础数据目录解析）"""
    it = profile_entry(pid)
    if not it:
        return None
    v = (it.get("data_dir") or "").strip() or f"profiles/{pid}"
    p = Path(v)
    return p if p.is_absolute() else (base_data_dir() / p)


def set_active_profile(pid: str, data_dir: str | Path | None = None) -> str:
    """切换当前好友：data_dir/persona_dir/cache_dir 随之切换（同 IFWE_DATA_DIR 效果）"""
    if not pid:
        _ACTIVE.update({"id": "", "data_dir": ""})
        return ""
    d = Path(data_dir) if data_dir else profile_data_dir(pid)
    _ACTIVE.update({"id": pid, "data_dir": str(d) if d else ""})
    return pid


def _active_profile_cfg() -> dict:
    """当前好友的 profile.yaml（好友目录内；不存在返回 {}）"""
    pid = active_profile()
    if not pid:
        return {}
    d = profile_data_dir(pid) or (profiles_base() / pid)
    p = d / "profile.yaml"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if _PF_CACHE["path"] == str(p) and _PF_CACHE["mtime"] == mtime:
        return dict(_PF_CACHE["data"])
    data = {}
    try:
        data = _load_yaml_file(p)
    except Exception as e:
        print(f"[config] 读取 {p.name} 失败（{e}），忽略好友级配置")
        data = {}
    _PF_CACHE.update({"path": str(p), "mtime": mtime, "data": data})
    return dict(data)


def settings_path() -> Path:
    return ROOT / "config.yaml"


def persist_settings(updates: dict[str, Any], section: str = "llm") -> tuple[bool, str]:
    """把少量字段定向写回 config.yaml（保留注释与其余内容）。

    优先「逐行最小替换」；结构对不上或解析失败时回退 YAML 整体重写（注释会丢）。
    config.yaml 不存在时从 config.example.yaml 复制一份再改。
    返回 (是否写入, 说明) —— 不抛异常，失败原因交给调用方提示。
    """
    import re
    import yaml  # 延迟导入：仅在存在配置文件时需要 PyYAML

    cfg_path = settings_path()
    if not cfg_path.is_file():
        tpl = template_path()                          # 打包后模板在只读资源目录
        if not tpl.is_file():
            return False, "找不到 config.example.yaml，无法生成 config.yaml"
        try:
            cfg_path.write_text(tpl.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as e:
            return False, f"生成 config.yaml 失败: {e}"

    orig_text = cfg_path.read_text(encoding="utf-8")

    def _dump(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        return json.dumps(str(v), ensure_ascii=False)      # JSON 双引号是 YAML 合法标量

    text = orig_text
    for key, val in updates.items():
        new = _dump(val)
        pat = re.compile(rf"^([ \t]*{re.escape(key)}:[ \t]*)([^#\n]*?)([ \t]*(?:#.*)?)$", re.M)
        # 只在该 section 段落内替换：把 section 段切成 [段首, 段体, 其余]
        m = re.search(rf"^{re.escape(section)}:[ \t]*(?:#.*)?$", text, re.M)
        if not m:
            text = ""
            break
        seg_start = m.end()
        nxt = re.search(r"^(?![ \t#])\S", text[seg_start:], re.M)   # 下一个顶层键
        seg_end = seg_start + (nxt.start() if nxt else len(text) - seg_start)
        body = text[seg_start:seg_end]
        body2, n = pat.subn(lambda mm: mm.group(1) + new + mm.group(3), body, count=1)
        if n == 0:                                                # 段内没有该键 → 追加
            body2 = body.rstrip("\n") + f"\n  {key}: {new}\n"
        text = text[:seg_start] + body2 + text[seg_end:]

    if text:
        try:
            parsed = yaml.safe_load(text)
            ok = isinstance(parsed, dict) and all(
                (parsed.get(section) or {}).get(k) == (int(v) if isinstance(v, int) else v)
                or str((parsed.get(section) or {}).get(k)) == str(v)
                for k, v in updates.items())
            if ok:
                if text != orig_text:
                    cfg_path.write_text(text, encoding="utf-8")
                    return True, "已定向写回 config.yaml（注释保留）"
                return False, "配置无变化"
        except Exception:
            pass

    # 兜底：整体解析 → 更新 → 重写（注释会丢失）
    try:
        data = yaml.safe_load(orig_text)
        if not isinstance(data, dict):
            raise ValueError("config.yaml 顶层不是键值映射")
    except Exception as e:
        return False, f"config.yaml 无法解析（{e}）"
    sec = data.setdefault(section, {})
    sec.update(updates)
    cfg_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")
    return True, "已整体重写 config.yaml（原注释丢失）"


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


def persist_sender_matches(a_names: list[str], b_names: list[str]) -> bool:
    """把「我 / 对方」的账号名（列表）写回 config.yaml 的 people.A/B.match。

    多来源场景下同一个人的账号名往往有多个（微信 wxid、Telegram `user_me`、
    纯文本里的昵称），所以用列表承接。

    为什么必须记下来：导入向导的 A/B 默认值在没有配置时只能「按消息条数猜」，
    而对方话多时就会**把 A 猜成对方** —— 一份来源的说话人整体颠倒，会一路传导到
    关系五维、人格档案与推演语料，而且多份来源一致猜反时 `map_conflicts` 抓不到。
    记进配置后，下次导入界面就能按用户自己登记过的名字预选。

    策略与 persist_import_config 一致：优先最小文本替换（保住注释），
    结构对不上再整体重写。返回 True 表示文件已更新。
    """
    import json as _json
    import re as _re
    import yaml  # 延迟导入：仅在存在配置文件时需要 PyYAML

    cfg_path = ROOT / "config.yaml"
    if not cfg_path.is_file():
        return False
    a_names = [str(x).strip() for x in (a_names or []) if str(x).strip()]
    b_names = [str(x).strip() for x in (b_names or []) if str(x).strip()]
    if not a_names and not b_names:
        return False
    want = {"A": a_names, "B": b_names}

    orig_text = cfg_path.read_text(encoding="utf-8")

    def _values_ok(d: dict) -> bool:
        people = d.get("people") or {}
        for key in ("A", "B"):
            if not want[key]:
                continue
            cur = (people.get(key) or {}).get("match")
            have = cur if isinstance(cur, list) else ([cur] if cur else [])
            have = [str(x).strip() for x in have if str(x).strip()]
            if have != want[key]:
                return False
        return True

    try:                                   # 值本来就对：不写
        cur = yaml.safe_load(orig_text)
        if isinstance(cur, dict) and _values_ok(cur):
            return False
    except Exception:
        pass

    text = orig_text
    for key in ("A", "B"):
        if not want[key]:
            continue
        # 标量或列表都吃掉（列表形如 ["a", "b"]），替换成 JSON 列表字面量
        text = _re.sub(
            rf"(?m)^([ \t]*{key}:[^\n{{}}]*\{{[^\n{{}}]*?match:[ \t]*)"
            rf"(\[[^\]\n]*\]|\"[^\"\n]*\"|'[^'\n]*'|[^,{{}}\n]+)",
            lambda m: m.group(1) + _json.dumps(want[key], ensure_ascii=False),
            text, count=1)

    try:
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict) and _values_ok(parsed):
            if text != orig_text:
                cfg_path.write_text(text, encoding="utf-8")
                return True
            return False
    except Exception:
        pass

    try:                                   # 兜底：整体重写（注释会丢失）
        data = yaml.safe_load(orig_text)
        if not isinstance(data, dict):
            raise ValueError("config.yaml 顶层不是键值映射")
    except Exception as e:
        print(f"[config] config.yaml 无法解析（{e}），跳过「我/对方」账号名回填")
        return False
    people = data.setdefault("people", {})
    for key in ("A", "B"):
        if want[key] and isinstance(people.get(key), dict):
            people[key]["match"] = want[key]
    cfg_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")
    return True


def root() -> Path:
    return ROOT


_RES_DIR_CACHE: dict[str, str] = {}


def resource_dir() -> Path:
    """只读资源目录（schema / 静态前端 / 示例数据所在处）。

    源码运行 = 仓库 `app/`；PyInstaller 冻结时模块的 `__file__` 指向 `_MEIPASS` 根，
    而 datas 把它们放在 `_MEIPASS/app/`，所以必须逐个候选目录探测（以 schema_v1.sql 为标志）。
    """
    if _RES_DIR_CACHE.get("app"):
        return Path(_RES_DIR_CACHE["app"])
    cands: list[Path] = [Path(__file__).resolve().parent]        # 源码：仓库 app/
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands += [Path(meipass) / "app", Path(meipass)]          # 打包：_MEIPASS/app
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).resolve().parent / "_internal" / "app")
    for d in cands:
        try:
            if (d / "schema_v1.sql").is_file():
                _RES_DIR_CACHE["app"] = str(d)
                return d
        except OSError:
            continue
    return Path(__file__).resolve().parent


def sample_dir() -> Path:
    """示例数据目录（sample_data/）：源码在仓库根，打包后在只读资源目录同级"""
    cands = [ROOT / "sample_data"]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands += [Path(meipass) / "sample_data", Path(meipass) / "app" / "sample_data"]
    cands.append(resource_dir() / "sample_data")
    for d in cands:
        try:
            if (d / "chat.sample.jsonl").is_file():
                return d
        except OSError:
            continue
    return cands[0]


def template_path() -> Path:
    """config.example.yaml 的位置（源码在仓库根；打包后在只读资源目录）"""
    cands = [ROOT / "config.example.yaml"]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands += [Path(meipass) / "config.example.yaml",
                  Path(meipass) / "app" / "config.example.yaml"]
    for p in cands:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return cands[0]


def version() -> str:
    """版本号唯一来源：仓库根 `VERSION`（打包时随 exe 一起分发；缺失则回退 dev 标识）。

    冻结运行（PyInstaller）时先在 exe 同级找，再退回只读资源目录（`_MEIPASS`）。
    """
    cands = [ROOT / "VERSION"]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(Path(meipass) / "VERSION")
    for p in cands:
        try:
            v = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if v:
            return v
    return "0.0.0-dev"


def base_data_dir() -> Path:
    """基础数据目录（config paths.data_dir / IFWE_DATA_DIR；不含好友级覆盖）"""
    p = Path(_global_cfg()["paths"]["data_dir"])
    return p if p.is_absolute() else ROOT / p


def data_dir() -> Path:
    """当前生效的数据目录：好友 profile 优先，其次全局配置"""
    d = _ACTIVE.get("data_dir")
    if d:
        p = Path(d)
        return p if p.is_absolute() else ROOT / p
    pid = active_profile()
    if pid:
        pd = profile_data_dir(pid)
        if pd is not None:
            return pd
    return base_data_dir()


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
    """按配置读取 LLM 密钥：环境变量优先，其次系统凭据管理器/DPAPI（GUI 设置面板写入）。

    兼容旧环境变量名 DEEPSEEK_API_KEY；密钥永不从 config.yaml 读取。
    """
    env_name = load()["llm"]["api_key_env"] or "LLM_API_KEY"
    v = os.environ.get(env_name) or os.environ.get("DEEPSEEK_API_KEY")
    if v:
        return v
    try:                                   # keyring / DPAPI 回退（可选依赖，缺失即跳过）
        from secret_store import read_key
        k = read_key(env_name)
    except Exception:
        return None
    if k:
        os.environ.setdefault(env_name, k)     # 进程内缓存，避免每次调用都问凭据管理器
    return k or None


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


# 模块加载完再整理标准流（_safe_stdio 需要用到上面定义的 base_data_dir()/version()）。
# config 是所有入口最先 import 的模块之一，这行确保 CLI / 服务 / 打包 exe 都拿到可用流。
_safe_stdio()
