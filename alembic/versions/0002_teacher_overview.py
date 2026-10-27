"""0002：teacher_overview 视图——教师信息全貌（打开即看，零同步成本）。

背景：
  teachers 表只存职称/入职时间两栏档案，教师的账号(users)、角色(user_roles)、
  任教(teaching_assignments)、教研任命(research_leads) 散在四张表，DBeaver 里看不到全貌。
  本迁移建一个只读视图，把它们拼成"一行一师"：

    seq         账户展示序号（与前端用户列表连号同口径：按 users.id 排序的 1-based 位置）
    user_id     users.id（物理主键，接口调用用它）
    username / name / is_active   登录名 / 姓名 / 启用状态
    title / joined_at             职称 / 入职时间（teachers 静态档案，可空）
    roles       该账号全部角色（逗号分隔，如 teacher,researcher）
    teaching    任教清单（年级班名:学科，多条竖线分隔，如 初三1班:math | 初三2班:math）
    leads       教研任命（年级:学科，如 初三:math）

  视图不存数据，每次查询现场 JOIN 现算，源头改了它立刻跟着变。
  往 teachers 里物理搬 username/name 这些列会变成"处处要同步"的冗余数据，
  改个姓名就可能留下脏行，所以全貌只拼不搬。

行范围：带 teacher 角色的账号（与 teachers 档案行集合一致）。

应用：uv run alembic upgrade head
      （若报错说 0001 检测到已有表，说明库是脚本建的没打迁移戳：
        先 uv run alembic stamp 0001_initial 再 upgrade head）
回滚：uv run alembic downgrade -1（只删视图，不动任何表）
"""

from alembic import op

revision = "0002_teacher_overview"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


# PostgreSQL 语法：string_agg / concat / 窗口函数。sqlite 测试库不需要这张视图。
_VIEW_SQL = """
CREATE OR REPLACE VIEW teacher_overview AS
SELECT
    (SELECT COUNT(*) FROM users u2 WHERE u2.id <= u.id) AS seq,
    u.id          AS user_id,
    u.username    AS username,
    u.name        AS name,
    u.is_active   AS is_active,
    t.title       AS title,
    t.joined_at   AS joined_at,
    COALESCE(r.roles,     '') AS roles,
    COALESCE(ta.teaching, '') AS teaching,
    COALESCE(ld.leads,    '') AS leads
FROM users u
LEFT JOIN teachers t ON t.user_id = u.id
LEFT JOIN (
    SELECT user_id, string_agg(role, ',' ORDER BY role) AS roles
    FROM user_roles GROUP BY user_id
) r ON r.user_id = u.id
LEFT JOIN (
    SELECT a.teacher_id,
           string_agg(concat(c.grade, c.name, ':', a.subject), ' | '
                      ORDER BY c.grade, c.name, a.subject) AS teaching
    FROM teaching_assignments a
    JOIN classes c ON c.id = a.class_id
    GROUP BY a.teacher_id
) ta ON ta.teacher_id = u.id
LEFT JOIN (
    SELECT user_id,
           string_agg(concat(grade, ':', subject), ' | '
                      ORDER BY grade, subject) AS leads
    FROM research_leads GROUP BY user_id
) ld ON ld.user_id = u.id
WHERE u.id IN (SELECT user_id FROM user_roles WHERE role = 'teacher')
"""


def upgrade() -> None:
    op.execute(_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS teacher_overview")
