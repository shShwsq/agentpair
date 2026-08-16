"""自动生成练习题(auto_generate)守卫分支单元测试

不依赖数据库:mock Session 的 query 链,按调用顺序注入返回值,
覆盖 orchestrator 钩子里 auto_generate_practice_for_task 的全部守卫:
- 匿名任务(user_id 为空)跳过
- 用户开关关闭跳过(开关开启/未配置不拦)
- 同一任务已有题目(任意状态)跳过
- 无结构化 finding(无 cwe/severity 元信息)跳过
- 命中守卫后调 generator,返回生成数
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.practice.auto_generate import auto_generate_practice_for_task


def _mock_db(pref_row=None, existing_question=None, result_metas=None):
    """按 auto_generate 内的 query 顺序注入:
    1) UserPreference 2) Question.id 3) Result.metadata_
    """
    q_pref = MagicMock()
    q_pref.filter.return_value.first.return_value = pref_row
    q_question = MagicMock()
    q_question.filter.return_value.first.return_value = existing_question
    q_result = MagicMock()
    q_result.filter.return_value.all.return_value = [
        (m,) for m in (result_metas or [])
    ]
    db = MagicMock()
    db.query.side_effect = [q_pref, q_question, q_result]
    return db


def _task(user_id=None):
    t = MagicMock()
    t.id = uuid4()
    t.user_id = user_id or uuid4()
    return t


def test_skip_anonymous_task():
    db = MagicMock()
    task = MagicMock()
    task.user_id = None
    assert auto_generate_practice_for_task(task, db) == 0
    db.query.assert_not_called()


def test_skip_when_preference_off():
    pref = MagicMock()
    pref.auto_generate_practice = False
    db = _mock_db(pref_row=pref)
    with patch("app.services.practice.auto_generate.generate_questions_for_task") as gen:
        assert auto_generate_practice_for_task(_task(), db) == 0
    gen.assert_not_called()


def test_skip_when_questions_already_exist():
    db = _mock_db(pref_row=None, existing_question=(uuid4(),))
    with patch("app.services.practice.auto_generate.generate_questions_for_task") as gen:
        assert auto_generate_practice_for_task(_task(), db) == 0
    gen.assert_not_called()


def test_skip_without_structured_findings():
    # 纯摘要 Result:metadata 为空/无 cwe/severity/空串
    for metas in ([], [None], [{}], [{"title": "x"}], [{"severity": "   "}]):
        db = _mock_db(pref_row=None, result_metas=metas)
        with patch("app.services.practice.auto_generate.generate_questions_for_task") as gen:
            assert auto_generate_practice_for_task(_task(), db) == 0
        gen.assert_not_called()


def test_generate_with_cwe_meta():
    db = _mock_db(pref_row=None, result_metas=[{"cwe": "CWE-89"}])
    task = _task()
    with patch(
        "app.services.practice.auto_generate.generate_questions_for_task",
        return_value=([MagicMock()] * 3, 1),
    ) as gen:
        assert auto_generate_practice_for_task(task, db) == 3
    gen.assert_called_once_with(db, task, task.user_id)


def test_generate_with_severity_meta():
    db = _mock_db(pref_row=None, result_metas=[{}, {"severity": "high"}])
    with patch(
        "app.services.practice.auto_generate.generate_questions_for_task",
        return_value=([MagicMock()], 0),
    ) as gen:
        assert auto_generate_practice_for_task(_task(), db) == 1
    gen.assert_called_once()


def test_preference_on_does_not_block():
    pref = MagicMock()
    pref.auto_generate_practice = True
    db = _mock_db(pref_row=pref, result_metas=[{"cwe": "79"}])
    with patch(
        "app.services.practice.auto_generate.generate_questions_for_task",
        return_value=([MagicMock()], 0),
    ) as gen:
        assert auto_generate_practice_for_task(_task(), db) == 1
