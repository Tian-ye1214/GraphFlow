import pytest

from app.models import QcFailure, QcMetric


async def _seed(session_factory, run_id):
    async with session_factory() as s:
        s.add(QcMetric(run_id=run_id, node_id="qc1", total=10, first_round_pass=6))
        s.add(QcFailure(run_id=run_id, node_id="qc1", sample_json='{"q":"x"}',
                        reasons_json='[{"model_config_id":2,"pass":false,"reason":"短"}]'))
        await s.commit()


async def test_qc_metrics_and_failures_endpoints(auth_client, session_factory):
    from sqlalchemy import select
    from app.models import Run, User
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        run = Run(user_id=uid, workflow_id=0, workflow_version_id=0, status="completed")
        s.add(run); await s.commit(); run_id = run.id
    await _seed(session_factory, run_id)
    metrics = (await auth_client.get(f"/api/runs/{run_id}/qc-metrics")).json()
    assert metrics[0]["first_round_pass"] == 6 and abs(metrics[0]["first_round_rate"] - 0.6) < 1e-6
    failures = (await auth_client.get(f"/api/runs/{run_id}/qc-failures")).json()
    assert failures[0]["sample"]["q"] == "x" and failures[0]["reasons"][0]["reason"] == "短"


async def test_qc_endpoints_reject_foreign_run(auth_client, session_factory):
    from sqlalchemy import select
    from app.models import Run, User
    # Create a "stranger" user directly in the DB (without touching the shared auth_client cookie)
    async with session_factory() as s:
        stranger = User(username="stranger", display_name="stranger")
        s.add(stranger)
        await s.commit()
        sid = stranger.id
        run = Run(user_id=sid, workflow_id=0, workflow_version_id=0, status="completed")
        s.add(run)
        await s.commit()
        rid = run.id
    # auth_client is logged in as "tester" — requesting stranger's run must return 404
    assert (await auth_client.get(f"/api/runs/{rid}/qc-metrics")).status_code == 404
