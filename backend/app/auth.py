from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import User

COOKIE_NAME = "gf_session"
COOKIE_MAX_AGE = 7 * 24 * 3600


def make_session_cookie(user_id: int) -> str:
    return TimestampSigner(settings.secret_key).sign(str(user_id)).decode()


def parse_session_cookie(value: str) -> int | None:
    try:
        raw = TimestampSigner(settings.secret_key).unsign(value, max_age=COOKIE_MAX_AGE)
        return int(raw)
    except (BadSignature, ValueError):
        return None


class DevAuthProvider:
    """开发模式：输入用户名即登录，不存在则自动建用户。SSO 协议确认后新增同接口实现。"""

    async def login(self, session: AsyncSession, username: str) -> User:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            user = User(username=username, display_name=username, auth_provider="dev")
            session.add(user)
            await session.commit()
        return user


auth_provider = DevAuthProvider()


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    gf_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    user_id = parse_session_cookie(gf_session) if gf_session else None
    user = await session.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user
