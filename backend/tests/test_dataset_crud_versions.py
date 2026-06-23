from conftest import wait_ready

from app.models import Dataset, User


async def _upload_csv(client, content: bytes = b"name,age\nAda,36\nLinus,55\nGrace,37\n"):
    r = await client.post(
        "/api/datasets/upload",
        files=[("files", ("people.csv", content, "text/csv"))],
    )
    assert r.status_code == 200
    return await wait_ready(client, r.json()[0]["id"])      # 等后台摄入完成再做版本操作


async def _range(client, dataset_id: int, start: int, end: int):
    r = await client.get(f"/api/datasets/{dataset_id}/rows?start_row={start}&end_row={end}")
    assert r.status_code == 200
    return r.json()["rows"]


async def test_delete_visible_rows_creates_new_version(auth_client):
    ds = await _upload_csv(auth_client)
    r = await auth_client.post(
        f"/api/datasets/{ds['id']}/versions",
        json={"operations": [{"op": "delete_rows", "start_row": 2, "end_row": 3}]},
    )
    assert r.status_code == 200
    new_ds = r.json()
    assert new_ds["id"] != ds["id"]
    assert new_ds["version"] == 2
    assert new_ds["version_of_dataset_id"] == ds["id"]
    assert new_ds["row_count"] == 1

    assert await _range(auth_client, ds["id"], 2, 4) == [
        {"name": "Ada", "age": "36"},
        {"name": "Linus", "age": "55"},
        {"name": "Grace", "age": "37"},
    ]
    assert await _range(auth_client, new_ds["id"], 1, 2) == [
        {"__row_type": "header", "columns": ["name", "age"]},
        {"name": "Grace", "age": "37"},
    ]


async def test_replace_visible_row_creates_new_version(auth_client):
    ds = await _upload_csv(auth_client)
    r = await auth_client.post(
        f"/api/datasets/{ds['id']}/versions",
        json={"operations": [
            {"op": "replace_rows", "start_row": 2, "rows": [{"name": "Alan", "age": "41"}]}
        ]},
    )
    assert r.status_code == 200
    new_ds = r.json()
    assert new_ds["row_count"] == 3
    assert await _range(auth_client, new_ds["id"], 2, 3) == [
        {"name": "Alan", "age": "41"},
        {"name": "Linus", "age": "55"},
    ]


async def test_insert_before_visible_row_creates_new_version(auth_client):
    ds = await _upload_csv(auth_client, b"name,age\nAda,36\nLinus,55\n")
    r = await auth_client.post(
        f"/api/datasets/{ds['id']}/versions",
        json={"operations": [
            {"op": "insert_rows", "before_row": 2, "rows": [{"name": "Grace", "age": "37"}]}
        ]},
    )
    assert r.status_code == 200
    new_ds = r.json()
    assert new_ds["row_count"] == 3
    assert await _range(auth_client, new_ds["id"], 2, 4) == [
        {"name": "Grace", "age": "37"},
        {"name": "Ada", "age": "36"},
        {"name": "Linus", "age": "55"},
    ]


async def test_column_rename_drop_add_constant(auth_client):
    ds = await _upload_csv(auth_client, b"name,age\nAda,36\n")
    r = await auth_client.post(
        f"/api/datasets/{ds['id']}/versions",
        json={"operations": [
            {"op": "rename_column", "from": "name", "to": "full_name"},
            {"op": "drop_column", "name": "age"},
            {"op": "add_constant_column", "name": "source", "value": "manual"},
        ]},
    )
    assert r.status_code == 200
    new_ds = r.json()
    assert new_ds["columns"] == ["full_name", "source"]
    assert await _range(auth_client, new_ds["id"], 1, 2) == [
        {"__row_type": "header", "columns": ["full_name", "source"]},
        {"full_name": "Ada", "source": "manual"},
    ]


async def test_cannot_delete_or_replace_header_row(auth_client):
    ds = await _upload_csv(auth_client)
    delete_header = await auth_client.post(
        f"/api/datasets/{ds['id']}/versions",
        json={"operations": [{"op": "delete_rows", "start_row": 1, "end_row": 1}]},
    )
    replace_header = await auth_client.post(
        f"/api/datasets/{ds['id']}/versions",
        json={"operations": [{"op": "replace_rows", "start_row": 1, "rows": [{"name": "x"}]}]},
    )
    assert delete_header.status_code == 422
    assert replace_header.status_code == 422


async def test_crud_rejects_foreign_dataset(auth_client, session_factory):
    async with session_factory() as s:
        stranger = User(username="crud_stranger", display_name="x")
        s.add(stranger)
        await s.flush()
        ds = Dataset(user_id=stranger.id, name="foreign", row_count=0, columns_json="[]")
        s.add(ds)
        await s.commit()
        ds_id = ds.id

    r = await auth_client.post(
        f"/api/datasets/{ds_id}/versions",
        json={"operations": [{"op": "add_constant_column", "name": "x", "value": "y"}]},
    )
    assert r.status_code == 404
