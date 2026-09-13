#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IfWe · 一键入口（项目根执行）

  python run.py doctor --source <文件>   # 导入体检：只读识别格式/跨度/账号/结论
  python run.py init      # 生成 config.yaml（首次）；配合 --source/--sender-a/--sender-b 直接导入
  python run.py analyze   # 分析：事件/记忆/关系状态/转折点/人格（--skip-llm 离线路径）
  python run.py server    # 启动本地聊天界面（仅 127.0.0.1）
  python run.py demo      # 不导入真实数据：用内置虚构示例数据体验完整界面

GUI 能力的命令行等价物（v0.3 桌面化）：
  python run.py profile list|new <名>|use <id>|rm <id>|migrate   # 多好友（profile）
  python run.py settings --provider/--base-url/--model           # LLM 设置
  python run.py key set|show|clear                               # 密钥（凭据管理器/DPAPI）
  python run.py desktop                                          # 桌面窗口（等同 desktop.py）

隐私：全部计算在本地完成；只有「发送消息给数字人格」会把脱敏后的上下文
发给你自行配置的 LLM API。详见 PRIVACY.md。
"""
from __future__ import annotations

import argparse
import json
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
            if cfg_mod.persist_import_config(Path(src).as_posix(), smap):
                print("[init] 已将 source/账号映射写入 config.yaml"
                      "（之后重导入无需再带 --sender-a/--sender-b）")
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


# ---------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    """导入体检（只读不写库）：格式识别/跨度/候选账号/类型分布/脱敏预估/结论。"""
    from doctor import format_report, run_doctor

    src = Path(args.source)
    if not src.is_absolute():
        src = ROOT / src
    report = run_doctor(src)
    print(format_report(report))
    return 0 if report.get("importable") else 1


# ---------------------------------------------------------------- analyze
def cmd_analyze(args) -> int:
    sys.path.insert(0, str(ROOT / "app"))
    import config as cfg_mod
    import analyze_pipeline

    _use_profile(getattr(args, "profile", ""))
    summary = analyze_pipeline.run_all(skip_llm=args.skip_llm)
    if summary.get("persona") == "template":
        pdir = cfg_mod.persona_dir()
        try:
            pdir_disp = pdir.relative_to(ROOT).as_posix()
        except ValueError:
            pdir_disp = str(pdir)
        empties = [f"persona_v1_{p}.json" for p in ("A", "B")
                   if (pdir / f"persona_v1_{p}.json").exists()
                   and analyze_pipeline.persona_file_is_empty(pdir / f"persona_v1_{p}.json")]
        if empties:
            print(f"""
