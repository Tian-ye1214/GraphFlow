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
