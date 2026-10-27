"""登录失败计数双后端的一致性 / 降级测试（M1-T2 加固，2026-10-27）。

【测什么】
  ① 语义：滑动窗口只数最近 lockout_minutes 分钟 —— 内存版与 Redis 版必须逐条一致
  ② 归一化：`" R003 "` 与 `"r003"` 是同一个账号，不能因为大小写/空格多出计数
  ③ 降级：Redis 连不上 / 调用中途炸掉，都必须退回内存并且**不抛异常**（登录不能 500）
  ④ 冷却：降级后 60 秒内不再尝试连 Redis，否则每次登录都要先吃一次连接超时

【为什么自己写 FakeRedis 而不是拉 fakeredis 依赖】
  RedisGuard 只用了 5 条命令（ZADD / ZCARD / ZCOUNT / ZREMRANGEBYSCORE / EXPIRE + pipeline），
  手写 40 行假实现就够，且能精确控制"第几条命令抛异常"——
  真 Redis 反而没法模拟"连接在 pipeline 执行时断开"这种场景。
"""
import pytest

from app.core import login_guard
from app.core.login_guard import MemoryGuard, RedisGuard

USER = "R003"


# =====================================================================
# 假 Redis：只实现 RedisGuard 用到的那几条命令
# =====================================================================
class _FakePipeline:
    """把命令排队，execute() 时一次性执行 —— 和真 Redis 的 pipeline 语义一致。"""

    def __init__(self, client):
        self._c = client
        self._ops: list[tuple] = []

    def zremrangebyscore(self, key, lo, hi):
        self._ops.append(("zremrangebyscore", key, lo, hi))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def zcard(self, key):
        self._ops.append(("zcard", key))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self):
        out = [self._run(*op) for op in self._ops]
        self._ops.clear()
        return out

    def _run(self, name, *args):
        if name == "zremrangebyscore":
            key, lo, hi = args
            z = self._c.data.get(key, {})
            dead = [m for m, s in z.items() if float(lo) <= s <= float(hi)]
            for m in dead:
                z.pop(m)
            return len(dead)
        if name == "zadd":
            key, mapping = args
            self._c.data.setdefault(key, {}).update(mapping)
            return len(mapping)
        if name == "zcard":
            return len(self._c.data.get(args[0], {}))
        if name == "expire":
            key, ttl = args
            self._c.ttl[key] = ttl
            return 1
        raise AssertionError(f"FakeRedis 没实现这条命令：{name}")


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, dict[str, float]] = {}   # key -> {member: score}
        self.ttl: dict[str, int] = {}

    def pipeline(self):
        return _FakePipeline(self)

    def zcount(self, key, lo, hi):
        z = self.data.get(key, {})
        low = float("-inf") if lo == "-inf" else float(lo)
        high = float("inf") if hi == "+inf" else float(hi)
        return sum(1 for s in z.values() if low <= s <= high)

    def delete(self, key):
        self.data.pop(key, None)
        self.ttl.pop(key, None)
        return 1

    def ping(self):
        return True


class _BrokenRedis:
    """连得上、但命令执行时断线 —— 模拟"ping 通了，后面挂了"这种真实故障。"""

    def pipeline(self):
        return _Exploding()

    def zcount(self, key, lo, hi):
        raise ConnectionError("Connection reset by peer")

    def delete(self, key):
        raise ConnectionError("Connection reset by peer")

    def ping(self):
        return True


class _Exploding:
    def zremrangebyscore(self, *a): return self
    def zadd(self, *a): return self
    def zcard(self, *a): return self
    def expire(self, *a): return self

    def execute(self):
        raise ConnectionError("Connection reset by peer")


class _Clock:
    """可控时钟：换掉 login_guard 里的 time 模块，窗口推进靠改 now 而不是 sleep。"""

    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(login_guard, "time", c)
    MemoryGuard._log.clear()            # 类级字典，测试之间必须清干净
    monkeypatch.setattr(login_guard, "_redis_guard", None)
    monkeypatch.setattr(login_guard, "_down_until", 0.0)
    return c


@pytest.fixture()
def fake_redis(clock):
    return _FakeRedis()


