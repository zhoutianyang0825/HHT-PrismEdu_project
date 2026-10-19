"""pytest 公共夹具：sqlite 内存库替代 PG（模型全部用可移植类型，单测不需要真容器）。

 scope/BKT 这类纯逻辑单测不该依赖 Docker——跑不起容器的环境也能验证权限逻辑。

 陌生文件 conftest.py：
 pytest 的约定文件名——同目录（及上级）所有以test_xxx命名的py文件，运行前 pytest 会自动 import 它，
 里面的 fixture 供所有测试直接按参数名取用，不需要谁去 import。
 
 改这里的夹具 = 一次改动对所有测试生效。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 保证 tests 既能从项目根跑也能从 tests/ 里跑
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Base, Chapter, KnowledgePoint  

# @pytest.fixture 声明"测试夹具"：
# 测试函数的参数里写 fixture 名（如 def test_xxx(session)），
# pytest 自动先跑这个函数、把 yield 出来的值，注入进去，测试结束后从 yield 处继续执行（收尾清理）。
# 每个测试脚本运行前，拿到的是全新的一份sqlite沙盒，互不污染——这就是函数级隔离
@pytest.fixture()
def session():
    """每个测试一个独立的 sqlite 内存库，用完即弃，互不污染。

    sqlite:///:memory:：连的不是文件是内存 —— 快（无磁盘 IO）且进程结束即蒸发，天生干净。
    Base.metadata.create_all：把 20 张表按模型定义建进这个内存库。
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    maker = sessionmaker(bind=engine, future=True)
    s = maker() # 创建一个session会话

    # 预置 FK 依赖：一章一点（测试里插的题都挂在这两个业务 id 下）
    s.add(Chapter(id="ch_quadratic", name="一元二次方程", subject="math", grade="初三"))
    s.add(KnowledgePoint(id="kp_crossmult", name="十字相乘法", chapter_id="ch_quadratic", subject="math"))
    s.commit()
    yield s          # ← 测试函数在这里拿到 session 开跑

    # ← 测试跑完（无论成败）从这里继续：收尾清理
    s.close()        
    engine.dispose()
