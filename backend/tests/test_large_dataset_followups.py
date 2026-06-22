"""Regression tests for the 6 must-fix review findings on the large-dataset feature:
1 删除回收分片磁盘 / 2 损坏 Excel→422 / 3 超长 CSV 单元格不 500 /
4 CRUD 非有限数中和 / 5 嵌套单元格导出 xlsx 不 500 / 6 分页不被 agent 预算截断。
"""
from io import BytesIO

from openpyxl import load_workbook

from app.config import settings
from app.models import User
from app.services.dataset_store import create_dataset_from_upload, read_dataset_range


async def _upload(client, name: str, content: bytes):
    return await client.post(
        "/api/datasets/upload",
        files=[("files", (name, content, "application/octet-stream"))],
    )


async def _user(session_factory, username="followup_user") -> int:
    async with session_factory() as s:
        user = User(username=username, display_name="x")
        s.add(user)
        await s.commit()
        return user.id


# --- Fix 1: delete reclaims shard disk -------------------------------------

async def test_delete_dataset_reclaims_shard_files(auth_client):
    ds = (await _upload(auth_client, "p.csv", b"q,a\nq1,a1\nq2,a2\n")).json()[0]
    assert list((settings.data_dir / "datasets").rglob("part-*.jsonl"))
    r = await auth_client.delete(f"/api/datasets/{ds['id']}")
    assert r.status_code == 200
    assert not list((settings.data_dir / "datasets").rglob("part-*.jsonl"))


async def test_unlink_run_exports_removes_run_artifact_dir():
    from app.services.run_service import unlink_run_exports

    run_dir = settings.data_dir / "runs" / "777" / "node_x"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "part-000001.jsonl").write_text("{}\n", encoding="utf-8")
    unlink_run_exports([777], settings.data_dir)
    assert not (settings.data_dir / "runs" / "777").exists()


# --- Fix 2: corrupt Excel upload -> 422, not 500 ---------------------------

async def test_corrupt_xlsx_upload_returns_422(auth_client):
    r = await _upload(auth_client, "bad.xlsx", b"this is not a real workbook")
    assert r.status_code == 422


# --- Fix 3: a single >128KB CSV cell is accepted (legit large-text row) -----

async def test_large_csv_cell_uploads(auth_client):
    big = "x" * 200_000
    content = f"q,a\nq1,{big}\n".encode("utf-8")
    r = await _upload(auth_client, "big.csv", content)
    assert r.status_code == 200
    assert r.json()[0]["row_count"] == 1


# --- Fix 4: CRUD non-finite values are neutralized to null in canonical shards

async def test_crud_constant_non_finite_neutralized(tmp_path, session_factory):
    from app.services.dataset_crud import apply_dataset_operations

    uid = await _user(session_factory)
    src = tmp_path / "d.csv"
    src.write_text("q\nq1\n", encoding="utf-8")
    async with session_factory() as s:
        ds = (await create_dataset_from_upload(
            s, user_id=uid, filename="d.csv", source_path=src,
            data_dir=settings.data_dir, shard_size=10))[0]
        new = await apply_dataset_operations(
            s, source=ds, user_id=uid, data_dir=settings.data_dir,
            operations=[{"op": "add_constant_column", "name": "c", "value": float("inf")}])
        page = await read_dataset_range(
            s, new, data_dir=settings.data_dir, start_row=2, end_row=2)
    assert page["rows"] == [{"q": "q1", "c": None}]
    for p in (settings.data_dir / "datasets").rglob("part-*.jsonl"):
        text = p.read_text(encoding="utf-8")
        assert "Infinity" not in text and "NaN" not in text


# --- Fix 5: xlsx export of nested (dict/list) cells stringifies, not 500 ----

async def test_export_xlsx_stringifies_nested_cells(auth_client):
    ds = (await _upload(auth_client, "n.jsonl", b'{"a": {"x": 1}}\n')).json()[0]
    r = await auth_client.get(f"/api/datasets/{ds['id']}/export?format=xlsx")
    assert r.status_code == 200
    wb = load_workbook(BytesIO(r.content), read_only=True, data_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    assert rows[0] == ("a",)
    assert rows[1] == ('{"x": 1}',)


# --- Fix 6: human pagination returns a full page even past the 60KB budget ---

async def test_rows_page_not_truncated_by_agent_budget(auth_client):
    big = "y" * 4000
    content = ("q\n" + "\n".join(big for _ in range(20)) + "\n").encode("utf-8")
    ds = (await _upload(auth_client, "wide.csv", content)).json()[0]
    assert ds["row_count"] == 20
    r = await auth_client.get(f"/api/datasets/{ds['id']}/rows?page=1&page_size=20")
    assert r.status_code == 200
    assert len(r.json()["rows"]) == 20