def _run_sequence(guard, username, plan):
    """按 plan 跑一串动作，返回每一步后的"是否锁定"。

    plan 里 'f' = 记一次失败，'c' = 清空，数字 = 时间前进多少秒。
    """
    out = []
    for step in plan:
        if step == "f":
            guard.register_failed(username)
        elif step == "c":
            guard.clear(username)
        else:
            login_guard.time.advance(step)
        out.append(guard.is_locked(username))
    return out


# =====================================================================
# ① 语义：只数窗口内的失败
# =====================================================================
def test_memory_lock_at_fifth_failure(clock):
    """第 4 次不锁、第 5 次锁 —— 阈值来自 settings.login_max_failures（默认 5）。"""
    g = MemoryGuard()
    for _ in range(4):
        assert g.register_failed(USER) is False
    assert g.is_locked(USER) is False
    assert g.register_failed(USER) is True
    assert g.is_locked(USER) is True


def test_memory_window_slides_out(clock):
    """锁满之后等窗口过去，自动解锁（没有任何"解锁线程"定时器）。"""
    g = MemoryGuard()
    for _ in range(5):
        g.register_failed(USER)
    assert g.is_locked(USER) is True

    clock.advance(15 * 60 + 1)          # 窗口 15 分钟 + 1 秒
    assert g.is_locked(USER) is False, "滑出窗口外的失败记录不该再算数"


def test_memory_only_recent_failures_count(clock):
    """4 次失败在窗口边缘过期后，只剩第 5 次 —— 计数必须退回 1，不能留 5。"""
    g = MemoryGuard()
    for _ in range(4):
        g.register_failed(USER)         # t=0 连续 4 次
    clock.advance(15 * 60 - 1)          # 快到窗口边缘
    assert g.register_failed(USER) is True          # 第 5 次 → 锁
    assert g.is_locked(USER) is True

    clock.advance(2)                    # 前 4 条已过期，第 5 条还在窗口内
    assert g.is_locked(USER) is False, "只剩 1 次，不该还锁着"


def test_memory_clear_resets(clock):
    """登录成功清空 → 之前的 4 次失败不再累计。"""
    g = MemoryGuard()
    for _ in range(4):
        g.register_failed(USER)
    g.clear(USER)
    assert g.is_locked(USER) is False
    assert g.register_failed(USER) is False         # 重新从 1 开始数


def test_username_is_normalized(clock):
    """`" R003 "` 与 `"r003"` 必须落在同一个 key 上。"""
    g = MemoryGuard()
    for _ in range(5):
        g.register_failed(" R003 ")
    assert g.is_locked("r003") is True, "大小写/空格不同不该变成两个账号"
    assert g.is_locked("R003") is True

    r = RedisGuard(_FakeRedis())
    for _ in range(5):
        r.register_failed(" R003 ")
    assert r.is_locked("r003") is True
    assert list(r._c.data) == ["prismedu:login_fail:r003"], "key 必须带命名空间且已归一化"


# =====================================================================
# ② 两套后端逐条一致（这是"换后端不改行为"的硬证据）
# =====================================================================
@pytest.mark.parametrize("plan", [
    ["f", "f", "f", "f", "f"],                          # 连续 5 次 → 锁
    ["f", "f", "f", "f", 900, "f"],                     # 4 次后过了 15 分钟再失败 → 只有 1 次
    ["f", "f", "f", "f", 899, "f", 2],                  # 4 次 + 差 1 秒到窗口时第 5 次 → 锁，2 秒后解锁
    ["f", "f", "c", "f", "f", "f", "f"],                # 中途清空 → 重新计数
    ["f", "f", "f", "f", 300, "f", 300, "f"],           # 跨窗口交错
])
def test_memory_and_redis_agree(clock, fake_redis, plan):
    mem = MemoryGuard()
    red = RedisGuard(fake_redis)

    mem_seq = _run_sequence(mem, USER, plan)

    # 同一序列在 Redis 版上重跑：先清空状态，再把时钟拨回去
    MemoryGuard._log.clear()
    start = clock.now
    red.clear(USER)
    red_seq = _run_sequence(red, USER, plan)
    clock.now = start

    assert mem_seq == red_seq, f"序列 {plan} 上两个后端判定不一致"


