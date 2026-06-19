import json
import socket
import sys
import threading
import time

import pytest
import uvicorn

import app.cli as cli
from app.config import settings


def gf(*argv: str):
    cli.main(list(argv))


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "cli.json")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    from app.main import create_app
    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                        log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def test_login_writes_state(server, capsys):
    gf("login", "alice", "--server", server)
    state = json.loads(cli.STATE_FILE.read_text(encoding="utf-8"))
    assert state["server"] == server and state["cookie"]
    assert "已登录 alice" in capsys.readouterr().out


def test_st_shows_user(server, capsys):
    gf("login", "alice", "--server", server)
    capsys.readouterr()
    gf("st")
    assert "alice" in capsys.readouterr().out


def test_st_without_login_dies(server, capsys):
    with pytest.raises(SystemExit) as e:
        gf("st")
    assert e.value.code == 1
    assert "gf login" in capsys.readouterr().err


def test_logout_clears_state(server, capsys):
    gf("login", "tester", "--server", server)
    gf("wf", "add", "流X"); gf("use", "流X")
    capsys.readouterr()
    gf("logout")
    assert "已登出" in capsys.readouterr().out
    state = json.loads(cli.STATE_FILE.read_text(encoding="utf-8"))
    assert not state.get("cookie") and not state.get("workflow_id")
    assert state.get("server") == server   # server 保留，方便重登
    with pytest.raises(SystemExit):        # 登出后需重新登录
        gf("st")


def login_and_wf(server: str, name: str = "流"):
    gf("login", "tester", "--server", server)
    gf("wf", "add", name)
    gf("use", name)


def test_wf_lifecycle(server, capsys):
    gf("login", "tester", "--server", server)
    gf("wf", "add", "流A")
    gf("use", "流A")
    capsys.readouterr()
    gf("st")
    assert "流A" in capsys.readouterr().out
    gf("wf", "ls")
    assert "流A" in capsys.readouterr().out
    gf("wf", "rm", "流A")
    capsys.readouterr()
    gf("wf", "ls")
    assert "流A" not in capsys.readouterr().out


def test_use_unknown_name_dies(server, capsys):
    gf("login", "tester", "--server", server)
    with pytest.raises(SystemExit) as e:
        gf("use", "不存在的流")
    assert e.value.code == 1
    assert "找不到名为" in capsys.readouterr().err


def test_show_lists_nodes_and_edges(server, capsys):
    login_and_wf(server)
    capsys.readouterr()
    gf("show")
    out = capsys.readouterr().out
    assert "节点（0）" in out and "连线（0）" in out


