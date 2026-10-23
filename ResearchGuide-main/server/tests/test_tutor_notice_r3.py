# -*- coding: utf-8 -*-
"""带读提示（Haiku 面板第三轮 B）：固定题库提示按原因分，模型恢复后不再提；待判写清已保存几题。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tutor  # noqa: E402


def _state(reason: str) -> dict:
    slots = [{"qid": "q1", "status": "pass"}, {"qid": "q2", "status": "pending"}]
    return {"status": "active", "rev": 1, "cur": 1, "plan": slots, "slots": slots, "stations": [],
            "rules_fallback": True, "rules_fallback_reason": reason}


def test_quota_notice():
    n = tutor.public(_state("quota"), ready=False)["notice"]
    assert "今天的模型额度用完了" in n
    assert "有 1 题已保存，等模型恢复后判分" in n
    assert "规则题" not in n


def test_model_notice():
    n = tutor.public(_state("model"), ready=False)["notice"]
    assert "模型刚才没响应" in n
    assert "规则题" not in n


def test_ready_hides_fallback():
    n = tutor.public(_state("quota"), ready=True)["notice"]
    assert "有 1 题已保存，等模型恢复后判分" in n
    assert "今天的模型额度用完了" not in n
    assert "模型刚才没响应" not in n
    assert "规则题" not in n