# =====================================================================
# ③ Redis 细节：TTL 续期 + 同微秒多次失败不被吞
# =====================================================================
def test_redis_sets_ttl(clock, fake_redis):
    """每次记账都续期，窗口一过 key 自然消失，不会留僵尸 key。"""
    RedisGuard(fake_redis).register_failed(USER)
    key = "prismedu:login_fail:r003"
    assert fake_redis.ttl[key] == 15 * 60 + 60


def test_redis_counts_same_microsecond_failures(clock, fake_redis):
    """同一时刻连打 5 次（高并发爆破）也必须算 5 次。

    如果拿时间戳当 ZSET 成员，ZADD 会当成"同一个成员更新 score"，5 次只剩 1 条 → 永不锁定。
    """
    g = RedisGuard(fake_redis)          # 时钟不推进，now 完全相同
    results = [g.register_failed(USER) for _ in range(5)]
    assert results == [False, False, False, False, True]


# =====================================================================
# ④ 降级：Redis 连不上 / 中途炸掉
# =====================================================================
def test_connect_failure_falls_back_to_memory(clock, monkeypatch):
    """建连失败 → 用内存后端，且冷却期内不再尝试（只连一次）。"""
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(login_guard, "_build_redis_guard", boom)
    monkeypatch.setattr(login_guard.settings, "redis_url", "redis://localhost:6380/0")
    monkeypatch.setattr(login_guard.settings, "login_guard_backend", "auto")

    assert isinstance(login_guard.get_guard(), MemoryGuard)
    assert login_guard.get_guard() is login_guard._memory
    assert calls["n"] == 1, "冷却期内不该反复重连（每次登录都吃一次超时）"

    clock.advance(login_guard.RETRY_SECONDS + 1)     # 冷却结束，允许再试
    login_guard.get_guard()
    assert calls["n"] == 2


def test_runtime_failure_demotes_without_raising(clock, monkeypatch):
    """ping 通了但命令执行时断线：登录不能 500，失败次数改记在内存里。"""
    # 必须配了 redis_url，get_guard() 才会真的用 _redis_guard —— 否则直接走内存，测不到降级
    monkeypatch.setattr(login_guard.settings, "redis_url", "redis://localhost:6380/0")
    monkeypatch.setattr(login_guard.settings, "login_guard_backend", "auto")
    monkeypatch.setattr(login_guard, "_redis_guard", RedisGuard(_BrokenRedis()))

    # 不抛异常，且降级后用内存计数（第 5 次仍然能触发锁定）
    results = [login_guard.register_failed(USER) for _ in range(5)]
    assert results == [False, False, False, False, True]
    assert login_guard._redis_guard is None, "出错后必须摘掉 Redis 后端"
    assert login_guard.is_locked(USER) is True


def test_backend_memory_forces_memory(clock, monkeypatch):
    """LOGIN_GUARD_BACKEND=memory 时，即使配了 REDIS_URL 也不碰 Redis。"""
    monkeypatch.setattr(login_guard.settings, "redis_url", "redis://localhost:6380/0")
    monkeypatch.setattr(login_guard.settings, "login_guard_backend", "memory")
    assert isinstance(login_guard.get_guard(), MemoryGuard)


def test_no_url_means_memory(clock, monkeypatch):
    """没配 REDIS_URL（默认）→ 和改造前完全一样，纯内存。"""
    monkeypatch.setattr(login_guard.settings, "redis_url", "")
    assert isinstance(login_guard.get_guard(), MemoryGuard)


def test_security_wrapper_still_works(clock):
    """security.py 的三个老函数名必须继续可用（auth.py 与老测试都靠它）。"""
    from app.core import security

    security.clear_failures(USER)
    for _ in range(4):
        assert security.register_failed_login(USER) is False
    assert security.is_locked(USER) is False
    security.register_failed_login(USER)
    assert security.is_locked(USER) is True
    security.clear_failures(USER)
    assert security.is_locked(USER) is False
