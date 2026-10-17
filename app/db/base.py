"""SQLAlchemy declarative Base：所有模型继承它，Alembic 靠它的 metadata 自动建表。

（SQLAlchemy 2.0 写法，本项目 20 张表全用它）：

1) DeclarativeBase —— "声明式基类"基座
   是啥：SQLAlchemy 2.0 的官方基类。继承它写一个空类，这个类就自带两样东西：
        ① registry（登记处）：所有继承 Base 的模型类自动注册进来；
        ② metadata（表结构清单）：登记时把"表名/列名/类型/索引"全记到 Base.metadata。

   干啥用：Alembic 建迁移、create_all 建表，读的都是 Base.metadata
          所以每个模型文件必须 import 本文件，否则那张表就是"黑户"，建表时被漏掉。

   补充：Alembic = 表结构的 git，给每一版表结构生成一个 "版本快照";
        要执行 `uv run Alembic revision --autogenerate -m "描述" ` -> 生成迁移脚本，“把库往模型的样子搬”

          
2) Mapped[类型] —— 列的"类型声明"（Python 3 的类型注解直接当建表信息用）
   是啥：`id: Mapped[int]` 表示这一列在 Python 侧是 int；
        `Mapped[str | None]` 的 `| None` 表示"可空"，数据库里生成 NULL 列；
        不写 `| None` 就是 NOT NULL
   干啥用：一个注解同时干三件事——建表时的列类型、读出来的 Python 类型、
          IDE 补全和类型检查提示。写错类型启动时就能暴露。

          
3) mapped_column(类型, 参数...) —— 列的"施工参数"
   是啥：往 Mapped 注解上附加建表细节的函数。
   常用参数（后面模型文件里反复出现，这里一次讲全）：
     primary_key=True     主键；
     autoincrement=True 整数主键自增（不写默认就自增）
     ForeignKey("表.列")   外键，指向别的表；
     ondelete="CASCADE"   父行删除时子行跟着删
     String(64) / Text    变长字符串（必须给最大长度）/ 不限长大文本
     index=True           建索引（常查询/常过滤的列才加，加多了拖慢写入）
     unique=True          唯一约束，插重复值直接报错
     default=xxx          Python 侧默认值：insert 时由 SQLAlchemy 填
     server_default=func.now()  数据库侧默认值：建表语句里写 DEFAULT now()，
                          连用 psql 手插行也会自动填时间——和 default 的本质区别
     onupdate=func.now()  每次 UPDATE 时数据库自动刷新该列（"最后更新时间"专用）
     comment="..."        列注释，会写进 PG 的表结构注释里

     
4) relationship(...) —— 表之间的"对象级跳转"（模型文件里首次出现时详讲）
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    空类：继承 DeclarativeBase，纯粹为了让全部模型有共同的登记处registry 和 metadata表结构清单。
    """

    pass
