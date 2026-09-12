#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IfWe · 一键入口（项目根执行）

  python run.py init      # 生成 config.yaml（首次）；配合 --source/--sender-a/--sender-b 直接导入
  python run.py analyze   # 分析：事件/记忆/关系状态/转折点/人格（--skip-llm 离线路径）
  python run.py server    # 启动本地聊天界面（仅 127.0.0.1）
  python run.py demo      # 不导入真实数据：用内置虚构示例数据体验完整界面

隐私：全部计算在本地完成；只有「发送消息给数字人格」会把脱敏后的上下文
发给你自行配置的 LLM API。详见 PRIVACY.md。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

DEMO_DATA_DIR = "data_demo"


# ---------------------------------------------------------------- init
def cmd_init(args) -> int:
    import config as cfg_mod

    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        shutil.copy(ROOT / "config.example.yaml", cfg_path)
        print(f"[init] 已生成 {cfg_path.name}（从 config.example.yaml 复制）")
    else:
        print(f"[init] {cfg_path.name} 已存在，跳过")

    src = args.source
    if src:
        from phase1_ingest import ingest

        people = cfg_mod.load()["people"]
        smap = {}
        for key, cli_val in (("A", args.sender_a), ("B", args.sender_b)):
            name = (cli_val or (people.get(key) or {}).get("match") or "").strip()
            if name:
                smap[name] = key
        if len(smap) < 2:
            print("[init] 缺少账号名映射：用 --sender-a/--sender-b，"
                  "或写进 config.yaml 的 people.A.match / people.B.match")
            return 2
        p = Path(src)
        if not p.is_absolute():
            p = ROOT / p
        if not p.is_file():
            print(f"[init] 源文件不存在: {p}")
            return 2
        print(f"[init] 导入 {p}（映射 {smap}）…")
        cfg = cfg_mod.load()
        summary = ingest(p, smap, db_path=cfg_mod.db_path(),
                         session_gap_s=int(cfg["chat"]["session_gap_minutes"]) * 60)
        if summary.get("gates_all_pass"):
            print("[init] 导入完成。下一步: python run.py analyze")
            return 0
        print("[init] 导入完成，但存在未通过的门禁项（见上方汇总），请检查数据")
        return 1

    print("""
[init] 下一步：
  1. 编辑 config.yaml：chat.source 指向你的 JSONL；people.A.match / people.B.match
     填导出文件里你与对方的原始账号名
  2. 导入:      python run.py init --source 你的记录.jsonl
     （或单独） python scripts/import_chat.py --source ... --sender-a ... --sender-b ...
  3. 分析:      python run.py analyze
  4. 启动:      python run.py server
  不想导入真实数据？直接体验: python run.py demo""")
    return 0


# ---------------------------------------------------------------- analyze
def cmd_analyze(args) -> int:
    sys.path.insert(0, str(ROOT / "app"))
    import analyze_pipeline

    analyze_pipeline.run_all(skip_llm=args.skip_llm)
    return 0


# ---------------------------------------------------------------- server
def cmd_server(args) -> int:
    import config as cfg_mod

    port = int(args.port or cfg_mod.load()["defaults"]["port"])
    os.environ["PORT"] = str(port)          # phase15_api.PORT 与实际监听端口保持一致
    if not cfg_mod.db_path().exists():
        print("[server] 还没有数据库。先 python run.py init + analyze，"
              "或用 python run.py demo 体验示例数据。")
        return 1
    import uvicorn
    from phase15_api import app  # noqa: E402

    key_env = cfg_mod.load()["llm"]["api_key_env"]
    print(f"IfWe 对话服务: http://127.0.0.1:{port}  （Ctrl+C 停止）")
    if not cfg_mod.api_key():
        print(f"[warn] 未检测到 LLM API Key（环境变量 {key_env}）——可浏览，发消息会报错")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


# ---------------------------------------------------------------- demo
def cmd_demo(args) -> int:
    """虚构示例数据一键体验：导入 sample → 离线分析 → 装载示例人格 → 启动界面"""
    os.environ["IFWE_DATA_DIR"] = DEMO_DATA_DIR   # 与真实数据完全隔离
    sys.path.insert(0, str(ROOT / "app"))
    import config as cfg_mod
    import analyze_pipeline
    from phase1_ingest import ingest

    sample = ROOT / "sample_data" / "chat.sample.jsonl"
    print(f"[demo] 使用虚构示例数据（{sample.relative_to(ROOT)}），"
          f"全部产物写入 {DEMO_DATA_DIR}/，与真实数据隔离\n")
    summary = ingest(
        sample,
        {"沈星然": "A", "林晚语": "B"},            # 示例数据内置的虚构人物
        db_path=cfg_mod.db_path())
    print(f"[demo] 示例记录导入完成: {summary['message_count']} 条 "
          f"（{summary['first_day']} ~ {summary['last_day']}）")

    print("[demo] 离线分析（启发式事件 + 估计关系状态 + 转折点）…")
    analyze_pipeline.run_all(skip_llm=True)

    pdir = cfg_mod.persona_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    for person in ("A", "B"):
        shutil.copy(ROOT / "sample_data" / f"persona_v1_{person}.json",
                    pdir / f"persona_v1_{person}.json")
    print("[demo] 已装载示例人格档案（虚构人物）\n")

    if not args.no_server:
        cmd_server(args)
    else:
        print("[demo] 构建完成（--no-server）。之后可随时: python run.py server")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="run.py", description="IfWe 一键入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="生成 config.yaml 并（可选）导入聊天记录")
    p.add_argument("--source", type=str, default="", help="聊天记录 JSONL 路径")
    p.add_argument("--sender-a", type=str, default="", help="你的原始账号名 → A")
    p.add_argument("--sender-b", type=str, default="", help="对方的原始账号名 → B")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("analyze", help="生成事件/记忆/关系状态/转折点/人格")
    p.add_argument("--skip-llm", action="store_true",
                   help="离线路径：关键词启发式事件 + persona 模板（无需 API Key）")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("server", help="启动本地聊天界面（127.0.0.1）")
    p.add_argument("--port", type=int, default=0, help="端口（默认取 config defaults.port）")
    p.set_defaults(func=cmd_server)

    p = sub.add_parser("demo", help="用内置虚构示例数据体验完整界面")
    p.add_argument("--port", type=int, default=0, help="端口（默认取 config defaults.port）")
    p.add_argument("--no-server", action="store_true", help="只构建不启动界面")
    p.set_defaults(func=cmd_demo)

    args = ap.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
