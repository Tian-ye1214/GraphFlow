from sqlalchemy import select


async def test_runlog_insert_and_defaults(client, session_factory):
    from app.models import RunLog
    async with session_factory() as s:
        s.add(RunLog(run_id=1, node_id="n1", message="hi"))
        await s.commit()
    async with session_factory() as s:
        rows = (await s.execute(select(RunLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].message == "hi"
    assert rows[0].level == "info"
    assert rows[0].node_id == "n1"
    assert rows[0].created_at is not None
