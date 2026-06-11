from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import StreamingResponse

from app import events
from app.auth import COOKIE_NAME, parse_session_cookie

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
async def event_stream(gf_session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    # 只验签 cookie、不查库：SSE 连接常驻，不能占着数据库会话
    user_id = parse_session_cookie(gf_session) if gf_session else None
    if user_id is None:
        raise HTTPException(status_code=401, detail="未登录")
    q = events.subscribe(user_id)

    async def gen():
        try:
            while True:
                yield f"data: {await q.get()}\n\n"
        finally:
            events.unsubscribe(user_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")
