# -*- coding: utf-8 -*-
"""格式注册表（v0.2 O-5a）。

- register(imp): 登记一个 Importer 实例（模块导入侧自注册）；
- auto_detect(path): 按注册序逐个 detect，返回首个命中；全部未命中返回 None；
- by_name(name) / names(): 按 source_name 取用（CLI --format 用）。

内置 adapter 在首次使用时懒加载；扩展格式只需实现 Importer 协议并 register。
"""
from __future__ import annotations

from pathlib import Path

from importers.base import Importer

_IMPORTERS: list[Importer] = []
_LOADED = False

# 内置格式 → 实现模块（懒加载；顺序即 auto_detect 优先级）
_BUILTIN = ("chatlab_jsonl", "wecomsg_csv", "telegram_json")


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    import importlib
    for mod in _BUILTIN:
        try:
            importlib.import_module(f"importers.{mod}")
        except ImportError:
            pass
    _LOADED = True


def register(imp: Importer) -> None:
    """登记导入器（同 source_name 后到者忽略，先到者优先）。"""
    if all(getattr(x, "source_name", "") != imp.source_name for x in _IMPORTERS):
        _IMPORTERS.append(imp)


def _all() -> list[Importer]:
    _ensure_loaded()
    return list(_IMPORTERS)


def auto_detect(path: Path) -> Importer | None:
    """按注册序探测文件格式；单个 detect 异常视为未命中（不外泄）。"""
    for imp in _all():
        try:
            if imp.detect(path):
                return imp
        except Exception:
            continue
    return None


def by_name(name: str) -> Importer | None:
    for imp in _all():
        if imp.source_name == name:
            return imp
    return None


def names() -> list[str]:
    return [imp.source_name for imp in _all()]
