"""M5 · API 层包：五个路由组在此聚合，由 app/main.py 统一挂载。

 文件分工（架构文档 8.1~8.5）：
   auth.py     登录签发多角色 JWT
   student.py  学生端：SSE 引导 / resume / S-10 测验 / 掌握度
   teacher.py  老师端：布置作业 / 触发批改 / 主观题确认（第二个 HITL 场景）
   research.py 教研端：年级聚合 / R-04 分析 / R-08 出卷（聚合通道）
   admin.py    管理后台：用户 / CSV 导入 / 授课关系 / 任命 / 班级 / 日志

 权限总纲：每个路由组的守卫（require_any / require_detail_channel /
 require_aggregate_channel）挂在 router 依赖上——漏挂一个端点就是越权入口。
"""
