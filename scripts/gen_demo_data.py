"""M6-T4 · 演示数据生成脚本：账号 + 学习历史 + 虚拟班级作答（可反复跑，幂等）。

【这一个脚本伺候两条闭环线的"数据就绪"】
  闭环线一（S-10 测验）：学生得先有"近 30 天错题"，S-10 的 fetch_error_kps 才有料
      → 本脚本造 演示作业 + 错题作答 + 错因归因 + BKT 基线
  闭环线二（R-08 出卷 → R-04 分析）：分析要有"虚拟班级作答"才出得了 P/D
      → 本脚本 --paper-id 给指定试卷合成全班作答，且 P/D 布局确定可预期：
        第 1 题过难 / 第 2 题过易 / 第 3 题区分度不足 / 其余正常
        （analyze 后恰好 3 题写回 quality_flag=abnormal，下次出卷降权可见）

【用法】
  uv run python scripts/gen_demo_data.py                # 造账号/班级/学习历史（幂等）
  uv run python scripts/gen_demo_data.py --paper-id 3   # 再给 3 号卷合成虚拟作答
  uv run python scripts/gen_demo_data.py --paper-id 3 --force   # 删旧作答重合成

【演示账号】（密码统一 Passw0rd1）
  A001 管理员 / T001 王老师(1班) / T002 李老师(2班)
  T003 张双角色(teacher 1班 + researcher 初二) / R001 陈组长(researcher 初二)
  S001~S040 学生（1班 20 人 + 2 班 20 人；S001 是闭环线一的主角）

【幂等策略】
  账号/班级/授课关系：按业务键查重（username / grade+name），已有就复用不重建；
  学习历史：库里已有 student_answers 就跳过（--force 先清后造）；
  虚拟作答：该卷已有 source_type=exam 的作答就跳过（--force 先清后造）。
"""
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models import (
    BKTState,
    Class,
    ErrorLog,
    ExamPaper,
    ExamPaperQuestion,
    Homework,
    HomeworkQuestion,
    KnowledgePoint,
    MasteryRecord,
    Question,
    ResearchLead,
    Student,
    StudentAnswer,
    TeachingAssignment,
    User,
    UserRole,
)

PASSWORD = "Passw0rd1"          # 8~72 位含字母数字，过 validate_password_strength
HASH = hash_password(PASSWORD)  # 全员共用一个哈希，省 N 次 bcrypt（登录验证不受影响）

GRADE = "初二"
SUBJECT = "math"


# =====================================================================
# 一、组织数据：班级 / 用户 / 角色 / 授课关系 / 任命 / 学生档案
# =====================================================================
def ensure_org(session, n_students: int) -> dict:
    """建演示组织（幂等）。返回 {username: user_id} 供后续步骤引用。

    要做的操作：
      t1 两个班级（初二1班/2班）按 (grade,name) 查重
      t2 六类账号按 username 查重，新用户才建（含 user_roles）
      t3 授课关系 / 任命按复合键查重
      t4 学生档案按 user_id 查重，新档案才建（S001~Sn 均分到两个班）
    """
    ids: dict[str, int] = {}

    # ---- t1 班级 ----
    classes: dict[str, Class] = {}
    for name in ("1班", "2班"):
        row = session.execute(
            select(Class).where(Class.grade == GRADE, Class.name == name)
        ).scalar_one_or_none()
        if row is None:
            row = Class(grade=GRADE, name=name)
            session.add(row)
            session.flush()                      # 拿自增 id
            print(f"[org] 建班级 {GRADE}{name} -> id={row.id}")
        classes[name] = row

    # ---- t2 账号 ----
    # (username, name, roles, 班名 or None)：None = 不是学生
    accounts = [
        ("A001", "管理员", ["admin"], None),
        ("T001", "王老师", ["teacher"], None),
        ("T002", "李老师", ["teacher"], None),
        ("T003", "张双角色", ["teacher", "researcher"], None),
        ("R001", "陈组长", ["researcher"], None),
    ]
    for i in range(1, n_students + 1):
        cls = "1班" if i <= (n_students + 1) // 2 else "2班"
        accounts.append((f"S{i:03d}", f"演示学生{i:02d}", ["student"], cls))

    for username, name, roles, cls in accounts:
        user = session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if user is None:
            user = User(username=username, password_hash=HASH, name=name)
            session.add(user)
            session.flush()
            for r in roles:
                session.add(UserRole(user_id=user.id, role=r))
            print(f"[org] 建账号 {username}({name}) roles={roles} -> id={user.id}")
        ids[username] = user.id

        if cls is not None:
            has_profile = session.execute(
                select(Student).where(Student.user_id == user.id)
            ).scalar_one_or_none()
            if has_profile is None:
                session.add(Student(user_id=user.id, class_id=classes[cls].id,
                                    student_no=f"2026{user.id:04d}"))
                print(f"[org]   └ 档案挂到 {GRADE}{cls}")

    # ---- t3 授课关系 / 任命 ----
    assignments = [("T001", "1班"), ("T002", "2班"), ("T003", "1班")]
    for username, cls in assignments:
        dup = session.execute(
            select(TeachingAssignment).where(
                TeachingAssignment.teacher_id == ids[username],
                TeachingAssignment.class_id == classes[cls].id,
                TeachingAssignment.subject == SUBJECT)
        ).scalar_one_or_none()
        if dup is None:
            session.add(TeachingAssignment(teacher_id=ids[username],
                                           class_id=classes[cls].id, subject=SUBJECT))
            print(f"[org] 建授课关系 {username} -> {GRADE}{cls}")

    for username in ("R001", "T003"):
        dup = session.execute(
            select(ResearchLead).where(ResearchLead.user_id == ids[username],
                                       ResearchLead.grade == GRADE,
                                       ResearchLead.subject == SUBJECT)
        ).scalar_one_or_none()
        if dup is None:
            session.add(ResearchLead(user_id=ids[username], grade=GRADE, subject=SUBJECT))
            print(f"[org] 建任命 {username} -> 教研组长({GRADE}×{SUBJECT})")

    session.commit()
    return ids


