"""显示名回捞：kp_id → 知识点名，chapter_id → 章节名，kp_id → 所属 chapter_id。

为什么单独一个文件：
    出卷配额表的「章节」列、抽题缺口里的知识点前缀、前端年级/章节下拉，三处都要名字。
    这些是"课程元数据"，不是权限也不是业务规则，塞进 selection_engine（策略纯函数）
    或 research.py（接口层）都不合适，单独一层最干净。

★ 为什么必须从库里回捞、不能在前端硬编码：
    知识图谱重构后 chapter_id 换过一轮（旧 ch_quadratic → 新 ch21_quad_eq），
    前端 kp-names.js 里那套静态表已经过期，硬编码必然对不上。
    库是唯一真源，前端只负责显示。
"""
from sqlalchemy import select

# ★ 不用 `from app.db.session import get_session_factory`：那样会把函数对象在导入时绑死，
#   测试里 monkeypatch("app.db.session.get_session_factory") 就换不掉了（会去连真 PG）。
#   走模块引用，调用时才查属性，沙盒才换得动。
from app.db import session as db_session
from app.models import Chapter, KnowledgePoint


def fetch_kp_names(kp_ids: list[str]) -> dict[str, str]:
    """批量回捞知识点名。

    得到的数据：kp_ids=["kp_peifang", "kp_crossmult"]（传 [] 直接返回 {}，避免非法 IN ()）
    要做的操作：一条 IN 查询，只取 id/name
    返回：{"kp_peifang": "配方法", "kp_crossmult": "十字相乘法"}
          ★ 库里没有的 id 不会出现在结果里——调用方要自己兜底（说明这个 kp 是编的）
    """
    if not kp_ids:
        return {}

    session = db_session.get_session_factory()()
    try:
        rows = session.execute(
            select(KnowledgePoint.id, KnowledgePoint.name)
            .where(KnowledgePoint.id.in_(list(kp_ids)))
        ).all()

    finally:
        session.close()

    return {kp_id: name for kp_id, name in rows}


def fetch_chapter_names(subject: str | None = None, grade: str | None = None) -> dict[str, str]:
    """回捞章节名（可按学科/年级过滤）。

    得到的数据：subject="math"；grade="初三"（都可空，空 = 不过滤）
    要做的操作：按条件查 chapters，只取 id/name
    返回：{"ch_quadratic": "第21章 一元二次方程", "ch_trig": "第28章 锐角三角函数"}
    """
    session = db_session.get_session_factory()()
    try:
        stmt = select(Chapter.id, Chapter.name)
        if subject:
            stmt = stmt.where(Chapter.subject == subject)
        if grade:
            stmt = stmt.where(Chapter.grade == grade)

        rows = session.execute(stmt.order_by(Chapter.id)).all()

    finally:
        session.close()

    return {ch_id: name for ch_id, name in rows}


def fetch_kp_chapter_map(kp_ids: list[str]) -> dict[str, str]:
    """回捞"每个知识点真实属于哪个章节"。

    得到的数据：kp_ids=["kp_peifang", "kp_sqrt"]
    要做的操作：查 knowledge_points.chapter_id（知识点表里本来就有归属章节）
    返回：{"kp_peifang": "ch_quadratic"}
          ★ kp_sqrt 这类库里没有的点不会出现——正好拿来识别 LLM 编的 kp_id

    为什么要有这个：命题规划师只该决定"考哪些知识点"，章节归属是数据事实。
    让它自己编 chapter_id，结果就是 ch_21_quad_eq 这种库里不存在的值，
    章节覆盖率统计直接失真（covered 集合跟真实章节对不上）。
    """
    if not kp_ids:
        return {}

    session = db_session.get_session_factory()()
    try:
        rows = session.execute(
            select(KnowledgePoint.id, KnowledgePoint.chapter_id)
            .where(KnowledgePoint.id.in_(list(kp_ids)))
        ).all()

    finally:
        session.close()

    return {kp_id: chapter_id for kp_id, chapter_id in rows}


