# -*- coding: utf-8 -*-
"""
IfWe Phase 5 · A5 防人格漂移 PCC（全量反思 + 规划；用户已拍板）
- 规划: 每模拟日开始前，双 Agent 各自生成「今日互动计划」（Generative Agents 每日规划思想）
- 反思: 每会话结束后，对该会话双 Agent 表现做一致性检查（对照 persona L/M/S/U 基线），
        输出 deviations(偏离点) + calibration(校准指令)，校准指令回填下一轮 Agent 提示
- 日志: 全部落入 sim_pcc_log（kind=plan/reflection/calibration）
用法: 供 phase5_a2_loop.py 调用；`python phase5_a5_pcc.py` 冒烟（无 key 时输出提示）
"""
from __future__ import annotations

import json

import phase5_common as pc
from phase5_llm import DailyPlan, Reflection, PLAN_SYS, REFLECTION_SYS, plan_user_block, reflection_user_block

PCC_KINDS = ("plan", "reflection", "calibration")


def daily_plan(client, agent_key: str, persona_block: str, world_desc: str,
               memories_text: str, yesterday: str, buffer: str) -> dict:
    """生成角色 agent_key 的今日计划 → 返回 {"person": key, "day": …, "plan": …}"""
    user = plan_user_block(persona_block, world_desc, memories_text, yesterday, buffer)
    obj = client.extract(system=PLAN_SYS, user=user, response_model=DailyPlan)
    return {"plan": obj.plan}


def reflect(client, agent_key: str, persona_block: str, transcript: str,
            world_desc: str) -> dict:
    """会话后反思 agent_key → {"person": key, "content", "deviations", "calibration"}"""
    user = reflection_user_block(persona_block, transcript, world_desc)
    obj = client.extract(system=REFLECTION_SYS, user=user, response_model=Reflection)
    return {"person": agent_key, "content": obj.content,
            "deviations": obj.deviations, "calibration": obj.calibration}


def log_pcc(conn, sim_id: str, day: str, kind: str, target: str, content: str,
            deviations: list | None = None) -> None:
    dev = json.dumps(deviations or [], ensure_ascii=False)
    lid = f"{sim_id}-{kind[:3].upper()}{len(conn.execute('SELECT 1 FROM sim_pcc_log').fetchall()) + 1:03d}"
    conn.execute(
        """INSERT OR REPLACE INTO sim_pcc_log (log_id, sim_id, day, kind, target, content, deviations, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (lid, sim_id, day, kind, target, content, dev, pc.now_str()))
    conn.commit()
    return lid


if __name__ == "__main__":
    print("PCC 模块就绪（plan/reflection/calibration）；由 A2 会话循环调用。")