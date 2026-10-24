"""教材入库脚本：data/raw/ 下的 PDF → 切片 → Milvus textbook_math collection。

 用法：
   uv run python scripts/seed_textbook.py                 # 扫 data/raw/*.pdf 逐个入库
   uv run python scripts/seed_textbook.py --demo          # 没有 PDF 时用内置演示文本跑通链路
 说明：演示文本模拟"一元二次方程"两页教材，专门覆盖 M1 完成判据里的检索场景。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymilvus import MilvusClient

from app.core.config import settings
from app.rag.ingest import ingest_textbook_pages, ingest_textbook_pdf
from app.rag.retriever import retrieve

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"

DEMO_PAGES = [
    (
        "一元二次方程：等号两边都是整式，只含有一个未知数，并且未知数的最高次数是2的方程，"
        "叫做一元二次方程。一般形式 ax^2+bx+c=0（a≠0），其中 ax^2 叫二次项，a 叫二次项系数。"
        "解法一：直接开平方法，适用于 (x+m)^2=p（p≥0）的形式。"
        "解法二：配方法，通过加上一次项系数一半的平方，把方程配成完全平方形式。",
        1,
    ),
    (
        "解法三：公式法，当 Δ=b^2-4ac≥0 时，x=(-b±√Δ)/(2a)。"
        "判别式 Δ=b^2-4ac：Δ>0 时方程有两个不相等的实数根；Δ=0 时有两个相等的实数根；"
        "Δ<0 时没有实数根。"
        "解法四：因式分解法，利用 A·B=0 则 A=0 或 B=0，把二次降为一次。"
        "十字相乘法：x^2+px+q=(x+m)(x+n)，其中 m+n=p，mn=q。"
        "根与系数关系（韦达定理）：x1+x2=-b/a，x1·x2=c/a。",
        2,
    ),
]


def main() -> None:
    client = MilvusClient(uri=settings.milvus_uri)
    # glob("*.pdf")：按通配符列出目录下所有 PDF（Path 对象的方法，返回生成器）；
    # sorted() 把它落成有序列表，保证多次运行入库顺序一致
    pdfs = sorted(RAW_DIR.glob("*.pdf"))

    if pdfs:
        total = 0
        for pdf in pdfs:
            n = ingest_textbook_pdf(client, pdf)
            print(f"[OK] {pdf.name} 入库 {n} 个切片")
            total += n
    elif "--demo" in sys.argv:
        total = ingest_textbook_pages(client, DEMO_PAGES, source_name="人教版九上-一元二次方程(演示)")
        print(f"[OK] 演示教材入库 {total} 个切片")
    else:
        print("[WARN] data/raw/ 下没有 PDF。开发期可加 --demo 用内置演示文本先跑通链路")
        sys.exit(0)

    # 落盘：Milvus insert 只进内存缓冲（growing segment），检索能命中但统计=0、
    #   重启可能丢——必须 flush 一次才真正持久化（与 seed_question_bank 结尾同款）
    client.flush(settings.collection_textbook)
    print(f"[OK] textbook_math 已 flush 落盘")

    # 自检：分别用 student / teacher 角色检索，验证可见性与溯源
    for role in ("student", "teacher"):
        hits = retrieve(client, "一元二次方程有哪些解法", role=role, top_k=2)
        print(f"[自检] role={role} top{len(hits)}：")
        for h in hits:
            print(f"   score={h.score:.3f} 出处=《{h.source}》 | {h.text[:36]}...")


if __name__ == "__main__":
    main()