def test_node_add_set_show(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    capsys.readouterr()
    gf("node", "set", "llm_synth_1", "prompt=Q:{{q}}", "conc=8", "temp=0.5", "out=a")
    capsys.readouterr()
    gf("node", "show", "llm_synth_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["user_prompt"] == "Q:{{q}}"
    assert node["config"]["concurrency"] == 8
    assert node["config"]["params"]["temperature"] == 0.5
    assert node["config"]["output_column"] == "a"


def test_node_auto_numbering(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    gf("node", "add", "llm")
    capsys.readouterr()
    gf("show")
    out = capsys.readouterr().out
    assert "llm_synth_1" in out and "llm_synth_2" in out


def test_node_set_unknown_key_dies(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    with pytest.raises(SystemExit):
        gf("node", "set", "llm_synth_1", "nosuch=1")
    assert "未知配置键" in capsys.readouterr().err


def test_link_unlink_and_rm_cleans_edges(server, capsys):
    login_and_wf(server)
    gf("node", "add", "input")
    gf("node", "add", "llm")
    gf("link", "input_1", "llm_synth_1")
    capsys.readouterr()
    gf("show")
    assert "input_1 -> llm_synth_1" in capsys.readouterr().out
    gf("unlink", "input_1", "llm_synth_1")
    capsys.readouterr()
    gf("show")
    assert "input_1 -> llm_synth_1" not in capsys.readouterr().out
    gf("link", "input_1", "llm_synth_1")
    gf("node", "rm", "llm_synth_1")
    capsys.readouterr()
    gf("show")
    out = capsys.readouterr().out
    assert "llm_synth_1" not in out and "->" not in out


def test_qc_node_set_and_rescan_link(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    gf("node", "add", "qc")
    gf("node", "set", "qc_1", "system=你是质检员", "prompt=判定:{{a}}", "max_rounds=2")
    capsys.readouterr()
    gf("node", "show", "qc_1")
    node = json.loads(capsys.readouterr().out)
    assert node["type"] == "qc"
    assert node["config"]["system_prompt"] == "你是质检员"
    assert node["config"]["user_prompt"] == "判定:{{a}}"
    assert node["config"]["max_rounds"] == 2
    gf("link", "llm_synth_1", "qc_1")
    capsys.readouterr()
    gf("link", "qc_1", "llm_synth_1", "--kind", "rescan")
    assert "回扫" in capsys.readouterr().out
    gf("show")
    assert "qc_1 ⟲回扫 llm_synth_1" in capsys.readouterr().out


def test_qc_col_key_now_dies(server, capsys):
    login_and_wf(server)
    gf("node", "add", "qc")
    with pytest.raises(SystemExit):
        gf("node", "set", "qc_1", "qc_col=a")
    assert "未知配置键" in capsys.readouterr().err


def test_rescan_from_non_qc_node_dies(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    gf("node", "add", "output")
    with pytest.raises(SystemExit):
        gf("link", "llm_synth_1", "output_1", "--kind", "rescan")
    assert "qc 节点" in capsys.readouterr().err


def test_node_add_without_use_dies(server, capsys):
    gf("login", "tester", "--server", server)
    with pytest.raises(SystemExit):
        gf("node", "add", "llm")
    assert "gf use" in capsys.readouterr().err


def test_node_set_bad_int_dies(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    with pytest.raises(SystemExit) as e:
        gf("node", "set", "llm_synth_1", "conc=8.5")
    assert e.value.code == 1
    assert "8.5" in capsys.readouterr().err


def test_op_lifecycle(server, capsys):
    login_and_wf(server)
    gf("node", "add", "auto")
    gf("op", "add", "auto_process_1", "dedup", "q")
    gf("op", "add", "auto_process_1", "filter", "q", "min_len", "5")
    gf("op", "add", "auto_process_1", "shuffle")
    capsys.readouterr()
    gf("op", "ls", "auto_process_1")
    out = capsys.readouterr().out
    assert "1. 去重" in out and "2. 过滤" in out and "3. 打乱" in out
    gf("node", "show", "auto_process_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["operations"][1] == {"op": "filter", "column": "q",
                                               "mode": "min_len", "value": 5}
    gf("op", "rm", "auto_process_1", "1")
    capsys.readouterr()
    gf("op", "ls", "auto_process_1")
    out = capsys.readouterr().out
    assert "去重" not in out and "1. 过滤" in out


def test_op_on_non_auto_node_dies(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    with pytest.raises(SystemExit):
        gf("op", "add", "llm_synth_1", "shuffle")
    assert "不是自动处理节点" in capsys.readouterr().err


def test_model_lifecycle(server, capsys):
    gf("login", "tester", "--server", server)
    gf("model", "add", "通义", "--url", "http://x/v1", "--model", "qwen", "--key", "k")
    capsys.readouterr()
    gf("model", "ls")
    out = capsys.readouterr().out
    assert "通义" in out and "qwen" in out and "key:已配置" in out
    gf("model", "set", "通义", "model=qwen-max", "temp=0.7")
    capsys.readouterr()
    gf("model", "ls")
    out = capsys.readouterr().out
    assert "qwen-max" in out and "key:已配置" in out  # api_key 留空不覆盖
    gf("model", "rm", "通义")
    capsys.readouterr()
    gf("model", "ls")
    assert "通义" not in capsys.readouterr().out


def test_model_test_reports_result(server, capsys, monkeypatch):
    from app.services import llm

    async def fake_chat(mc, system, user, params=None, retries=3):
        return "pong", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake_chat)
    gf("login", "tester", "--server", server)
    gf("model", "add", "m", "--url", "http://x/v1", "--model", "q", "--key", "k")
    capsys.readouterr()
    gf("model", "test", "m")
    assert "连通正常" in capsys.readouterr().out


def test_data_up_head_rm(server, capsys, tmp_path):
    gf("login", "tester", "--server", server)
    f = tmp_path / "种子.jsonl"
    f.write_text('{"q": "问0"}\n{"q": "问1"}\n{"q": "问2"}\n', encoding="utf-8")
    gf("data", "up", str(f))
    assert "已上传 种子" in capsys.readouterr().out
    gf("data", "ls")
    out = capsys.readouterr().out
    assert "种子" in out and "3 行" in out
    gf("data", "head", "种子", "2")
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["q"] == "问0"
    gf("data", "rm", "种子")
    capsys.readouterr()
    gf("data", "ls")
    assert "种子" not in capsys.readouterr().out


def test_data_up_missing_file_dies(server, capsys):
    gf("login", "tester", "--server", server)
    with pytest.raises(SystemExit):
        gf("data", "up", "不存在.jsonl")
    assert "文件不存在" in capsys.readouterr().err


def test_data_download(server, capsys, tmp_path):
    gf("login", "tester", "--server", server)
    seed = tmp_path / "下载集.jsonl"
    seed.write_text('{"q": "甲"}\n{"q": "乙"}\n', encoding="utf-8")
    gf("data", "up", str(seed))
    out = tmp_path / "out.jsonl"
    capsys.readouterr()
    gf("data", "download", "下载集", "-o", str(out))
    assert "已下载" in capsys.readouterr().out
    lines = [json.loads(l) for l in out.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 2 and lines[0]["q"] == "甲"


def test_cli_full_chain(server, capsys, tmp_path, monkeypatch):
    from app.services import llm

    async def fake_chat(mc, system, user, params=None, retries=3):
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake_chat)
    seed = tmp_path / "种子.jsonl"
    seed.write_text('{"q": "问0"}\n{"q": "问1"}\n', encoding="utf-8")

    gf("login", "tester", "--server", server)
    gf("model", "add", "通义", "--url", "http://x/v1", "--model", "qwen", "--key", "k")
    gf("data", "up", str(seed))
    gf("wf", "add", "翻译流水线")
    gf("use", "翻译流水线")
    gf("node", "add", "input")
    gf("node", "set", "input_1", "dataset=种子")
    gf("node", "add", "llm")
    gf("node", "set", "llm_synth_1", "prompt=Q:{{q}}", "model=通义", "out=a")
    gf("node", "add", "output")
    gf("link", "input_1", "llm_synth_1")
    gf("link", "llm_synth_1", "output_1")
    capsys.readouterr()
    gf("run", "-f")
    out = capsys.readouterr().out
    assert "已启动" in out and "已完成" in out

    export_path = tmp_path / "导出.jsonl"
    gf("export", "1", "-o", str(export_path))
    lines = [json.loads(l) for l in
             export_path.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 2 and lines[0]["a"] == "答[Q:问0]"

    capsys.readouterr()
    gf("runs")
    assert "翻译流水线" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        gf("cancel", "1")
    assert "不可取消" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        gf("rerun", "1")
    assert "没有失败行" in capsys.readouterr().err


def test_watch_without_runs_dies(server, capsys):
    login_and_wf(server)
    with pytest.raises(SystemExit):
        gf("watch")
    assert "还没有运行记录" in capsys.readouterr().err


def test_http_node_add_set_show(server, capsys):
    login_and_wf(server)
    gf("node", "add", "http")
    capsys.readouterr()
    gf("node", "set", "http_fetch_1", "url=http://api/{{q}}", "method=GET",
       "extract=temp:data.temp,desc:data.weather.0.desc", "conc=8")
    capsys.readouterr()
    gf("node", "show", "http_fetch_1")
    node = json.loads(capsys.readouterr().out)
    assert node["type"] == "http_fetch"
    assert node["config"]["url"] == "http://api/{{q}}"
    assert node["config"]["method"] == "GET"
    assert node["config"]["extract"] == {"temp": "data.temp", "desc": "data.weather.0.desc"}
    assert node["config"]["concurrency"] == 8


def test_http_node_show_summary(server, capsys):
    login_and_wf(server)
    gf("node", "add", "http")
    gf("node", "set", "http_fetch_1", "url=http://api/x")
    capsys.readouterr()
    gf("show")
    out = capsys.readouterr().out
    assert "HTTP 取数" in out and "http://api/x" in out


def test_node_set_judge_models_and_pass_k(server, capsys):
    login_and_wf(server)
    gf("model", "add", "裁判甲", "--url", "http://x/v1", "--model", "q1", "--key", "k1")
    gf("model", "add", "裁判乙", "--url", "http://x/v1", "--model", "q2", "--key", "k2")
    gf("node", "add", "qc")
    capsys.readouterr()
    gf("node", "set", "qc_1", "judge_models=裁判甲,裁判乙", "pass_k=2")
    capsys.readouterr()
    gf("node", "show", "qc_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["judge_model_ids"] == [1, 2]
    assert node["config"]["pass_k"] == 2


def test_wf_rename(server, capsys):
    login_and_wf(server, "旧名")
    gf("wf", "rename", "旧名", "新名")
    capsys.readouterr()
    gf("wf", "ls")
    out = capsys.readouterr().out
    assert "新名" in out and "旧名" not in out


def test_cols_shows_lineage(server, capsys, tmp_path):
    gf("login", "tester", "--server", server)
    seed = tmp_path / "种子.jsonl"
    seed.write_text('{"q": "问0"}\n', encoding="utf-8")
    gf("data", "up", str(seed))
    gf("wf", "add", "血缘流"); gf("use", "血缘流")
    gf("node", "add", "input"); gf("node", "set", "input_1", "dataset=种子")
    gf("node", "add", "llm"); gf("node", "set", "llm_synth_1", "out=a")
    gf("link", "input_1", "llm_synth_1")
    capsys.readouterr()
    gf("cols")
    out = capsys.readouterr().out
    assert "llm_synth_1" in out and "q" in out and "a" in out


def test_cols_unknown_node_dies(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    with pytest.raises(SystemExit) as e:
        gf("cols", "不存在节点")
    assert e.value.code == 1
    assert "不存在" in capsys.readouterr().err


def test_node_prompt_from_file(server, capsys, tmp_path):
    login_and_wf(server)
    gf("node", "add", "llm")
    pf = tmp_path / "p.md"
    pf.write_text("# 指令\n把 {{q}} 翻译成英文\n", encoding="utf-8")
    gf("node", "prompt", "llm_synth_1", "--user", "--file", str(pf))
    capsys.readouterr()
    gf("node", "show", "llm_synth_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["user_prompt"] == "# 指令\n把 {{q}} 翻译成英文\n"


def test_node_prompt_from_stdin(server, capsys, monkeypatch, tmp_path):
    import io
    login_and_wf(server)
    gf("node", "add", "qc")
    monkeypatch.setattr("sys.stdin", io.StringIO("判定规则：必须为 JSON"))
    gf("node", "prompt", "qc_1", "--system", "-")
    capsys.readouterr()
    gf("node", "show", "qc_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["system_prompt"] == "判定规则：必须为 JSON"


def test_node_prompt_requires_field_and_source(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    with pytest.raises(SystemExit) as e:   # 缺 --system/--user：argparse 互斥必填
        gf("node", "prompt", "llm_synth_1", "--file", "x")
    assert e.value.code == 2


def test_node_set_new_keys(server, capsys):
    login_and_wf(server)
    gf("model", "add", "m", "--url", "http://x/v1", "--model", "q", "--key", "k")
    gf("node", "add", "llm")
    gf("node", "set", "llm_synth_1", "drop=secret,tmp", "outs=q_en,cat_en",
       "think=on", "effort=high")
    capsys.readouterr()
    gf("node", "show", "llm_synth_1")
    c = json.loads(capsys.readouterr().out)["config"]
    assert c["drop_columns"] == ["secret", "tmp"]
    assert c["output_columns"] == ["q_en", "cat_en"]
    assert c["params"]["thinking_enabled"] is True
    assert c["params"]["reasoning_effort"] == "high"


def test_node_set_qc_status_feedback_cols(server, capsys):
    login_and_wf(server)
    gf("node", "add", "qc")
    gf("node", "set", "qc_1", "status_col=verdict", "feedback_col=fb")
    capsys.readouterr()
    gf("node", "show", "qc_1")
    c = json.loads(capsys.readouterr().out)["config"]
    assert c["status_column"] == "verdict" and c["feedback_column"] == "fb"


def test_node_set_http_headers(server, capsys):
    login_and_wf(server)
    gf("node", "add", "http")
    gf("node", "set", "http_fetch_1", "headers=Authorization:Bearer x,X-Tag:demo")
    capsys.readouterr()
    gf("node", "show", "http_fetch_1")
    c = json.loads(capsys.readouterr().out)["config"]
    assert c["headers"] == {"Authorization": "Bearer x", "X-Tag": "demo"}


def test_node_set_think_off(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    gf("node", "set", "llm_synth_1", "think=off")
    capsys.readouterr()
    gf("node", "show", "llm_synth_1")
    assert json.loads(capsys.readouterr().out)["config"]["params"]["thinking_enabled"] is False


def test_wf_dump_load_roundtrip(server, capsys, tmp_path):
    login_and_wf(server, "导出流")
    gf("node", "add", "input"); gf("node", "add", "output")
    gf("link", "input_1", "output_1")
    dump = tmp_path / "graph.json"
    gf("wf", "dump", "-o", str(dump))
    graph = json.loads(dump.read_text(encoding="utf-8"))
    assert {n["id"] for n in graph["nodes"]} == {"input_1", "output_1"}
    # 改名后 load 回去
    gf("wf", "add", "空流"); gf("use", "空流")
    gf("wf", "load", str(dump))
    capsys.readouterr()
    gf("show")
    assert "input_1 -> output_1" in capsys.readouterr().out


def _build_and_run(server, tmp_path, monkeypatch):
    from app.services import llm

    async def fake_chat(mc, system, user, params=None, retries=3):
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake_chat)
    seed = tmp_path / "种子.jsonl"
    seed.write_text('{"q": "问0"}\n{"q": "问1"}\n', encoding="utf-8")
    gf("login", "tester", "--server", server)
    gf("model", "add", "通义", "--url", "http://x/v1", "--model", "qwen", "--key", "k")
    gf("data", "up", str(seed))
    gf("wf", "add", "链"); gf("use", "链")
    gf("node", "add", "input"); gf("node", "set", "input_1", "dataset=种子")
    gf("node", "add", "llm"); gf("node", "set", "llm_synth_1", "prompt=Q:{{q}}", "model=通义", "out=a")
    gf("node", "add", "output")
    gf("link", "input_1", "llm_synth_1"); gf("link", "llm_synth_1", "output_1")
    gf("run", "-f")


def test_rows_default_output_node(server, capsys, tmp_path, monkeypatch):
    _build_and_run(server, tmp_path, monkeypatch)
    capsys.readouterr()
    gf("rows", "1")
    out = capsys.readouterr().out
    assert "答[Q:问0]" in out and "答[Q:问1]" in out


def test_rows_specific_node(server, capsys, tmp_path, monkeypatch):
    _build_and_run(server, tmp_path, monkeypatch)
    capsys.readouterr()
    gf("rows", "1", "--node", "input_1")
    assert "问0" in capsys.readouterr().out


def test_logs_shows_timeline(server, capsys, tmp_path, monkeypatch):
    _build_and_run(server, tmp_path, monkeypatch)
    capsys.readouterr()
    gf("logs", "1")
    out = capsys.readouterr().out
    assert out.strip()   # 至少有日志行（含节点名/级别）


def test_qc_prints_metrics_and_failures(server, capsys, tmp_path):
    import json as _json
    from app.config import settings as _s
    from app.db import get_session_factory
    from app.models import Run, User, QcMetric, QcFailure
    from sqlalchemy import select
    import asyncio

    gf("login", "tester", "--server", server)

    async def seed():
        sf = get_session_factory()
        async with sf() as s:
            uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
            run = Run(user_id=uid, workflow_id=0, workflow_version_id=0, status="completed")
            s.add(run); await s.commit(); rid = run.id
            s.add(QcMetric(run_id=rid, node_id="qc1", total=10, first_round_pass=6))
            s.add(QcFailure(run_id=rid, node_id="qc1", sample_json='{"q":"x"}',
                            reasons_json=_json.dumps([{"model_config_id": 1, "status": "failed", "reason": "短"}])))
            await s.commit()
            return rid

    rid = asyncio.new_event_loop().run_until_complete(seed())
    capsys.readouterr()
    gf("qc", str(rid))
    out = capsys.readouterr().out
    assert "60" in out and "短" in out   # 首轮通过率 60% + 失败原因
    dl = tmp_path / "fail.jsonl"
    gf("qc", str(rid), "--download", "-o", str(dl))
    rec = _json.loads(dl.read_text(encoding="utf-8").strip().splitlines()[0])
    assert rec["_qc_model_1"] == "failed"
