"""安全基线：密码哈希与强度策略 / 登录失败锁定 / JWT 签发与校验。

 得到的数据：bcrypt 哈希、带 roles 数组的 JWT、锁定状态
 要做的操作：seed/admin 导入用 hash_password；登录用 verify_password + 锁定检查；
            签发用 create_access_token(roles=[...])
"""
from datetime import datetime, timedelta, timezone
from typing import Any

# 陌生库 bcrypt：密码专用的"慢哈希"库。和 MD5/SHA256 的本质区别：
#   ① 自带随机盐（gensalt()），同样的密码每次哈希结果都不同；
#   ② 故意慢（可调 cost），暴力破解一个密码要几百年，GPU 也快不起来。
#   所以存库的是 hashpw 的产物，验证用 checkpw 反推——不存在"解密"这回事
import bcrypt


# 陌生库 python-jose：JWT（JSON Web Token）的签发/解析库。
#   JWT 是啥：一段自带的身份字符串，形如 xxxxx.yyyyy.zzzzz（头.载荷.签名）
#   载荷paylaod里，明文放着用户信息（Base64，不是加密！谁都能解开看），末尾签名保证没被改过。
#   好处：服务端不用存 session，拿到 token 验签即可信；
#   坏处：签发后无法主动作废，所以有效期不能给太长（本项目 12h），且停用账号要在 get_current_user 里再查库核对
from jose import JWTError, jwt

from app.core import login_guard
from app.core.config import settings

# 所有的角色容器
VALID_ROLES = {"student", "teacher", "researcher", "admin"}

# =================== 密码三件套：校验 → 哈希 → 比对 ==========================

# 简版强度策略：8~72 字节且同时含字母和数字。纯演示口径，生产再上复杂度规则库
def validate_password_strength(password: str) -> None:
    """
    校验密码强度，不合格直接抛 ValueError（调用方转 400）。

    得到的数据：password 明文
    要做的操作：检查长度上限 / 下限 / 字母 / 数字四项
    不合格会怎样：ValueError 带明确原因，绝不能静默放行——未成年人系统弱密码是红线
    坑：bcrypt 硬限制 72 字节，超长直接 ValueError
    """
    if not (settings.password_min_length <= len(password) <= 72):
        raise ValueError(f"密码长度须在 {settings.password_min_length}~72 位之间")
    if not any(c.isalpha() for c in password):
        raise ValueError("密码必须包含字母")
    if not any(c.isdigit() for c in password):
        raise ValueError("密码必须包含数字")


def hash_password(password: str) -> str:
    """生成hash值

    陌生函数链：hashpw(明文bytes, 随机盐) 生成hash
        - gensalt() 每次生成随机盐 → 同一密码 "Prism2026" 两次哈希结果完全不同

    因为bcrypt只吃字节串，所以先 .encode("utf-8")）+ 随机烟盐  再 .decode("ascii") 转回字符串

    产出形如 $2b$12$KIXQ...（$2b$ 是算法代号，12 是慢哈希强度）。
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """明文 vs 哈希比对。哈希为空/格式坏一律 False，不抛异常。

    陌生函数 checkpw：从 hashed 里读出当年用的盐，用同样的盐再哈希一遍plain明文，比对两次结果。
    这就是"同一密码每次哈希不同却能验证"的原理。
    """
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except Exception:
        return False


# ---------------------------------------------------------------- 登录失败锁定
# 真正的计数放在 app/core/login_guard.py：
#   - 有 REDIS_URL → Redis ZSET（多 worker 共享，这是挪 Redis 的唯一目的）
#   - 没配 / 连不上 → 进程内存（等价于改造前的行为）
# 这里保留三个老函数名，是为了 auth.py 和已有单测一行都不用改。
def register_failed_login(username: str) -> bool:
    """记一次登录失败，返回是否已触发锁定。

    得到的数据：username
    要做的操作：交给 login_guard 记一笔，窗口 = lockout_minutes
    返回：True = 已锁定（窗口内达 login_max_failures 次），False = 还没锁
    """
    return login_guard.register_failed(username)


def is_locked(username: str) -> bool:
    """账号当前是否处于锁定期（窗口内失败次数达标即锁）。

    只数最近 lockout_minutes 分钟内的失败 —— 滑出窗口的自动不算数。
    """
    return login_guard.is_locked(username)


def clear_failures(username: str) -> None:
    """登录成功后清空失败记录。"""
    login_guard.clear_failures(username)



# ------ JWT签发与解析
def create_access_token(user_id: int, username: str, roles: list[str]) -> str:
    """签发 JWT。roles 是数组——多角色一次签发。

    得到的数据：user_id / username / roles（如 ["teacher","researcher"]）
    要做的操作：塞进 payload 签 HS256 token，有效期 12h（一周版无 refresh token）
    填错会怎样：roles 传了非 VALID_ROLES 的值直接 ValueError，防止脏角色进 token
    """

    # 第 1 步：脏角色拦截。set(roles) - VALID_ROLES = 求差集，差集非空说明混进了非法角色
    bad = set(roles) - VALID_ROLES
    if bad:
        raise ValueError(f"非法角色: {bad}，合法值 {VALID_ROLES}")
    

    payload: dict[str, Any] = {
        "sub": str(user_id),    # sub=subject，JWT 标准声明：用户标识
        "username": username,
        "roles": roles,         # ["teacher","researcher"] 多角色一次签发
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes),
    }

    # 陌生函数 jwt.encode：
    #   三个入参：载荷 dict / 密钥secret_key / 算法jwt_algorithm。
    #   jwt字符串 = HS256( base64(header) + "." + base64(payload),  secret_key )
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """解析并校验 JWT，返回 payload；无效/过期抛 JWTError（调用方转 401）。

    decode 自动做了三件事：
        验 HS256 签名（改过payload一个字都不行）/ 
        校验 payload的 exp 过期时间 /
        Base64 解回 payload 成 dict。

    任何一步失败都抛 JWTError
    """
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