# =====================================================================
# 二、学习历史：BKT 基线 + 演示作业 + 错题作答 + 错因归因
# =====================================================================
def ensure_learning_history(session, ids: dict, seed: int) -> None:
    """造闭环线一需要的"错题历史"与雷达图需要的 BKT 基线（幂等）。

    数据形态（n=40 人、题库取 5 道不同知识点的题）：
      bkt_state        40 人 × 全部知识点，每人每点一行 p_known∈[0.05,0.95]
      homeworks        每班一份"演示历史作业"
      student_answers  弱半区（含 S001）5 题全错，强半区全对 —— S-10 的错题就从这来
      error_logs       每条错答挂一条错因（四类轮转，带 evidence）
    为什么弱半区"全错"这么粗暴：演示数据要的是"错题热点稳定可预期"，
      S001 永远错在同 5 个知识点上，S-10 生成的测验目标点才可复现。
    """
    existing = session.execute(select(StudentAnswer.id).limit(1)).scalar_one_or_none()
    if existing is not None:
        print("[hist] 学习历史已存在（student_answers 非空），跳过；--force 可重造")
        return

    rnd = random.Random(seed)
    kps = session.execute(select(KnowledgePoint).order_by(KnowledgePoint.id)).scalars().all()
    if not kps:
        print("[hist] ⚠️ 知识点表为空：请先跑 scripts/seed_question_bank.py")
        return

    # ---- BKT 基线：每人每点一行 ----
    students = session.execute(select(Student).order_by(Student.id)).scalars().all()
    n = len(students)
    for rank, stu in enumerate(students):
        ability = rank / max(n - 1, 1)                 # 0=最弱 … 1=最强
        for kp in kps:
            # 0.6 权重看能力、0.4 权重看知识点个人差异 → 雷达图有起伏才好看
            p = 0.05 + 0.90 * (0.6 * ability + 0.4 * rnd.random())
            session.add(BKTState(student_id=stu.id, kp_id=kp.id, p_known=round(p, 3)))
            session.add(MasteryRecord(student_id=stu.id, kp_id=kp.id,
                                      mastery_value=round(p, 3)))
    print(f"[hist] BKT 基线：{n} 人 × {len(kps)} 点 = {n * len(kps)} 行")

    # ---- 演示作业 + 作答：每班一份，题目取题库前 5 个不同知识点 ----
    picked: list[Question] = []
    seen_kp: set[str] = set()
    for q in session.execute(select(Question).order_by(Question.id)).scalars().all():
        if q.kp_id not in seen_kp:
            picked.append(q)
            seen_kp.add(q.kp_id)
        if len(picked) == 5:
            break

    error_cycle = ["概念不清", "方法不会", "粗心", "审题失误"]
    for cls_name, teacher in (("1班", "T001"), ("2班", "T002")):
        cls_id = session.execute(
            select(Class.id).where(Class.grade == GRADE, Class.name == cls_name)
        ).scalar_one()
        hw = Homework(teacher_id=ids[teacher], class_id=cls_id, title="演示历史作业",
                      kp_ids=json.dumps([q.kp_id for q in picked], ensure_ascii=False))
        session.add(hw)
        session.flush()
        for q in picked:
            session.add(HomeworkQuestion(homework_id=hw.id, question_id=q.id, score=5.0))

        members = [s for s in students if s.class_id == cls_id]
        weak_half = set(sorted(members, key=lambda s: s.id)[: max(1, len(members) // 2)])
        wrong_n = 0
        for stu in members:
            for j, q in enumerate(picked):
                wrong = stu in weak_half          # 弱半区全错、强半区全对（确定性）
                ans = StudentAnswer(source_type="homework", source_id=hw.id,
                                    question_id=q.id, student_id=stu.id,
                                    content="（演示作答）" + ("解题过程不完整" if wrong else "解：略，结果正确"),
                                    is_correct=not wrong,
                                    score=0.0 if wrong else 5.0)
                session.add(ans)
                session.flush()
                if wrong:
                    wrong_n += 1
                    session.add(ErrorLog(
                        answer_id=ans.id,
                        error_type=error_cycle[(stu.id + j) % 4],   # 四类错因轮转
                        confidence=0.85,
                        evidence=f"（演示归因）{stu.student_no or stu.id} 在「{q.content[:16]}」上的作答显示概念不清",
                        explanation_strategy="先复习前置知识点，再回到本题关键步骤",
                    ))
        print(f"[hist] {cls_name} 演示作业：{len(members)} 人 × {len(picked)} 题，错答 {wrong_n} 条")

    session.commit()
    print("[hist] 提示：S001 的近 30 天错题已就绪 → 学生端点『生成测验』即可走闭环线一")


# =====================================================================
# 三、虚拟班级作答：给指定试卷合成 P/D 布局确定的作答
# =====================================================================
def simulate_exam_answers(session, paper_id: int, seed: int, force: bool) -> None:
    """给试卷合成全班作答（闭环线二的"虚拟班级作答"一步）。

    P/D 布局（按题目在卷内的卷面序号 i，能力序 r=0 最弱 … n-1 最强，
              f=该题卷面真实分值，来自 exam_paper_questions.score）：
      i=0 过难：只有前 25%（强生）得分   → P=0.25，D=1.0 → 异常[过难]
      i=1 过易：最弱 k 人只拿步骤分      → P≈0.92，D≈0.30 → 异常[过易]
      i=2 区分度不足：人人得一半步骤分   → P=0.50，D=0.00 → 异常[区分度不足]
      i≥3 正常：强半区得分弱半区不得分   → P≈0.55，D=1.00 → 不异常
    为什么用手搓而不是随机抽样：随机抽样下小样本 P/D 会抖，
      演示时"第 1 题过难、第 2 题过易"必须每次跑都成立——闭环要可复现。

    【过易题为什么给"步骤分"而不是纯 0 分】
      要同时满足 P>0.9（≤10% 的失分）和 D≥0.2（高低分组差得开），
      纯 0 分方案在 n<30 时无整数解（会卡在 0.9/0.2 的浮点边界上，
      1.0-0.8=0.19999…<0.2 把"区分度不足"也带出来）。改用连续步骤分：
      设有效失分量 u=0.08×n（0.08n∈[0.0675n, 0.1n) 恒成立），
      最弱 k 人各拿 m=1-u/k 的步骤分，P=1-0.08=0.92、D=u/n_tail≈0.30，
      任意人数都干净落在"只报过易"的区间里。
    """
    paper = session.get(ExamPaper, paper_id)
    if paper is None:
        raise SystemExit(f"[FAIL] 试卷 {paper_id} 不存在，先在教研端出卷（POST /api/research/exam/generate）")

    q_ids = (paper.quota_plan_json or {}).get("question_ids") or []
    if not q_ids:
        raise SystemExit(f"[FAIL] 试卷 {paper_id} 的 quota_plan_json 里没有 question_ids，无法合成作答")

    # 每题满分 + 卷面顺序：以 exam_paper_questions（定稿时固化）为准。
    #   为什么要读真值：R-04 的 P = 均分 ÷ 该题满分，均摊公式会把
    #   2.5 分的选择题当成 5.56 分、11 分的解答题当成 5.56 分，
    #   "第 1 题过难 / 第 2 题过易"这套布局就全歪了 —— 脚本必须和 analyze 同口径。
    #   未定稿的卷没有卷面表 → 退回均摊 + quota_plan 里的顺序（只为老卷兜底）。
    paper_qs = session.execute(
        select(ExamPaperQuestion.question_id, ExamPaperQuestion.score)
        .where(ExamPaperQuestion.paper_id == paper_id)
        .order_by(ExamPaperQuestion.seq)
    ).all()
    fallback_full = 100.0 / len(q_ids)
    if paper_qs:
        q_ids = [qid for qid, _ in paper_qs]
        score_map = {qid: float(sc or 0) for qid, sc in paper_qs}
    else:
        score_map = {qid: fallback_full for qid in q_ids}
        print(f"[exam] 提示：试卷 {paper_id} 尚未定稿（无卷面分值），按 100/{len(q_ids)} 均摊合成")

    existing = session.execute(
        select(StudentAnswer.id)
        .where(StudentAnswer.source_type == "exam", StudentAnswer.source_id == paper_id)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        if not force:
            print(f"[exam] 试卷 {paper_id} 已有虚拟作答，跳过（--force 删掉重造）")
            return
        deleted = session.query(StudentAnswer).filter(
            StudentAnswer.source_type == "exam", StudentAnswer.source_id == paper_id
        ).delete()
        session.commit()
        print(f"[exam] --force：清掉旧作答 {deleted} 条")

    students = session.execute(select(Student).order_by(Student.id)).scalars().all()
    n = len(students)
    if n < 4:
        raise SystemExit(f"[FAIL] 学生人数 {n} 太少（至少 4 人），先跑一遍本脚本不带 --paper-id")

    n_tail = max(1, int(n * 0.27))       # 前后 27% 分组（analysis.py 同口径）
    hard_cut = int(n * 0.75)             # 过难题：r >= 75% 才得分
    # 过易题：最弱 k 人拿步骤分 m（见 docstring 的 u=0.08n 推导）
    k_easy_miss = max(2, round(0.075 * n))
    miss_ratio = max(0.0, 1.0 - (0.08 * n) / k_easy_miss)

    anomaly_plan = {}                    # q_id -> 预期标签（打印给操作者核对）
    inserted = 0
    for rank, stu in enumerate(students):
        for i, qid in enumerate(q_ids):
            f = score_map[qid]                            # 该题真实满分（不是均摊值）
            if i == 0:                                   # 过难
                score = f if rank >= hard_cut else 0.0
                anomaly_plan.setdefault(qid, "过难")
            elif i == 1:                                 # 过易
                score = f * miss_ratio if rank < k_easy_miss else f
                anomaly_plan.setdefault(qid, "过易")
            elif i == 2:                                 # 区分度不足（人人步骤分）
                score = f * 0.5
                anomaly_plan.setdefault(qid, "区分度不足")
            else:                                        # 正常题
                threshold = int(n * 0.45) + (i % 5) - 2  # 少量错落，P 约 0.5~0.65
                score = f if rank >= max(n_tail + 1, min(threshold, n - n_tail - 1)) else 0.0

            session.add(StudentAnswer(
                source_type="exam", source_id=paper_id,
                question_id=qid, student_id=stu.id,
                content=f"（虚拟作答·能力序{rank}）" + ("得满分" if score == f else
                                                  "步骤分" if score > 0 else "未得分"),
                is_correct=(score == f),
                score=round(score, 2),
            ))
            inserted += 1

    session.commit()
    print(f"[exam] 试卷 {paper_id}：{n} 人 × {len(q_ids)} 题 = {inserted} 条虚拟作答已写入")
    print(f"[exam] 预期异常题（analyze 后应写回 quality_flag=abnormal）：")
    for qid, label in anomaly_plan.items():
        print(f"[exam]   题 {qid} -> {label}")
    print(f"[exam] 下一步：教研端 POST /api/research/paper/{paper_id}/analyze 跑 R-04，")
    print(f"[exam]        再出一次卷看 abnormal 题被降权（落位见出卷返回的配额表 review）")


# =====================================================================
# 入口
# =====================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="HHT棱镜课堂 演示数据生成")
    parser.add_argument("--paper-id", type=int, default=None,
                        help="给指定试卷合成虚拟班级作答（闭环线二）")
    parser.add_argument("--students", type=int, default=40, help="学生人数（默认 40）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（BKT 基线用）")
    parser.add_argument("--force", action="store_true", help="已有数据先清后造（只清作答类）")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        ids = ensure_org(session, args.students)

        if args.force and args.paper_id is None:
            # --force 不带 --paper-id = 学习历史重造：只清学习过程表，账号保留
            for model in (ErrorLog, StudentAnswer, HomeworkQuestion, Homework,
                          MasteryRecord, BKTState):
                session.query(model).delete()
            session.commit()
            print("[hist] --force：学习历史已清空，重造中")

        ensure_learning_history(session, ids, args.seed)

        if args.paper_id is not None:
            simulate_exam_answers(session, args.paper_id, args.seed, args.force)

        print("\n[OK] 演示数据就绪。登录密码统一：", PASSWORD)
    except Exception:
        session.rollback()
        print("[FAIL] 已回滚，不留半批数据")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
