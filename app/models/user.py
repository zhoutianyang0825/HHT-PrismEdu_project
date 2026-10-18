"""用户域三张表：users / user_roles / teachers。

 得到的数据：User（账号本体，不存 role）、UserRole（多对多角色）、Teacher（教师静态档案）
 关键设计：v1.5 定稿——角色不在 users 表上，一人多角色靠 user_roles 展开，
          JWT 签发时查这张表拼 roles: [...] 数组

          
先用 Python 类构建表结构，
import Base，并继承 Base 基类时，将 表结构 注册registry 进 Base.metadata 表结构清单中，
然后，调 SQLAlchemy 把清单里的表结构描述，现场翻译成目标数据库的 SQL 执行，生成一张张实体的 table，
最后，数据由 seed 脚本另行灌入
    
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# 表1
class User(Base):
    __tablename__ = "users"

    # primary_key=True ： 表的主键
    # autoincrement=True ： 自增长
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # index=True：给这列建 B-tree 索引。查询 WHERE username=... 走索引
    #   （毫秒级），不建则全表扫描（数据多了秒级）。代价：每次 INSERT 多维护一份索引
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, comment="登录名，如 R003")

    password_hash: Mapped[str] = mapped_column(String(128), comment="bcrypt 哈希，绝不存明文")
    name: Mapped[str] = mapped_column(String(64), comment="真实姓名")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="停用则为False，保留历史作答数据") # 停用/启用，类似软删除
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


    # relationship：SQLAlchemy 的"表间跳转"；表示：user 对象身上挂一个 .roles属性，访问时自动去 user_roles 表查
    #   back_populates="user"：指向双向声明的另一头 UserRole.user
    #   cascade="all, delete-orphan"：级联规则。删 User 时它名下的 UserRole 行自动跟着删
    roles: Mapped[list["UserRole"]] = relationship(back_populates="user", cascade="all, delete-orphan")


# 表2
class UserRole(Base):
    __tablename__ = "user_roles"

    # 陌生点 UniqueConstraint：复合唯一约束，这里是 (user_id, role) 两列组合唯一，“同一个人 + 同一个角色” 不能出现两次
    #   但不同人可以有同角色、同一人也可以有多种角色。name= 是约束名，报错时好认
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 陌生点 ondelete="CASCADE"：数据库层的级联删除——users 表一行被删时，
    #   PG 自动删掉这里所有指向它的行。注意它写在 ForeignKey 里（数据库行为），
    #   和 relationship 的 cascade（Python/SQLAlchemy 行为）是两码事，都配才稳
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    # role ∈ {student, teacher, researcher, admin}，用 String+应用层校验而非 PG 枚举，迁移省事
    role: Mapped[str] = mapped_column(String(32), comment="student/teacher/researcher/admin")


    # relationship：SQLAlchemy 的"表间跳转"；
        #   表示：UserRole 对象身上挂一个 .user 属性，访问时自动去 User 表查
    
    # back_populates="roles"：指向双向声明的另一头 User.user
    user: Mapped[User] = relationship(back_populates="roles")


# 表3
class Teacher(Base):
    __tablename__ = "teachers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    title: Mapped[str | None] = mapped_column(String(64), comment="职称等静态信息")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 注意：班级与学科关系一律看 teaching_assignments，这里只放静态档案
