"""内容域三张表：chapters / knowledge_points / questions。

 得到的数据：章节、知识点（与 Neo4j 节点共用同一套 String id）、题库
 关键设计：chapter_id / kp_id 用业务可读字符串主键（如 ch_quadratic / kp_crossmult），
          与 Neo4j 里 Chapter / KnowledgePoint 节点的 id 属性一一对应，双写不迷路。
          quality_flag 由 R-04 写回（normal/abnormal/disabled）、R-08 消费。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="业务 id，如 ch_quadratic")
    name: Mapped[str] = mapped_column(String(128), comment="章节名，如 一元二次方程")
    subject: Mapped[str] = mapped_column(String(32), comment="学科，如 math", index=True)
    grade: Mapped[str] = mapped_column(String(16), comment="年级，如 初三")
    order: Mapped[int] = mapped_column(Integer, default=0, comment="章节顺序")
    # server_default=func.now() 的机制详见 db/base.py 第 3 点：数据库侧默认值，
    # 建表语句里写 DEFAULT now()，手插行也会自动填
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgePoint(Base):
    __tablename__ = "knowledge_points"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="业务 id，如 kp_crossmult")
    name: Mapped[str] = mapped_column(String(128), comment="知识点名，如 十字相乘法")
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    difficulty: Mapped[float] = mapped_column(Float, default=0.5, comment="固有权重 0~1，非掌握度")
    description: Mapped[str | None] = mapped_column(Text, comment="一句话说明")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(32), default="math", index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    kp_id: Mapped[str] = mapped_column(ForeignKey("knowledge_points.id"), index=True)
    # question_type ∈ {choice, fill, subjective}
    question_type: Mapped[str] = mapped_column(String(16), comment="choice/fill/subjective")
    difficulty: Mapped[str] = mapped_column(String(8), comment="easy/medium/hard")
    content: Mapped[str] = mapped_column(Text, comment="题干")
    answer: Mapped[str] = mapped_column(Text, comment="标准答案（选择题含选项字母）")
    # 陌生点 Text vs String(N)：Text 不限长度（题目解析可能几千字），String 必须给 N
    #   且超出会报错。短枚举/短名用 String（可索引），长文本一律 Text
    options: Mapped[str | None] = mapped_column(Text, comment="选择题选项，JSON 数组字符串，如 [\"A. ..\",\"B. ..\"]")
    analysis: Mapped[str | None] = mapped_column(Text, comment="解析")
    source: Mapped[str | None] = mapped_column(String(128), comment="出处，如 人教版九上 P25 例1")
    # quality_flag ∈ {normal, abnormal, disabled}：R-04 写回、R-08 选题时降权/剔除
    quality_flag: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
