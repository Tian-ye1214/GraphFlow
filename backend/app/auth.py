from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import User

COOKIE_NAME = "gf_session"
COOKIE_MAX_AGE = 7 * 24 * 3600
ACT_AS_COOKIE = "gf_act_as"


def make_session_cookie(user_id: int) -> str:
    return TimestampSigner(settings.secret_key).sign(str(user_id)).decode()


def parse_session_cookie(value: str) -> int | None:
    try:
        raw = TimestampSigner(settings.secret_key).unsign(value, max_age=COOKIE_MAX_AGE)
        return int(raw)
    except (BadSignature, ValueError):
        return None


def make_act_as_cookie(user_id: int) -> str:
    return TimestampSigner(settings.secret_key).sign(str(user_id)).decode()


class DevAuthProvider:
    """开发模式：输入用户名即登录，不存在则自动建用户。SSO 协议确认后新增同接口实现。"""

    async def login(self, session: AsyncSession, username: str) -> User:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            user = User(username=username, display_name=username, auth_provider="dev")
            session.add(user)
        user.is_admin = username in settings.admin_user_set
        await session.commit()
        return user


auth_provider = DevAuthProvider()


async def get_real_user(
    session: AsyncSession = Depends(get_session),
    gf_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    user_id = parse_session_cookie(gf_session) if gf_session else None
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    gf_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    gf_act_as: str | None = Cookie(default=None, alias=ACT_AS_COOKIE),
) -> User:
    """返回有效用户：仅当真实用户是管理员且 act-as cookie 有效时切换为目标用户。"""
    user_id = parse_session_cookie(gf_session) if gf_session else None
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if user.is_admin and gf_act_as:
        target_id = parse_session_cookie(gf_act_as)
        target = await session.get(User, target_id) if target_id is not None else None
        if target is not None:
            return target
    return user


async def require_admin(user: User = Depends(get_real_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
