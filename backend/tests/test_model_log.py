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


async def test_prune_keeps_failure_traces_and_limits_success_qc_logs(session_factory):
    from app.models import QcFailure, RunRow
    model_log._success_counts.clear()
    for i in range(25):
        with model_log.log_context(user_id=1, run_id=300, node_id="qc", source="qc", trace_id=f"ok-{i}"):
            await model_log.log_model_call(messages=[{"role": "user", "content": "x"}],
                                           response_text=f"r{i}", ok=True,
                                           model_name="m", provider="openai")
    async with session_factory() as s:
        s.add(QcFailure(run_id=300, node_id="qc", trace_id="ok-24",
                        sample_json='{"q":"x"}', reasons_json='[]'))
        s.add(RunRow(run_id=300, node_id="gen", row_idx=0, status="failed", trace_id="row-fail"))
        s.add(ModelCallLog(run_id=300, node_id="gen", source="synth", trace_id="row-fail",
                           response_json="boom"))
        await s.commit()

    assert await _count(session_factory, run_id=300, source="qc") == 25
    await model_log.prune_run_model_logs(session_factory, 300, success_per_node=3)
    async with session_factory() as s:
        logs = (await s.execute(select(ModelCallLog).where(ModelCallLog.run_id == 300)
                                .order_by(ModelCallLog.id))).scalars().all()
    traces = [l.trace_id for l in logs]
    assert "ok-24" in traces
    assert "row-fail" in traces
    assert len([t for t in traces if t.startswith("ok-") and t != "ok-24"]) == 3


def test_forget_run_clears_only_that_run():
    """M1: run 到终态后清理其 (run_id,node_id) 计数键，防止长跑进程无界累积；不误清其它 run。"""
    model_log._success_counts.clear()
    model_log._success_counts.update({(1, "a"): 7, (1, "b"): 3, (2, "a"): 1})
    model_log.forget_run(1)
    assert not any(k[0] == 1 for k in model_log._success_counts)
    assert model_log._success_counts[(2, "a")] == 1
    model_log._success_counts.clear()


async def test_redaction_no_secret(session_factory):
    with model_log.log_context(user_id=1, source="redlotus"):
        await model_log.log_model_call(
            messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "q"}],
            response_text="a", ok=True, model_name="m", provider="openai")
    async with session_factory() as s:
        row = (await s.execute(select(ModelCallLog))).scalars().first()
    blob = row.request_json + row.response_json
    assert "rules" in blob and "api_key" not in blob.lower() and "authorization" not in blob.lower()
