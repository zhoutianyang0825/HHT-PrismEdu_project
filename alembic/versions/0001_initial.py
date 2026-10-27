"""首版迁移：创建全部 20 张业务表。

首次：
    执行 uv run alembic upgrade head，其实执行的是0001_xxx.py;
    0001_xxx.py没有用 uv run alembic revision --autogenerate -m "init" 直接生成，
    而是手写 upgrade() 和  downgrade()方法，用creat_all 直接读取 Base.metadata，一次性建20张表。

第二次及以后：
    都用 uv run alembic revision --autogenerate -m "..." 对比metadata vs 库现状，生成迁移脚本；
    然后人工过目生成的脚本；
    脚本确认ok后，执行 uv run alembic upgrade head，库即可追平 metadata
    
"""

from alembic import op

# revision / down_revision：迁移文件的"版本链"，Alembic 靠这两个变量，把所有迁移脚本，串成一条链
# 首版 down_revision = None（链条起点），以后每个新迁移的 down_revision 填：上一个迁移脚本的 revision 号，upgrade head 就按 版本链 顺序执行
revision = "0001_initial"
down_revision = None    # 链的起点/前置版本

branch_labels = None
depends_on = None


def _table_names(bind) -> set[str]:
    """
    获取当前库里 已存在的表名集合

    陌生点 inspect()：SQLAlchemy 的"数据库探针"，不写一条 SQL 就能问库：有哪些表/列/索引。这里用来做空库检查；
    
    """
    from sqlalchemy import inspect
    insp = inspect(bind)
    return set(insp.get_table_names())


def upgrade() -> None:
    """ upgrade（）： 前置检查，库里有表则报错退出，防止在半初始化的库上误跑 """

    from app.models import Base

    # op.get_bind() 是 Alembic 递给迁移脚本用的"当前数据库连接"，探针和建表都得拿它去操作。
    # 先连数据库，再利用inspect（）探针，捞数据库已存在的表
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
