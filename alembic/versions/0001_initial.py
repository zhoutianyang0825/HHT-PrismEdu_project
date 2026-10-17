"""首版迁移：创建全部 20 张业务表。

 实现方式说明（review 时可以讨论的点）：
 用 Base.metadata.create_all / drop_all 生成——因为这是"从零到一"的首版，
 表结构定义已经 100% 收敛在 app/models/ 里，迁移脚本只是触发器；
 后续增量改动（M3 加字段等）改用 `alembic revision --autogenerate` 生成标准 diff，
 那时才需要手写 op.add_column。
 upgrade 前置检查：库里有表则报错退出，防止在半初始化的库上误跑造成困惑。
"""
from alembic import op

# 陌生点 revision / down_revision：迁移文件的"版本链"——Alembic 靠这两个变量
#   把所有迁移串成一条链。首版 down_revision=None（链条起点）；以后每个新迁移
#   的 down_revision 填上一个迁移的 revision 号，upgrade head 就按链顺序执行
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _table_names(bind) -> set[str]:
    """拿当前库里已存在的表名集合（检查器无关：同时兼容 PG / SQLite）。

    陌生点 inspect()：SQLAlchemy 的"数据库探针"——不写一条 SQL 就能问库：
    有哪些表/列/索引。这里用来做空库检查；op.get_bind() 是 Alembic 递给
    迁移脚本用的"当前数据库连接"，探针和建表都得拿它去操作。
    """
    from sqlalchemy import inspect
    insp = inspect(bind)
    return set(insp.get_table_names())


def upgrade() -> None:
    from app.models import Base

    existing = _table_names(op.get_bind())
    # alembic 自己的 alembic_version 表不算业务表，排除掉
    existing.discard("alembic_version")
    if existing:
        raise RuntimeError(
            f"数据库已存在业务表 {sorted(existing)[:5]}...，"
            "首版迁移只允许在空库上执行；如需重建请先 drop 库或手动清表"
        )
    # create_all：把 Base.metadata 里登记的 20 张表一次性建齐（幂等：已存在的不动）
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from app.models import Base

    # drop_all：把 metadata 里的表全删（回退=清库，首版没有更早的版本可回）
    Base.metadata.drop_all(bind=op.get_bind())
