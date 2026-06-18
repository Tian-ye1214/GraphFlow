from app.models import QcFailure, QcMetric


async def test_qc_metric_and_failure_insert(session_factory):
    async with session_factory() as s:
        s.add(QcMetric(run_id=1, node_id="qc1", total=10, first_round_pass=7))
        s.add(QcFailure(run_id=1, node_id="qc1", sample_json='{"q":"x"}',
                        reasons_json='[{"model_config_id":2,"pass":false,"reason":"太短"}]'))
        await s.commit()
    async with session_factory() as s:
        from sqlalchemy import select
        m = (await s.execute(select(QcMetric))).scalar_one()
        f = (await s.execute(select(QcFailure))).scalar_one()
    assert m.total == 10 and m.first_round_pass == 7
    assert f.node_id == "qc1" and "太短" in f.reasons_json
