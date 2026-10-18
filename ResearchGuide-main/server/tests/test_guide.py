from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guide
import memory
import store
from schemas import UserFact


@pytest.fixture
def uid(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "guide.db")
    store.init_db()
    return store.create_user("虚构首次引导")['uid']


def add(uid, key, value, source="declared"):
    store.add_fact(UserFact(user_id=uid, key=key, value=value, source=source, status="confirmed"))


def test_background_and_interest_are_not_enough(uid):
    add(uid, "grade", "大一")
    add(uid, "major", "数学")
    add(uid, "interest:ml", "机器学习")
    assert guide.assess(uid)["known_count"] == 2
    assert not guide.assess(uid)["ready"]


def test_registered_math_code_key_is_not_rejected_after_normalization(uid):
    assert memory.canon_key("base:math_code") == "base:math_code"
    assert memory.spec_for("base:math_code") is not None


def test_four_areas_with_topic_are_sufficient_without_every_field(uid):
    for key, value in [("interest:ml", "机器学习"), ("base:code", "只写过示例代码"),
                       ("reading_experience", "没读过论文"), ("reading_support", "希望术语先解释")]:
        add(uid, key, value)
    profile = guide.assess(uid)
    assert profile["ready"] and profile["known_count"] == 4
    assert profile["missing"] == ["background"]


def test_unknown_topic_privacy_preferences_and_inferences_do_not_fill_areas(uid):
    add(uid, "interest:unknown", "还没想好")
    add(uid, "style:transcript", "不想分享成绩单")
    add(uid, "grade", "大二", source="inferred")
    assert guide.assess(uid)["known_count"] == 0


def test_user_corrected_background_remains_known(uid):
    add(uid, "grade", "大一", source="user_edit")
    background = next(a for a in guide.assess(uid)["areas"] if a['id'] == 'background')
    assert background['known'] and background['summary'] == '大一'


def test_many_course_or_ability_facts_only_cover_one_area(uid):
    for i in range(12):
        add(uid, f"capability:topic{i}", f"接触过基础{i}")
    profile = guide.assess(uid)
    assert profile["known_count"] == 1
    assert not profile["ready"]


def test_legacy_done_phase_or_direction_alone_does_not_bypass_readiness(uid):
    store.set_onboard_state(uid, {"phase": "done"})
    add(uid, "direction:ai", "人工智能")
    assert not guide.assess(uid)["ready"]


def test_old_preview_is_available_beyond_recent_message_window(uid):
    import dialogue
    store.add_message(uid, "assistant", "识别出 22 门课。本次只预览，没有保存课程，也没有更新能力记录。")
    for i in range(15):
        store.add_message(uid, "user", f"无关的后续交流{i}")
    assert dialogue.build_env(uid, "", "之前预览过吗")["guidance"]["transcript_history"] == [
        {"mode": "preview", "count": 22, "saved": False}]
    assert store.list_enrollments(uid) == []


def test_readiness_recomputes_after_user_removes_a_fact(uid):
    for key, value in [("grade", "大一"), ("interest:ml", "机器学习"),
                       ("base:math", "学过微积分"), ("reading_support", "希望先举例")]:
        add(uid, key, value)
    assert guide.assess(uid)["ready"]
    with store._conn() as c:
        c.execute("UPDATE facts SET status='retracted' WHERE user_id=? AND key='base:math'", (uid,))
    assert not guide.assess(uid)["ready"]


def test_reading_summary_does_not_repeat_barrier_already_in_experience(uid):
    add(uid, "reading_experience", "读过中文综述，英文原文读得慢")
    add(uid, "reading_barrier", "英文原文读得慢")
    reading = next(a for a in guide.assess(uid)["areas"] if a['id'] == 'reading')
    assert reading['summary'] == '读过中文综述，英文原文读得慢'
