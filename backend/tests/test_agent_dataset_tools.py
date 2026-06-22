import json

from app.agent.data_preview import WorkflowDataPreview, make_preview_tools
from app.config import settings
from app.models import User
from app.services.dataset_store import create_dataset_from_upload


async def _create_csv_dataset(session_factory, username: str = "tester", rows: int = 2):
    source = settings.data_dir / f"{username}_agent_rows.csv"
    lines = ["name,age"] + [f"n{i},{i}" for i in range(rows)]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    async with session_factory() as s:
        user = User(username=username, display_name="x")
        s.add(user)
        await s.flush()
        ds = (await create_dataset_from_upload(
            s,
            user_id=user.id,
            filename=source.name,
            source_path=source,
            data_dir=settings.data_dir,
        ))[0]
        return user.id, ds.id


async def test_agent_read_dataset_rows_visible_range(session_factory):
    uid, ds_id = await _create_csv_dataset(session_factory)
    raw = await WorkflowDataPreview(session_factory, uid).read_dataset_rows(ds_id, 1, 2)
    payload = json.loads(raw)
    assert payload["dataset_id"] == ds_id
    assert payload["header_row"] == 1
    assert payload["data_start_row"] == 2
    assert payload["rows"] == [
        {"__row_type": "header", "columns": ["name", "age"]},
        {"name": "n0", "age": "0"},
    ]


async def test_agent_read_dataset_rows_column_projection(session_factory):
    uid, ds_id = await _create_csv_dataset(session_factory)
    raw = await WorkflowDataPreview(session_factory, uid).read_dataset_rows(
        ds_id, 1, 2, columns=["name"])
    assert json.loads(raw)["rows"] == [
        {"__row_type": "header", "columns": ["name"]},
        {"name": "n0"},
    ]


async def test_agent_read_dataset_rows_truncates_budget(session_factory):
    uid, ds_id = await _create_csv_dataset(session_factory, rows=505)
    raw = await WorkflowDataPreview(session_factory, uid).read_dataset_rows(ds_id, 2, 506)
    payload = json.loads(raw)
    assert payload["truncated"] is True
    assert len(payload["rows"]) == 500


async def test_agent_read_dataset_rows_rejects_foreign_dataset(session_factory):
    owner_id, ds_id = await _create_csv_dataset(session_factory, username="owner")
    async with session_factory() as s:
        other = User(username="other", display_name="x")
        s.add(other)
        await s.flush()
        other_id = other.id
        await s.commit()
    assert owner_id != other_id
    raw = await WorkflowDataPreview(session_factory, other_id).read_dataset_rows(ds_id, 1, 2)
    assert json.loads(raw)["error"] == "dataset_not_found"


async def test_preview_tools_include_read_dataset_rows(session_factory):
    uid, _ = await _create_csv_dataset(session_factory)
    names = {tool.__name__ for tool in make_preview_tools(session_factory, uid, 1, "node")}
    assert "read_dataset_rows" in names
