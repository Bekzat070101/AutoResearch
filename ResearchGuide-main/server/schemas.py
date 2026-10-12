# -*- coding: utf-8 -*-
"""核心数据契约（对应 docs/ARCHITECTURE.md §接口冻结）。

W0 demo 说明：
- Fact / NBA / MicroTask / Submission / Feedback 五个 schema 与正式契约一致；
- LLM 环节（onboarding 抽取、planner 决策、任务反馈）按 NEXT_PRE §3 白名单 mock，
  但数据结构与闭环逻辑是真实的。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid4().hex


FactCategory = Literal["background", "interest", "capability", "preference", "experience"]
# derived：从成绩单这类**官方底稿**由代码确定性推出来的（模型写不了，见 memory.py）
# user_edit：用户在界面上亲手改的
FactSource = Literal["declared", "inferred", "behavior", "derived", "user_edit"]
# superseded：被更新的事实取代（例如换了方向）。任何按状态取事实的地方都必须用白名单，
# 不能用排除列表，否则新增状态会静默漏进决策（见 memory.py 的 ACTIVE_STATUSES）。
# retracted：用户删掉的（对话/记忆层走 memory.user_retract，留 revision 可追溯）；
# deleted：同样表示「不再算数」，研读层用它作废一条结论。两个都**不能**进决策。
FactStatus = Literal["draft", "confirmed", "active", "superseded", "dismissed", "deleted", "retracted"]

# 参与决策的事实状态白名单。按状态筛选一律用白名单，不要用排除列表。
DECISION_STATUSES: tuple[str, ...] = ("confirmed", "active")
# 需要用户核对、还没生效的状态。
DRAFT_STATUSES: tuple[str, ...] = ("draft",)


class UserFact:
    """用户模型的最小单元：一条有证据、可解释的事实。"""

    def __init__(
        self,
        *,
        id: str | None = None,
        user_id: str = "",
        category: FactCategory = "interest",
        key: str = "",
        value: str = "",
        confidence: float = 0.6,
        source: FactSource = "declared",
        evidence: list[dict[str, Any]] | None = None,
        status: FactStatus = "draft",
        valid_until: str | None = None,
        affects: str = "",
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        self.id = id or new_id()
        self.user_id = user_id
        self.category = category
        self.key = key
        self.value = value
        self.confidence = round(float(confidence), 2)
        self.source = source
        self.evidence = evidence or []
        self.status = status
        # 临时约束的有效期（如「本周只有十分钟」）。到点后不再进环境包，见 memory.is_expired。
        self.valid_until = valid_until
        # 这条记忆改变未来的哪个决策（写入时举证，见 memory.AFFECTS）。
        self.affects = affects or ""
        self.created_at = created_at or now_iso()
        self.updated_at = updated_at or self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "category": self.category,
            "key": self.key,
            "value": self.value,
            "confidence": self.confidence,
            "source": self.source,
            "evidence": self.evidence,
            "status": self.status,
            "valid_until": self.valid_until,
            "affects": self.affects,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UserFact":
        return cls(
            id=d.get("id"),
            user_id=d.get("user_id", ""),
            category=d.get("category", "interest"),
            key=d.get("key", ""),
            value=d.get("value", ""),
            confidence=d.get("confidence", 0.6),
            source=d.get("source", "declared"),
            evidence=d.get("evidence") or [],
            status=d.get("status", "draft"),
            valid_until=d.get("valid_until"),
            affects=d.get("affects", ""),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )


class NBA:
    """Next Best Action：一次行动建议，rationale 必须引用事实。"""

    def __init__(
        self,
        *,
        action: str,
        title: str,
        rationale: str,
        rationale_facts: list[str] | None = None,
        alternatives: list[dict[str, Any]] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.action = action  # explore_direction | micro_task | review_progress
        self.title = title
        self.rationale = rationale
        self.rationale_facts = rationale_facts or []  # 引用的 fact id
        self.alternatives = alternatives or []
        self.payload = payload or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "title": self.title,
            "rationale": self.rationale,
            "rationale_facts": self.rationale_facts,
            "alternatives": self.alternatives,
            "payload": self.payload,
        }


class MicroTask:
    """微任务：10–30 分钟、带步骤与评分标准的真实小任务。

    **一个任务必须能回答三个问题**，否则用户不知道要干什么：
      做什么   steps        —— 具体动作
      交什么   deliverable  —— 交上来的东西长什么样（一段话？一张表？截图？）
      怎样算过 rubric       —— 逐条可判定，不是「做得不错」

    origin / action_id 是「这个任务从哪来」：
      origin="dialogue" —— 对话里「就做这个」产生的，action_id 指回那张行动卡
      origin="tree"     —— 任务区里点知识树节点产生的
    两者**必须都能在任务区看到**，所以 origin 是数据字段而不是各自的列表。
    """

    def __init__(
        self,
        *,
        id: str | None = None,
        user_id: str = "",
        direction: str = "",
        title: str = "",
        brief: str = "",
        steps: list[str] | None = None,
        deliverable: str = "",
        rubric: list[dict[str, str]] | None = None,
        time_budget_min: int = 20,
        difficulty: int = 1,
        status: str = "open",  # open | submitted | done
        created_at: str | None = None,
        origin: str = "tree",
        action_id: str = "",
        node_path: str = "",
        formal: dict[str, Any] | None = None,
    ) -> None:
        self.id = id or new_id()
        self.user_id = user_id
        self.direction = direction
        self.title = title
        self.brief = brief
        self.steps = steps or []
        self.deliverable = deliverable
        self.rubric = rubric or []
        self.time_budget_min = time_budget_min
        self.difficulty = difficulty
        self.status = status
        self.created_at = created_at or now_iso()
        self.origin = origin
        self.action_id = action_id
        self.node_path = node_path
        self.formal = formal or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "direction": self.direction,
            "title": self.title,
            "brief": self.brief,
            "steps": self.steps,
            "deliverable": self.deliverable,
            "rubric": self.rubric,
            "time_budget_min": self.time_budget_min,
            "difficulty": self.difficulty,
            "status": self.status,
            "created_at": self.created_at,
            "origin": self.origin,
            "action_id": self.action_id,
            "node_path": self.node_path,
            "formal": self.formal or {},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MicroTask":
        return cls(
            id=d.get("id"),
            user_id=d.get("user_id", ""),
            direction=d.get("direction", ""),
            title=d.get("title", ""),
            brief=d.get("brief", ""),
            steps=d.get("steps") or [],
            # 老任务没有 deliverable，回退到 brief，界面上不会出现空白格
            deliverable=d.get("deliverable") or "",
            rubric=d.get("rubric") or [],
            time_budget_min=d.get("time_budget_min", 20),
            difficulty=d.get("difficulty", 1),
            status=d.get("status", "open"),
            created_at=d.get("created_at"),
            origin=d.get("origin") or "tree",
            action_id=d.get("action_id") or "",
            node_path=d.get("node_path") or "",
            formal=d.get("formal") if isinstance(d.get("formal"), dict) else {},
        )


class Feedback:
    """任务反馈：rubric 逐条判定 + 下一步提示，不评价人格。"""

    def __init__(
        self,
        *,
        score: int,
        rubric: list[dict[str, Any]],
        next_hint: str,
        encouragement: str,
        learned_facts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.score = score  # 0-100
        self.rubric = rubric  # [{criterion, pass, comment}]
        self.next_hint = next_hint
        self.encouragement = encouragement
        self.learned_facts = learned_facts or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "rubric": self.rubric,
            "next_hint": self.next_hint,
            "encouragement": self.encouragement,
            "learned_facts": self.learned_facts,
        }
