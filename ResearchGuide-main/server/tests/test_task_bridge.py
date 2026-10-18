# -*- coding: utf-8 -*-
"""对话任务入口退出：旧客户端不能建任务；历史数据和其它模块继续保留。"""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dialogue
import llm
import store
import workbench

@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "task-exit.db")
    monkeypatch.setattr(llm, "enabled", lambda: False)
    store.init_db()

@pytest.mark.parametrize("kind", ["micro_task", "explore_direction", "course_action"])
@pytest.mark.parametrize("event", ["accept", "complete"])
def test_legacy_action_cannot_create_or_complete_tasks(kind, event):
    uid = store.create_user("旧客户端")["uid"]
    a = store.save_action(uid, conversation_id="c", action=kind, title="历史卡片", status="offered")
    with pytest.raises(ValueError, match="对话不再布置任务"):
        dialogue.action_event(uid, a["id"], event)
    assert store.list_tasks(uid) == []
    assert store.get_action(uid, a["id"])["status"] == "offered"
    h = dialogue.history(uid)
    assert h["pending_action"] is None and h["last_action"] is None and h["just_finished"] is None
    assert store.get_action(uid, a["id"])  # 不删除历史

def test_legacy_action_can_be_closed_without_creating_task():
    uid = store.create_user("旧客户端")["uid"]
    a = store.save_action(uid, conversation_id="c", action="micro_task", title="历史卡片")
    assert dialogue.action_event(uid, a["id"], "decline")["status"] == "abandoned"
    assert store.list_tasks(uid) == []

def test_legacy_tasks_and_tree_tasks_still_accessible():
    uid = store.create_user("历史数据")["uid"]
    a = store.save_action(uid, conversation_id="c", action="micro_task", title="历史卡片")
    # 模拟旧版已存在的任务，不经已关闭的对话入口创建。
    old = workbench.task_from_action(uid, a)
    tree = workbench.generate_task(uid, "ai", 1, title="特征")
    assert store.get_task(old.id).origin == "dialogue"
    assert store.get_task(tree.id).origin == "tree"
    assert len(store.list_tasks(uid)) == 2

def test_legacy_action_does_not_leak_across_portraits():
    uid = store.create_user("隔离")["uid"]
    a = store.save_action(uid, conversation_id="c", action="micro_task", title="历史卡片")
    store.open_new_portrait(uid)
    assert dialogue.action_event(uid, a["id"], "decline") is None
