from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import COOKIE_MAX_AGE, COOKIE_NAME, auth_provider, get_current_user, make_session_cookie
from app.db import get_session
from app.models import User

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    username: str


def _user_out(user: User) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


@router.post("/auth/login")
async def login(body: LoginIn, response: Response, session: AsyncSession = Depends(get_session)):
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="用户名不能为空")
    user = await auth_provider.login(session, username)
    response.set_cookie(COOKIE_NAME, make_session_cookie(user.id), httponly=True, max_age=COOKIE_MAX_AGE)
    return _user_out(user)


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return _user_out(user)