def fetch_naming(subject: str = "math", grade: str | None = None) -> dict:
    """一次拿全展示层需要的名字映射（前端一个请求搞定）。

    得到的数据：subject="math"；grade="初三"
    要做的操作：章节全量 + 该学科知识点全量
    返回：{"chapter": {"ch_quadratic": "第21章 一元二次方程"},
           "kp": {"kp_peifang": "配方法"}}

    为什么：配额表里的 chapter_id 和缺口文案里的 kp_id 都要翻译，
    一次拉全比"按需逐个查"少 N 次往返，而且前端可以缓存整个会话。
    """
    session = db_session.get_session_factory()()
    try:
        stmt = select(KnowledgePoint.id, KnowledgePoint.name).where(KnowledgePoint.subject == subject)
        kp_rows = session.execute(stmt).all()

    finally:
        session.close()

    return {
        "chapter": fetch_chapter_names(subject=subject, grade=grade),
        "kp": {kp_id: name for kp_id, name in kp_rows},
    }


def fetch_chapter_catalog(subject: str = "math", grade: str | None = None) -> list[dict]:
    """按"章节 → 下辖知识点"组织一份课程目录（喂给命题规划师当白名单）。

    得到的数据：subject="math"；grade="初三"
    要做的操作：一次查出章节 + 其下知识点，按章节分组（章节按 id 升序，知识点按 id 升序）
    返回：
        [{"chapter_id": "ch_quadratic", "chapter_name": "第21章 一元二次方程",
          "kp": [{"kp_id": "kp_crossmult", "name": "十字相乘法"}, ...]},
         {"chapter_id": "ch_rotation", "chapter_name": "第23章 旋转", "kp": []}]

    为什么：
        LLM 拿到"第21章 一元二次方程"这个中文名，会自己造一个 ch_21_quad_eq 当 id，
        而库里叫 ch_quadratic —— 配额表的章节列因此显示成一串谁都不认识的编码。
        给它白名单 + 明确"只能照抄"，才是治本；只在展示层翻译是治标。
    """
    session = db_session.get_session_factory()()
    try:
        stmt = select(Chapter.id, Chapter.name).where(Chapter.subject == subject)
        if grade:
            stmt = stmt.where(Chapter.grade == grade)
        chapters = session.execute(stmt.order_by(Chapter.id)).all()

        kps = session.execute(
            select(KnowledgePoint.id, KnowledgePoint.name, KnowledgePoint.chapter_id)
            .where(KnowledgePoint.subject == subject)
            .order_by(KnowledgePoint.id)
        ).all()

    finally:
        session.close()

    by_chapter: dict[str, list[dict]] = {}
    for kp_id, name, chapter_id in kps:
        by_chapter.setdefault(chapter_id, []).append({"kp_id": kp_id, "name": name})

    return [
        {
            "chapter_id": ch_id,
            "chapter_name": ch_name,
            # 挂不到任何章节的知识点单独归到 None 键下，不丢（丢了下游会当成"不存在的点"）
            "kp": by_chapter.get(ch_id, []),
        }
        for ch_id, ch_name in chapters
    ] + ([{"chapter_id": "unknown", "chapter_name": "（未归属章节）",
           "kp": by_chapter[None]}] if None in by_chapter else [])


def display_name(id_value: str | None, names: dict[str, str], fallback: str = "未知") -> str:
    """把 id 翻成人话，翻不出来时给出可读的兜底而不是裸露的 id。

    得到的数据：id_value="ch_21_quad_eq"（库里没有）；names={"ch_quadratic": "第21章 …"}
    要做的操作：查表，命中就返回名字
    返回：命中 → "第21章 一元二次方程"；未命中 → "未知（ch_21_quad_eq）"
          ★ 兜底必须带上原 id，否则组长看到"未知"无从排查
    """
    if not id_value:
        return fallback

    return names.get(id_value) or f"{fallback}（{id_value}）"
