"""意图分类准确率评估工具（真实会话数据驱动）。


数据来源：checkpoint_blobs 的 messages channel
    —— LangGraph 的 PG checkpointer 落库的真实会话，每条 Human 消息 = 一次真实输入。

用法：
    # 评估关键词兜底（不调 LLM，秒出）
    uv run --with msgpack python scripts/eval_intent.py

    # 评估 LLM 主路径（要调 API，约 1.3s/条）
    uv run --with msgpack python scripts/eval_intent.py --llm

    # 导出标注集（拿去训练 BERT 时用）
    uv run --with msgpack python scripts/eval_intent.py --export data/intent_dataset.jsonl

    注：msgpack 没进 pyproject（只有 LangGraph 内部在用），这里临时拉一个。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# =====================================================================
# 人工标注的期望意图（按产品语义：学生想要什么，不是字面关键词）
# 数据来源：checkpoint_blobs 里的真实会话（2026-10-25 提取，34 条去重）
# =====================================================================
GOLD: dict[str, str] = {
    # ---- 提交作答：纯答案内容 ----
    "-3": "submit_answer",
    "15   -15": "submit_answer",
    "3": "submit_answer",
    "3 5": "submit_answer",
    "5/4": "submit_answer",
    "x=2 或 x =3": "submit_answer",
    "x=2或3": "submit_answer",
    "x=3,x=5": "submit_answer",
    "x=5或x=1": "submit_answer",
    "x_1=2，x_2=3": "submit_answer",
    "答案是：5/4": "submit_answer",

    # ---- 要引导：想要方向/提示，自己没思路 ----
    "不会": "guide_scaffold",
    "不会写": "guide_scaffold",
    "怎么做": "guide_scaffold",
    "考啥": "guide_scaffold",
    "还是不会": "guide_scaffold",
    "还是没有思路": "guide_scaffold",
    "这题考什么": "guide_scaffold",

    # ---- 要完整讲解 ----
    "我完全没有思路，你跟我讲解下": "explain",
    "查看完整解析": "explain",
    "直接讲解": "explain",
    "直接讲解并告诉我最后答案": "explain",
    "直接跟我讲解下这道题": "explain",
    "讲解一下": "explain",
    "讲解下": "explain",
    "还是不会，直接讲解": "explain",
    "答案看不懂": "explain",
    "不需要给我提示，直接讲解这道题": "explain",

    # ---- 直接要答案 ----
    "不会，直接做出来给我看下": "ask_answer",
    "直接告诉我答案": "ask_answer",
    "答案": "ask_answer",
    "答案‘": "ask_answer",

    # ---- 闲聊 ----
    "你有病？": "off_topic",
}


def load_real_inputs() -> list[str]:
    """从 checkpoint_blobs 解出所有真实学生输入（去重）。

    LangGraph 的 PG checkpointer 把每条消息用 msgpack 序列化存进 checkpoint_blobs，
    channel='messages'，每条是 ExtType(code=5)，
    data 里是[模块路径, 类名, 字段字典, 校验器名] 四元组。
    """
    import msgpack
    from sqlalchemy import text

    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        rows = session.execute(text(
            "select blob from checkpoint_blobs where channel='messages'"
        )).fetchall()
    finally:
        session.close()

    out: list[str] = []
    for (blob,) in rows:
        for item in msgpack.unpackb(blob, raw=False):
            try:
                arr = msgpack.unpackb(item.data, raw=False)
            except Exception:
                continue
            if len(arr) >= 3 and arr[1] == "HumanMessage":
                content = arr[2].get("content")
                if isinstance(content, str) and content.strip():
                    out.append(content.strip())
    return sorted(set(out))


def evaluate_fallback(inputs: list[str]) -> tuple[int, int, list]:
    """评估关键词兜底（不调 LLM）。"""
    from app.graphs.student.nodes import _fallback_intent

    ok, wrong = 0, []
    for text in inputs:
        gold = GOLD.get(text)
        if not gold:
            continue
        pred = _fallback_intent(text)
        if pred == gold:
            ok += 1
        else:
            wrong.append((text, gold, pred))
    return ok, len(inputs), wrong


def evaluate_llm(inputs: list[str]) -> tuple[int, int, list, float]:
    """评估 LLM 主路径（走真实的 classify_intent 节点）。"""
    from langchain_core.messages import HumanMessage

    from app.graphs.student.nodes import classify_intent

    ok, wrong = 0, []
    t0 = time.time()
    for text in inputs:
        gold = GOLD.get(text)
        if not gold:
            continue
        out = classify_intent({
            "student_id": 1,
            "question_id": 34,
            "messages": [HumanMessage(content=text)],
        })
        pred = out["intent"]
        if pred == gold:
            ok += 1
        else:
            wrong.append((text, gold, pred))
    return ok, len(inputs), wrong, time.time() - t0


def evaluate_zero_shot(inputs: list[str]) -> tuple[int, int, list, float]:
    """评估零样本向量分类（bge-m3 原型匹配，零训练）。

    ★ 这是"不上模型"的第三条路：给每个意图写几个样例句当原型，
      学生输入 embedding 后算余弦相似度，取最近的原型。
      好处：零训练、零标注、42ms/条；坏处：不如 LLM 懂上下文。
    """
    import numpy as np

    from app.rag.embeddings import get_embeddings

    prototypes = {
        "ask_answer": ["这题答案是多少", "直接告诉我答案", "做出来给我看下", "答案"],
        "guide_scaffold": ["这题考什么", "我没思路", "给点提示", "不会写", "还是不会", "怎么做"],
        "submit_answer": ["x=2或3", "5/4", "我算出来是 3", "答案是：5/4"],
        "explain": ["讲解一下", "详细讲讲", "解析一下", "答案看不懂", "查看完整解析"],
        "off_topic": ["你好", "谢谢老师"],
    }

    em = get_embeddings()

    def norm(v):
        return v / np.linalg.norm(v)

    protos = {
        k: norm(np.mean([em.embed_query(s) for s in v], axis=0))
        for k, v in prototypes.items()
    }

    ok, wrong = 0, []
    t0 = time.time()
    for text in inputs:
        gold = GOLD.get(text)
        if not gold:
            continue
        v = norm(em.embed_query(text))
        pred = max(protos, key=lambda k: float(np.dot(v, protos[k])))
        if pred == gold:
            ok += 1
        else:
            wrong.append((text, gold, pred))
    return ok, len(inputs), wrong, time.time() - t0


def export_dataset(inputs: list[str], path: Path) -> int:
    """导出标注集（JSONL，训练 BERT 时用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for text in inputs:
            gold = GOLD.get(text)
            if not gold:
                continue
            f.write(json.dumps({"text": text, "intent": gold}, ensure_ascii=False) + "\n")
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="意图分类准确率评估")
    ap.add_argument("--llm", action="store_true", help="评估 LLM 主路径（要调 API）")
    ap.add_argument("--zero-shot", action="store_true", help="评估零样本向量分类（本地 bge-m3）")
    ap.add_argument("--export", type=Path, help="导出标注集到指定 JSONL 路径")
    args = ap.parse_args()

    inputs = load_real_inputs()
    print(f"真实去重输入: {len(inputs)} 条（来自 checkpoint_blobs 的真实会话）\n")

    # ---- 期望分布 ----
    dist = Counter(GOLD.get(t, "?") for t in inputs)
    print("=== 期望意图分布 ===")
    for k, v in dist.most_common():
        print(f"  {k:16s} {v}")
    print()

    # ---- 评估 ----
    if args.llm:
        ok, total, wrong, elapsed = evaluate_llm(inputs)
        print("=== LLM 主路径 ===")
        print(f"  准确率: {ok}/{total} = {100.0 * ok / total:.1f}%   "
              f"耗时 {elapsed:.1f}s ({elapsed / max(total, 1):.2f}s/条)")
    elif args.zero_shot:
        ok, total, wrong, elapsed = evaluate_zero_shot(inputs)
        print("=== 零样本向量分类（bge-m3 原型匹配）===")
        print(f"  准确率: {ok}/{total} = {100.0 * ok / total:.1f}%   "
              f"耗时 {elapsed:.1f}s ({elapsed / max(total, 1):.3f}s/条)")
    else:
        ok, total, wrong = evaluate_fallback(inputs)
        print("=== 关键词兜底（_fallback_intent）===")
        print(f"  准确率: {ok}/{total} = {100.0 * ok / total:.1f}%")

    if wrong:
        print("  错分明细:")
        for t, g, p in wrong:
            print(f"    {t[:32]:34s} 期望 {g:14s} 实得 {p}")
    print()

    # ---- 训练可行性 ----
    print("=== 训练可行性（BERT 微调通常每类要 200+ 条）===")
    for intent, n in dist.most_common():
        flag = " ← 太少" if n < 20 else ""
        print(f"  {intent:16s} {n} 条{flag}")
    if sum(dist.values()) < 200:
        print(f"\n  ★ 结论：当前 {sum(dist.values())} 条样本，不足以训练 BERT。"
              "\n    先把 LLM 主路径修好（已从 52.9% → 97.1%），"
              "\n    同时埋点积累真实样本，达到量级后再考虑微调。")
    print()

    # ---- 导出 ----
    if args.export:
        n = export_dataset(inputs, args.export)
        print(f"已导出 {n} 条标注集 → {args.export}")


if __name__ == "__main__":
    main()
