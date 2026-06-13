import app.services.run_service as rs


def test_parse_threshold():
    assert rs.parse_threshold("把首轮质检通过率提升到 90% 以上") == 0.9
    assert rs.parse_threshold("达到 0.85") == 0.85
    assert rs.parse_threshold("把数据清洗干净") is None


async def test_first_round_rate_aggregates(session_factory):
    from app.models import QcMetric
    async with session_factory() as s:
        s.add(QcMetric(run_id=7, node_id="a", total=10, first_round_pass=6))
        s.add(QcMetric(run_id=7, node_id="b", total=10, first_round_pass=8))
        await s.commit()
    rate = await rs.first_round_rate(session_factory, 7)
    assert abs(rate - 0.7) < 1e-6                 # (6+8)/(10+10)


async def test_first_round_rate_none_when_no_metric(session_factory):
    assert await rs.first_round_rate(session_factory, 999) is None
