# -*- coding: utf-8 -*-
"""
IfWe · 密钥存储（GUI 设置面板写入用）

隐私红线：**密钥不落明文**。本模块只提供两个后端，二者都满足该约束：

  1. `keyring` → Windows 凭据管理器（首选；服务名 `IfWe`，账号取 llm.api_key_env 的值）
  2. DPAPI 加密文件 → `data/.secret_llm_key`（keyring 不可用时的回退；密文由当前
     Windows 用户的 DPAPI 密钥保护，换用户/换机器均无法解密）

对外接口：`save_key / read_key / clear_key / hint / backend`。
读取顺序由 `config.api_key()` 决定：环境变量 > 凭据管理器（本模块）。
任何失败都静默降级（返回 None / False），绝不抛到调用方，也绝不打印密钥。
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import config as cfg_mod

SERVICE = "IfWe"
FALLBACK_NAME = ".secret_llm_key"
FALLBACK_MAGIC = b"IFWE1"

_cache: dict[str, object] = {"keyring_ok": None, "backend": ""}


def account(env_name: str | None = None) -> str:
    """凭据管理器里的账号名（用 llm.api_key_env 作账号，避免多套 key 互相覆盖）"""
    if env_name:
        return env_name
    try:
        return cfg_mod.load()["llm"]["api_key_env"] or "LLM_API_KEY"
    except Exception:
        return "LLM_API_KEY"


def fallback_path() -> Path:
    """DPAPI 回退文件（放基础数据目录，不属于任何单个好友）"""
    return cfg_mod.base_data_dir() / FALLBACK_NAME


# ---------------------------------------------------------------- keyring
def _keyring():
    import keyring
    return keyring


def keyring_ok() -> bool:
    """探测 keyring 是否真的可用（装了库但后端缺失时会在调用时抛错）"""
    if _cache["keyring_ok"] is not None:
        return bool(_cache["keyring_ok"])
    ok = False
    try:
        _keyring().get_password(SERVICE, account())
        ok = True
    except Exception:
        ok = False
    _cache["keyring_ok"] = ok
    return ok


# ---------------------------------------------------------------- DPAPI
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong if os.name != "nt" else ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_in(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_out_bytes(blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI 仅在 Windows 可用")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    out = _BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(_blob_in(data)),
                                  ctypes.c_wchar_p("IfWe LLM key"),
                                  None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptProtectData 失败 (err={ctypes.get_last_error()})")
    try:
        return _blob_out_bytes(out)
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI 仅在 Windows 可用")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    out = _BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(_blob_in(blob)), None,
                                    None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptUnprotectData 失败 (err={ctypes.get_last_error()})")
    try:
        return _blob_out_bytes(out)
    finally:
        kernel32.LocalFree(out.pbData)


def _file_save(key: str) -> None:
    p = fallback_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(FALLBACK_MAGIC + _dpapi_protect(key.encode("utf-8")))


def _file_read() -> str | None:
    p = fallback_path()
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if not raw.startswith(FALLBACK_MAGIC):
        return None
    try:
        return _dpapi_unprotect(raw[len(FALLBACK_MAGIC):]).decode("utf-8") or None
    except Exception:
        return None


def _file_clear() -> None:
    try:
        fallback_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 对外接口
def save_key(key: str, env_name: str | None = None) -> str:
    """保存密钥 → 返回实际使用的后端名（credential / dpapi-file）"""
    key = (key or "").strip()
    if not key:
        raise ValueError("密钥为空")
    acct = account(env_name)
    if keyring_ok():
        try:
            _keyring().set_password(SERVICE, acct, key)
            _file_clear()                      # 后端升级到凭据管理器，清理旧回退文件
            _cache["backend"] = "credential"
            return "credential"
        except Exception:
            _cache["keyring_ok"] = False
    _file_save(key)
    _cache["backend"] = "dpapi-file"
    return "dpapi-file"


def read_key(env_name: str | None = None) -> str | None:
    """读取密钥（凭据管理器优先，其次 DPAPI 文件）；读不到返回 None"""
    acct = account(env_name)
    if keyring_ok():
        try:
            v = _keyring().get_password(SERVICE, acct)
            if v:
                _cache["backend"] = "credential"
                return v
        except Exception:
            _cache["keyring_ok"] = False
    v = _file_read()
    if v:
        _cache["backend"] = "dpapi-file"
    return v


def clear_key(env_name: str | None = None) -> bool:
    """清除两个后端里的密钥；返回是否确实清掉了东西（用于 UI 提示）"""
    acct = account(env_name)
    had = False
    if keyring_ok():
        try:
            if _keyring().get_password(SERVICE, acct):
                had = True
            _keyring().delete_password(SERVICE, acct)
        except Exception:
            pass
    if fallback_path().exists():
        had = True
    _file_clear()
    return had


def backend() -> str:
    """当前密钥实际存于何处：credential / dpapi-file / none"""
    if _cache["backend"]:
        return str(_cache["backend"])
    if keyring_ok():
        try:
            if _keyring().get_password(SERVICE, account()):
                return "credential"
        except Exception:
            pass
    return "dpapi-file" if fallback_path().exists() else "none"


def hint(key: str | None = None) -> str:
    """密钥提示片段（首 3 + 末 3，如 `sk-***abc`）；永远不回传完整 key"""
    if key is None:
        key = read_key() or ""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-3:]}"


def describe() -> dict:
    """给设置面板用的状态快照（不含任何完整密钥）"""
    k = read_key()
    return {"key_state": "set" if k else "unset", "key_hint": hint(k),
            "backend": backend(), "keyring_available": keyring_ok(),
            "os": sys.platform}
