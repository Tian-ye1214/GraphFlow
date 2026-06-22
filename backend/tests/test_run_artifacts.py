import json

from sqlalchemy import select

from app.config import settings
from app.models import Dataset, Run, RunRow, User, Workflow, WorkflowVersion
from app.services.dataset_store import read_dataset_range
from app.services.run_artifacts import (
    ArtifactWriter,
    iter_artifact_rows,
    read_output_ref_rows,
    register_artifact_as_dataset,
)


def test_artifact_writer_appends_shards_and_manifest(tmp_path):
    writer = ArtifactWriter(tmp_path, run_id=7, node_id="out", columns=["q"], shard_size=2)
    ref1 = writer.append(2, [{"q": "a"}])
    ref2 = writer.append(3, [{"q": "b"}, {"q": "c"}])
    manifest = writer.close()

    assert manifest["row_count"] == 3
    assert len(manifest["shards"]) == 2
    assert read_output_ref_rows(ref1, tmp_path) == [{"q": "a"}]
    assert read_output_ref_rows(ref2, tmp_path) == [{"q": "b"}, {"q": "c"}]
    assert list(iter_artifact_rows(manifest)) == [
        (2, {"q": "a"}),
        (3, {"q": "b"}),
        (3, {"q": "c"}),
    ]


async def test_output_node_registers_artifact_as_dataset(session_factory, tmp_path):
    writer = ArtifactWriter(tmp_path, run_id=8, node_id="out", columns=["q"], shard_size=10)
    writer.append(1, [{"q": "a"}])
    artifact = writer.close()
    async with session_factory() as s:
        user = User(username="artifact_user", display_name="x")
        s.add(user)
        await s.flush()
        ds = await register_artifact_as_dataset(
            s,
            user_id=user.id,
            name="artifact dataset",
            source_artifact=artifact,
            data_dir=tmp_path,
            run_id=8,
            node_id="out",
        )
        ds_id = ds.id

    async with session_factory() as s:
        ds = await s.get(Dataset, ds_id)
        payload = await read_dataset_range(s, ds, data_dir=tmp_path, start_row=1, end_row=1)
    assert ds.source == "run"
    assert ds.row_count == 1
    assert payload["rows"] == [{"q": "a"}]


async def test_run_rows_endpoint_reads_artifact_output(auth_client, session_factory):
    writer = ArtifactWriter(
        settings.data_dir,
        run_id=9,
        node_id="out",
        columns=["q"],
        shard_size=10,
    )
    ref = writer.append(1, [{"q": "from-artifact"}])
    writer.close()
    async with session_factory() as s:
        uid = (await s.execute(select(User.id).where(User.username == "tester"))).scalar_one()
        wf = Workflow(user_id=uid, name="wf", graph_json='{"nodes":[],"edges":[]}')
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=wf.graph_json)
        s.add(ver)
        await s.flush()
        run = Run(user_id=uid, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run)
        await s.flush()
        s.add(RunRow(run_id=run.id, node_id="out", row_idx=0, status="done", output_ref=ref))
        await s.commit()
        run_id = run.id

    r = await auth_client.get(f"/api/runs/{run_id}/rows?node_id=out")
    assert r.status_code == 200
    assert r.json()["rows"] == [{"q": "from-artifact"}]