[note] persona 还是空模板：请编辑 {pdir_disp}/{' 与 '.join(empties)}
       各层填什么见文件内 _how_to_edit；填写范例见 sample_data/persona_v1_*.json
       编辑后无需重跑分析：新开一条对话线（或重启 python run.py server）即生效""")
    return 0


# ---------------------------------------------------------------- server
def cmd_server(args) -> int:
    import config as cfg_mod

    _use_profile(getattr(args, "profile", ""),
                 allow_registry=not getattr(args, "no_profile", False))
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
    print(f"[server] 当前好友: {cfg_mod.active_profile() or '(未启用多好友)'}"
          f"  数据目录: {cfg_mod.data_dir()}")
    if not cfg_mod.api_key():
        print(f"[warn] 未检测到 LLM API Key（环境变量 {key_env} 或界面「设置」面板）"
              "——可浏览，发消息会报错")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def _use_profile(pid: str, allow_registry: bool = True) -> None:
    """命令行指定好友：优先显式参数，其次环境变量 IFWE_PROFILE，否则用上次选择的好友"""
    import config as cfg_mod

    pid = (pid or os.environ.get(cfg_mod.ACTIVE_PROFILE_ENV, "")).strip()
    if not pid and allow_registry:
        items = cfg_mod.read_registry()
        if items:
            ranked = sorted(items, key=lambda it: it.get("last_active") or "", reverse=True)
            pid = ranked[0].get("id") or ""
    if not pid:
        return
    try:
        import profiles as pmod
        if not cfg_mod.profile_entry(pid):
            if pid == pmod.DEMO_ID:
                pmod.ensure_demo_profile()
            else:
                print(f"[profile] 好友 {pid} 不存在，按默认数据目录运行")
                return
        cfg_mod.set_active_profile(pid)
        import phase5_common as pc
        pc.refresh_paths()
    except Exception as e:
        print(f"[profile] 载入好友 {pid} 失败（按默认数据目录运行）：{e}")


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
        args.no_profile = True          # demo 目录自成一套，不走多好友注册表
        cmd_server(args)
    else:
        print("[demo] 构建完成（--no-server）。之后可随时: python run.py server")
    return 0


# ---------------------------------------------------------------- profile / settings / key（GUI 等价物）
def cmd_profile(args) -> int:
    """多好友管理（与界面左侧好友栏等价）"""
    import config as cfg_mod
    import profiles as pmod

    act = args.action
    if act == "migrate":
        res = pmod.ensure_migrated()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if act == "new":
        if not args.name:
            print("[profile] 需要好友名字：python run.py profile new <名字>")
            return 2
        entry = pmod.create(args.name)
        print(f"[profile] 已创建好友「{entry['name']}」(id={entry['id']}) "
              f"→ {entry['data_dir']}/")
        print("[profile] 切过去开始使用：python run.py profile use " + entry["id"])
        return 0

    if act == "use":
        if not args.name:
            print("[profile] 需要好友 id：python run.py profile use <id>")
            return 2
        if not cfg_mod.profile_entry(args.name):
            print(f"[profile] 好友 {args.name} 不存在")
            return 1
        cfg_mod.set_active_profile(args.name)
        pmod.touch(args.name)
        print(f"[profile] 当前好友已切换为 {args.name}（数据目录 {cfg_mod.data_dir()}）")
        print("[profile] 之后执行 run.py server / analyze 不带 --profile 也用它")
        return 0

    if act == "rm":
        if not args.name:
            print("[profile] 需要好友 id：python run.py profile rm <id>")
            return 2
        try:
            res = pmod.delete(args.name)
        except (KeyError, ValueError) as e:
            print(f"[profile] 删除失败：{e}")
            return 1
        if not res.get("ok"):
            print("[profile] 删除未完成（部分文件被占用）：" + "；".join(res.get("errors") or []))
            return 1
        print(f"[profile] 已删除好友 {args.name}（目录 {res.get('removed')}）")
        return 0

    # 默认 list
    st = pmod.status()
    print(f"好友列表（注册表 {st['registry']}，数据根 {st['base']}）:")
    for p in st["profiles"]:
        mark = "*" if p.get("active") else " "
        flags = []
        if p.get("demo"):
            flags.append("示例")
        if not p.get("exists"):
            flags.append("目录缺失")
        elif not p.get("has_db"):
            flags.append("无数据")
        print(f" {mark} {p['id']:<16} {p['name']:<18} {p['dir']}"
              + (f"  [{','.join(flags)}]" if flags else ""))
    print("\n  * = 当前好友；切换: python run.py profile use <id>")
    return 0


def cmd_settings(args) -> int:
    """LLM 设置（与界面「设置」面板等价；密钥请用 run.py key set）"""
    import config as cfg_mod

    updates = {}
    if args.provider:
        updates["provider"] = args.provider
    if args.base_url:
        updates["base_url"] = args.base_url
    if args.model:
        updates["model"] = args.model
    if args.max_tokens:
        updates["max_tokens"] = int(args.max_tokens)
    if updates:
        ok, note = cfg_mod.persist_settings(updates)
        print(f"[settings] {note}")
        if not ok and "失败" in note:
            return 1
    llm = cfg_mod.load()["llm"]
    key = cfg_mod.api_key()
    print(f"[settings] provider={llm['provider']}  model={llm['model']}  "
          f"base_url={llm['base_url']}  max_tokens={llm['max_tokens']}")
    print(f"[settings] API Key: {'已设置' if key else '未设置'}"
          f"（环境变量 {llm['api_key_env']} 或系统凭据管理器）")
    return 0


def cmd_key(args) -> int:
    """密钥管理：set（交互式隐藏输入）/ show（只看掩码）/ clear"""
    import config as cfg_mod
    import secret_store

    env_name = cfg_mod.load()["llm"]["api_key_env"] or "LLM_API_KEY"
    act = args.action
    if act == "set":
        import getpass
        k = getpass.getpass("粘贴 LLM API Key（输入不回显）：").strip()
        if not k:
            print("[key] 空输入，已取消")
            return 2
        backend = secret_store.save_key(k, env_name)
        where = "Windows 凭据管理器" if backend == "credential" else "DPAPI 加密文件"
        print(f"[key] 已保存到{where}（不写 config.yaml）：{secret_store.hint(k)}")
        print(f"[key] 提示：本机命令行会话仍需 export {env_name}=... 才会被当前进程读到；"
              f"重启服务后自动从凭据管理器读取")
        return 0
    if act == "clear":
        had = secret_store.clear_key(env_name)
        print("[key] 已清除" if had else "[key] 本就没有保存过密钥")
        return 0
    k = cfg_mod.api_key()
    print(f"[key] 状态: {'已设置' if k else '未设置'}  "
          f"来源: {'环境变量' if os.environ.get(env_name) else secret_store.backend()}  "
          f"掩码: {secret_store.hint(k) if k else '—'}")
    return 0


def cmd_desktop(args) -> int:
    """桌面窗口（等同直接运行 desktop.py）"""
    import subprocess
    cmd = [sys.executable, str(ROOT / "desktop.py")]
    if args.browser:
        cmd.append("--browser")
    if args.port:
        cmd += ["--port", str(args.port)]
    return int(subprocess.call(cmd))


def main() -> int:
    ap = argparse.ArgumentParser(prog="run.py", description="IfWe 一键入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="生成 config.yaml 并（可选）导入聊天记录")
    p.add_argument("--source", type=str, default="", help="聊天记录 JSONL 路径")
    p.add_argument("--sender-a", type=str, default="", help="你的原始账号名 → A")
    p.add_argument("--sender-b", type=str, default="", help="对方的原始账号名 → B")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="导入体检：只读分析聊天文件，不改库")
    p.add_argument("--source", type=str, required=True, help="导出的聊天记录文件路径")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("analyze", help="生成事件/记忆/关系状态/转折点/人格")
    p.add_argument("--skip-llm", action="store_true",
                   help="离线路径：关键词启发式事件 + persona 模板（无需 API Key）")
    p.add_argument("--profile", type=str, default="", help="指定好友 id（默认用上次选择的好友）")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("server", help="启动本地聊天界面（127.0.0.1）")
    p.add_argument("--port", type=int, default=0, help="端口（默认取 config defaults.port）")
    p.add_argument("--profile", type=str, default="", help="指定好友 id（默认用上次选择的好友）")
    p.set_defaults(func=cmd_server)

    p = sub.add_parser("demo", help="用内置虚构示例数据体验完整界面")
    p.add_argument("--port", type=int, default=0, help="端口（默认取 config defaults.port）")
    p.add_argument("--no-server", action="store_true", help="只构建不启动界面")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("desktop", help="桌面窗口启动（等同运行 desktop.py）")
    p.add_argument("--port", type=int, default=0, help="端口（默认取 config defaults.port）")
    p.add_argument("--browser", action="store_true", help="用默认浏览器打开，不建原生窗口")
    p.set_defaults(func=cmd_desktop)

    p = sub.add_parser("profile", help="多好友管理：list / new / use / rm / migrate")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "new", "use", "rm", "migrate"])
    p.add_argument("name", nargs="?", default="", help="好友名字（new）或好友 id（use/rm）")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("settings", help="查看/修改 LLM 设置（密钥用 run.py key set）")
    p.add_argument("--provider", type=str, default="", help="deepseek / custom")
    p.add_argument("--base-url", type=str, default="", help="OpenAI 兼容端点")
    p.add_argument("--model", type=str, default="", help="模型名")
    p.add_argument("--max-tokens", type=int, default=0, help="单次生成上限")
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("key", help="密钥管理：set / show / clear（不落明文）")
    p.add_argument("action", nargs="?", default="show", choices=["set", "show", "clear"])
    p.set_defaults(func=cmd_key)

    args = ap.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
