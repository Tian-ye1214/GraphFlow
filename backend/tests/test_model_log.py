from sqlalchemy import func, select

from app.models import ModelCallLog
from app.services import model_log


async def _count(session_factory, **w):
    async with session_factory() as s:
        stmt = select(func.count()).select_from(ModelCallLog)
        for k, v in w.items():
            stmt = stmt.where(getattr(ModelCallLog, k) == v)
        return await s.scalar(stmt)


async def test_no_context_no_log(session_factory):
    await model_log.log_model_call(messages=[{"role": "user", "content": "hi"}],
                                   response_text="ok", ok=True, model_name="m", provider="openai")
    assert await _count(session_factory) == 0


async def test_agent_source_full(session_factory):
    with model_log.log_context(user_id=1, session_id=7, source="redlotus"):
        for _ in range(30):
            await model_log.log_model_call(messages=[{"role": "user", "content": "x"}],
                                           response_text="r", ok=True, model_name="m", provider="openai")
    assert await _count(session_factory, source="redlotus") == 30   # Agent 类全量，不限量


async def test_node_source_success_capped_failures_kept(session_factory):
    with model_log.log_context(user_id=1, run_id=100, node_id="ls", source="synth"):
        for _ in range(25):
            await model_log.log_model_call(messages=[{"role": "user", "content": "x"}],
                                           response_text="r", ok=True, model_name="m", provider="openai")
        for _ in range(3):
            await model_log.log_model_call(messages=[{"role": "user", "content": "x"}],
                                           response_text="", ok=False, model_name="m", provider="openai")
    assert await _count(session_factory, source="synth", run_id=100) == model_log.NODE_LIMIT + 3


async def test_redaction_no_secret(session_factory):
    with model_log.log_context(user_id=1, source="redlotus"):
        await model_log.log_model_call(
            messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "q"}],
            response_text="a", ok=True, model_name="m", provider="openai")
    async with session_factory() as s:
        row = (await s.execute(select(ModelCallLog))).scalars().first()
    blob = row.request_json + row.response_json
    assert "rules" in blob and "api_key" not in blob.lower() and "authorization" not in blob.lower()
