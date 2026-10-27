"""登录失败计数后端：进程内存 / Redis 两套实现，对外只露三个方法。

【为什么要有这个文件】
  原实现是 security.py 里的一个模块级字典 `_failure_log`：
      {"R003": [1739812345.1, 1739812401.3]}   ← 一串失败时间戳
  单进程部署完全够用，但 `uvicorn --workers 4` 之后每个进程各记各的，
  攻击者只要让请求轮流落到不同 worker，5 次锁定就永远凑不齐 —— 防护等于没有。

【两套后端，一套语义】
  MemoryGuard  ← 原逻辑搬过来（滑动窗口 + 时间戳列表），Redis 挂了就退回它
  RedisGuard   ← ZSET 实现：member=时间戳字符串，score=时间戳
                 ZREMRANGEBYSCORE 剪掉窗口外的 / ZADD 记一笔 / ZCARD 数总数
                 语义和内存版逐条对齐：只数最近 lockout_minutes 分钟内的失败

【为什么用 ZSET 而不是 INCR + EXPIRE】
  ZSET 存的是"每次失败发生在什么时候"，和原来的滑动窗口一模一样，也就是"最近 15 分钟内有 5 次失败才锁"，改一个数字都不会漂。

【降级策略（产品的关键取舍）】
  Redis 抖动时，绝不拒绝登录，只是退回进程内存并打一条 warning：
    - 校园场景峰值 3000 人登录，Redis 挂 10 秒就全校进不来，代价远大于"防护退化"
    - 退化后的水平 = 改造前的水准（单 worker 各记各的），不是"没有防护"
    
  降级有 60 秒冷却（RETRY_SECONDS），期间不再反复去连 Redis，避免每次登录都卡在超时上。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)

# key 必须带命名空间：本机 OrbStack 里的 Redis 是共享实例，别的项目也在用同一个 db0
KEY_PREFIX = "prismedu:login_fail"

# Redis 掉线后的冷却时间：这 60 秒内直接走内存，不去试探连接
RETRY_SECONDS = 60

# 登录是热路径，Redis 再慢也不能把登录拖慢 —— 0.2 秒还没响应就当它挂了
REDIS_TIMEOUT = 0.2

# key 的兜底过期时间：窗口 + 60 秒。即使剪枝逻辑出问题，key 也不会永久堆积
TTL_MARGIN = 60


def _norm(username: str) -> str:
    """用户名归一化：去首尾空格 + 转小写。

    得到的数据：用户输入的 username 原文（可能带空格、可能大小写不同）

    要做的操作：strip + lower

    返回："r003"

    为什么需要：
        Redis 的 key 是精确匹配的字符串，`"R003 "` 和 `"r003"` 会变成两个不同的 key。
        能登录成功的用户名是库里精确匹配的那个（例如 R003），所以这样不会绕过锁定；
        但归一化之后"同一个账号的失败次数"才真的聚在一个 key 上，统计口径更干净。
    """
    return (username or "").strip().lower()


def _window_seconds() -> float:
    """滑动窗口长度（秒）。窗口外的失败记录一律不算数。"""
    return float(settings.lockout_minutes) * 60


def _limit() -> int:
    """窗口内失败多少次触发锁定。"""
    return int(settings.login_max_failures)


# =====================================================================
# 后端一：进程内存（原实现，Redis 不可用时的兜底）
# =====================================================================
class MemoryGuard:
    """进程内存计数：{归一化用户名: [失败时间戳, ...]}。

    得到的数据：username + 当前时间
    要做的操作：追加时间戳 → 剪掉窗口外的 → 数窗口内个数
    返回：register_failed 返回"是否已锁定"，is_locked 返回"当前是否锁定"

    为什么留着它：
        ① Redis 连不上时兜底（降级不退化成"零防护"）；
        ② 单测不依赖任何外部服务，跑得又快又稳。

    为什么要加锁：
        dict/list 的"读-改-写"三步不是原子操作。FastAPI 的同步端点跑在线程池里，
        并发登录时两个线程可能同时读到旧列表再各自写回，丢掉一次失败记录。
        GIL 只保证"单条字节码"不被打断，管不了这种复合操作。
    """

    name = "memory"

    _log: dict[str, list[float]] = {}
    _lock = threading.Lock()

    def register_failed(self, username: str) -> bool:
        key = _norm(username)
        now = time.time()
        window = _window_seconds()
        with self._lock:
            # 滑动窗口：只留 window 秒内的记录，之前的一律丢掉
            stamps = [t for t in self._log.get(key, []) if now - t < window]
            stamps.append(now)
            self._log[key] = stamps
            return len(stamps) >= _limit()

    def is_locked(self, username: str) -> bool:
        key = _norm(username)
        now = time.time()
        window = _window_seconds()
        with self._lock:
            stamps = [t for t in self._log.get(key, []) if now - t < window]
            if stamps:
                self._log[key] = stamps      # 顺手剪枝：只读操作也帮内存省点
            else:
                self._log.pop(key, None)      # 全过期了就把 key 删掉，别留空列表
        return len(stamps) >= _limit()

    def clear(self, username: str) -> None:
        with self._lock:
            self._log.pop(_norm(username), None)


# =====================================================================
# 后端二：Redis（多 worker 共享）
# =====================================================================
class RedisGuard:
    """Redis ZSET 计数：key = prismedu:login_fail:{username}，member = 失败时刻，score = 同一时刻。

    得到的数据：username + 当前时间
    要做的操作：
        register_failed: 一条 pipeline 里做完"剪枝 + 记账 + 数数 + 续期"
        is_locked      : ZCOUNT 数窗口内的成员（只读，单命令）
        clear          : DEL 整个 key
    返回：register_failed / is_locked 都返回 bool

    为什么用 pipeline：
        剪枝、记账、数数三条命令必须"连着发、一次往返"。分三次发 RTT 翻三倍，
        而且中间可能插进另一个 worker 的写入，数出来的值就不可信了。

    为什么 member 要带 uuid：
        ZSET 的成员是唯一的，同一个 member 重复 ZADD 只会更新 score 而不会多一条。
        两次失败落在同一微秒（高并发暴力破解完全可能）时，时间戳字符串会撞车，
        于是"第 5 次失败"被吞掉，锁定永远不触发。拼 8 位 uuid 彻底排除这种情况。
    """

    name = "redis"

    def __init__(self, client):
        """client 是 redis.Redis 实例（由 _build_redis_guard 建好，decode_responses=True）。"""
        self._c = client

    def _key(self, username: str) -> str:
        return f"{KEY_PREFIX}:{_norm(username)}"

    def register_failed(self, username: str) -> bool:
        key = self._key(username)
        now = time.time()
        window = _window_seconds()

        pipe = self._c.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)          # ① 剪掉窗口外的旧失败
        pipe.zadd(key, {f"{now:.6f}:{uuid.uuid4().hex[:8]}": now})   # ② 记这一笔
        pipe.zcard(key)                                       # ③ 数窗口内还剩几次
        pipe.expire(key, int(window) + TTL_MARGIN)            # ④ 续期，防僵尸 key
        count = pipe.execute()[2]                             # 只看第 ③ 条的返回值
        return int(count) >= _limit()

    def is_locked(self, username: str) -> bool:
        key = self._key(username)
        now = time.time()
        window = _window_seconds()
        # ZCOUNT 只数 score 落在 [now-window, +inf) 里的成员 —— 等价于内存版的列表过滤
        count = self._c.zcount(key, now - window, "+inf")
        return int(count) >= _limit()

    def clear(self, username: str) -> None:
        self._c.delete(self._key(username))


# =====================================================================
# 选后端：auto / redis / memory，带 60 秒降级冷却
# =====================================================================
_memory = MemoryGuard()
_redis_guard: RedisGuard | None = None
_down_until: float = 0.0           # 这个时间点之前不再尝试连 Redis
_state_lock = threading.Lock()


def _build_redis_guard() -> RedisGuard:
    """建 Redis 客户端并 ping 一次（ping 不通过就直接抛，交给调用方降级）。"""
    import redis          # 延迟导入：没装 redis 包 / 只用内存后端时，import 这一行都不执行

    client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,             # 返回 str 而不是 bytes，省得到处 decode
        socket_connect_timeout=REDIS_TIMEOUT,
        socket_timeout=REDIS_TIMEOUT,
    )
    client.ping()                          # 连得上 ≠ 用得了，ping 一次确认服务真的活着
    return RedisGuard(client)


def _demote_locked(exc: Exception) -> None:
    """降级到内存 + 开启冷却。**调用方必须已持有 _state_lock**。

    得到的数据：触发降级的异常
    要做的操作：摘掉 Redis 后端、记 60 秒冷却、状态真变了才打一条 warning
    返回：None

    为什么拆成 _locked 版：
        get_guard() 在 `with _state_lock:` 里发现自己连不上，要就地降级；
        而 threading.Lock **不可重入**，如果这时候再调 _demote()（它自己也要拿锁），
        就是自己等自己 —— 实测会直接把登录线程挂死（单测里表现为 pytest 永不返回）。
        所以把"真正干活"的部分抽出来，锁的获取统一交给外层。
    """
    global _redis_guard, _down_until
    was_redis = _redis_guard is not None
    _redis_guard = None
    _down_until = time.time() + RETRY_SECONDS
    if was_redis:
        logger.warning(
            "Redis 调用失败（%s: %s），登录失败计数降级为进程内存，%s 秒后重试",
            type(exc).__name__, exc, RETRY_SECONDS,
        )


def _demote(exc: Exception) -> None:
    """自己拿锁的降级入口（供 _call 在调用中途失败时用）。"""
    with _state_lock:
        _demote_locked(exc)


def get_guard() -> MemoryGuard | RedisGuard:
    """当前该用哪个后端。

    得到的数据：settings.redis_url / settings.login_guard_backend / 冷却截止时间
    要做的操作：memory 强制内存 / redis 强制 Redis（连不上也降级）/ auto 有 url 才用 Redis
    返回：MemoryGuard 或 RedisGuard 单例

    为什么每次都判断：降级是**运行期**发生的（Redis 中途挂掉），
        不能只在进程启动时决定一次，否则挂了就再也回不来。
    """
    global _redis_guard

    mode = (settings.login_guard_backend or "auto").strip().lower()
    if mode == "memory" or not settings.redis_url:
        return _memory

    if _redis_guard is not None:
        return _redis_guard
    if time.time() < _down_until:          # 冷却期内：直接用内存，别去试探
        return _memory

    with _state_lock:
        if _redis_guard is not None:       # 双检：可能别的线程刚建好
            return _redis_guard
        if time.time() < _down_until:
            return _memory
        try:
            _redis_guard = _build_redis_guard()
            logger.warning("登录失败计数已启用 Redis：%s（多 worker 共享）", settings.redis_url)
            return _redis_guard
        except Exception as exc:           # 连不上、没装包、url 写错 —— 一律降级
            _demote_locked(exc)            # 已经持有 _state_lock，不能再调会拿锁的 _demote
            return _memory


# =====================================================================
# 对外门面：security.py 的三个函数改成调这里
# =====================================================================
def _call(method: str, username: str):
    """调当前后端；Redis 中途抛异常就地降级，并用内存重试一次，绝不让登录 500。

    得到的数据：method（register_failed / is_locked / clear）+ username
    要做的操作：get_guard() 拿后端 → 调方法 → 失败则 _demote 后用 MemoryGuard 重放
    返回：后端方法的返回值

    为什么要包这一层：
        启动时 ping 通了不代表后面一直通（容器重启、网络抖动、maxmemory 打满）。
        不包的话，Redis 一断，登录接口直接 500 —— 全校登不上，比"没有锁定"糟得多。
    """
    guard = get_guard()
    try:
        return getattr(guard, method)(username)
    except Exception as exc:
        if guard is _memory:               # 内存后端自己出错，没有更下一层可退
            raise
        _demote(exc)
        return getattr(_memory, method)(username)


def register_failed(username: str) -> bool:
    """记一次失败，返回是否已达锁定阈值。"""
    return bool(_call("register_failed", username))


def is_locked(username: str) -> bool:
    """当前是否处于锁定期（窗口内失败次数已达阈值）。"""
    return bool(_call("is_locked", username))


def clear_failures(username: str) -> None:
    """登录成功后清空失败记录。"""
    _call("clear", username)
