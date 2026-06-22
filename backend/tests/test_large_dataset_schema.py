import json

from app.models import Dataset, RunRow, User


async def test_dataset_large_storage_metadata_defaults(session_factory):
    async with session_factory() as s:
        u = User(username="meta_user", display_name="x")
        s.add(u)
        await s.flush()
        ds = Dataset(user_id=u.id, name="big", columns_json="[]")
        s.add(ds)
        await s.commit()
        ds_id = ds.id

    async with session_factory() as s:
        ds = await s.get(Dataset, ds_id)
        assert ds.status == "ready"
        assert ds.imported_rows == 0
        assert ds.original_format == ""
        assert ds.version == 1
        assert ds.version_of_dataset_id is None
        assert ds.header_row is None
        assert ds.data_start_row == 1
        assert ds.total_rows_including_header == 0
        assert json.loads(ds.manifest_json) == {}


async def test_runrow_checkpoint_fields_defaults(session_factory):
    async with session_factory() as s:
        rr = RunRow(run_id=1, node_id="n", row_idx=0)
        s.add(rr)
        await s.commit()
        row_id = rr.id

    async with session_factory() as s:
        rr = await s.get(RunRow, row_id)
        assert rr.file_row is None
        assert rr.output_ref == ""
