"""题目生成(generator)解析与规范化单元测试

覆盖(不依赖数据库,直接喂 LLM 原始输出字符串):
- 合法 JSON 数组解析 / 单对象包裹 / markdown 围栏容错(json_repair)
- 字段校验:qtype 白名单、选项数、answer_idx 越界、全同选项
- true_false 强制选项 ["正确","错误"]
- CWE 元信息优先于 LLM 输出的 knowledge_key(含纯数字补前缀)
- 单条 finding 最多 3 题截断
- dedup_hash 稳定性
- 主题提示词切换(build_system_prompt)与工具说明段注入
- 迷你工具循环(_call_llm / _execute_practice_tool)
- 出题前工作区保障(_ensure_workspace 重新 clone 恢复)
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.services.practice.generator as gen
from app.services.practice.generator import (
    _MAX_TOOL_RESULT_CHARS,
    _call_llm,
    _ensure_workspace,
    _execute_practice_tool,
    _normalize_raw_question,
    _parse_llm_questions,
    build_system_prompt,
    compute_dedup_hash,
)

VALID_Q = {
    "qtype": "single_choice",
    "stem": "该代码存在哪种漏洞?",
    "code_snippet": "cursor.execute(sql)",
    "options": ["SQL 注入", "XSS", "CSRF", "无漏洞"],
    "answer_idx": 0,
    "explanation": "拼接 SQL 导致注入",
    "difficulty": 3,
    "knowledge_key": "CWE-89",
    "knowledge_name": "SQL 注入",
}


def _raw(**overrides):
    q = dict(VALID_Q)
    q.update(overrides)
    return q


# ============================================================
# 解析(json_repair 容错)
# ============================================================


def test_parse_json_array():
    import json

    content = json.dumps([_raw()], ensure_ascii=False)
    qs = _parse_llm_questions(content, {})
    assert len(qs) == 1
    assert qs[0]["qtype"] == "single_choice"
    assert qs[0]["answer_idx"] == 0


def test_parse_single_object_wrapped():
    import json

    content = json.dumps(_raw(), ensure_ascii=False)
    qs = _parse_llm_questions(content, {})
    assert len(qs) == 1


def test_parse_markdown_fence_tolerated():
    import json

    content = "```json\n" + json.dumps([_raw()], ensure_ascii=False) + "\n```"
    qs = _parse_llm_questions(content, {})
    assert len(qs) == 1


def test_parse_empty_or_garbage():
    assert _parse_llm_questions("", {}) == []
    assert _parse_llm_questions("完全不是 JSON 的散文", {}) == []


def test_parse_max_3_questions():
    import json

    content = json.dumps([_raw(stem=f"题干{i}") for i in range(5)], ensure_ascii=False)
    qs = _parse_llm_questions(content, {})
    assert len(qs) == 3


# ============================================================
# 字段校验
# ============================================================


def test_invalid_qtype_rejected():
    assert _normalize_raw_question(_raw(qtype="essay"), {}) is None
    assert _normalize_raw_question(_raw(qtype=""), {}) is None


def test_empty_stem_rejected():
    assert _normalize_raw_question(_raw(stem="  "), {}) is None


def test_answer_idx_out_of_range():
    assert _normalize_raw_question(_raw(answer_idx=9), {}) is None
    assert _normalize_raw_question(_raw(answer_idx=-1), {}) is None
    # answer_idx 非整数
    assert _normalize_raw_question(_raw(answer_idx="x"), {}) is None


def test_too_few_or_many_options():
    assert _normalize_raw_question(_raw(options=["A"]), {}) is None
    assert _normalize_raw_question(_raw(options=[str(i) for i in range(9)]), {}) is None


def test_duplicate_options_rejected():
    # 全同选项无效;含重复但非全同时保留(去重不是解析层职责)
    assert _normalize_raw_question(_raw(options=["A", "A"]), {}) is None
    assert _normalize_raw_question(_raw(options=["A", "A", "B"]), {}) is not None


def test_true_false_options_forced():
    q = _normalize_raw_question(_raw(
        qtype="true_false",
        stem="参数化查询可防御 SQL 注入",
        options=["对", "错", "不确定"],
        answer_idx=1,
    ), {})
    assert q is not None
    assert q["options"] == ["正确", "错误"]
    assert q["answer_idx"] == 1  # 索引保留(1 仍在新选项范围内)


def test_difficulty_clamped_and_default():
    assert _normalize_raw_question(_raw(difficulty=9), {})["difficulty"] == 5.0
    assert _normalize_raw_question(_raw(difficulty=0), {})["difficulty"] == 1.0
    assert _normalize_raw_question(_raw(difficulty="abc"), {})["difficulty"] == 3.0


# ============================================================
# knowledge_key:CWE 元信息优先
# ============================================================


def test_cwe_from_finding_meta_wins():
    q = _normalize_raw_question(_raw(knowledge_key="injection"), {"cwe": "CWE-79"})
    assert q["knowledge_key"] == "CWE-79"


def test_numeric_cwe_prefixed():
    q = _normalize_raw_question(_raw(), {"cwe": "89"})
    assert q["knowledge_key"] == "CWE-89"


def test_fallback_to_llm_key_then_general():
    assert _normalize_raw_question(_raw(), {})["knowledge_key"] == "CWE-89"  # LLM 输出
    q = _normalize_raw_question(_raw(knowledge_key="  "), {})
    assert q["knowledge_key"] == "general"


# ============================================================
# 去重哈希
# ============================================================


def test_dedup_hash_stable_and_trim_insensitive():
    h1 = compute_dedup_hash("题干", "code")
    h2 = compute_dedup_hash("  题干  ", "  code  ")
    assert h1 == h2
    assert h1 != compute_dedup_hash("另一题干", "code")


def test_dedup_hash_none_snippet():
    assert len(compute_dedup_hash("题干", None)) == 64


# ============================================================
# 主题提示词
# ============================================================


def test_build_system_prompt_topic_switch():
    assert "网络安全培训出题专家" in build_system_prompt("security", False)
    assert "架构培训出题专家" in build_system_prompt("architecture", False)
    assert "通用编码能力培训出题专家" in build_system_prompt("coding", False)


def test_build_system_prompt_unknown_topic_falls_back_security():
    assert "网络安全培训出题专家" in build_system_prompt("not_a_topic", False)


def test_build_system_prompt_tool_section_only_with_workspace():
    assert "源码查阅工具" in build_system_prompt("security", True)
    assert "源码查阅工具" not in build_system_prompt("security", False)


def test_build_system_prompt_common_rules_always_present():
    for topic in ("security", "architecture", "coding"):
        prompt = build_system_prompt(topic, True)
        assert "只输出 JSON 数组" in prompt
        assert "verified: false" in prompt  # 误报发现不出题提示


# ============================================================
# 迷你工具循环
# ============================================================


def _chunk(content=None, tool_deltas=None):
    """伪造 StreamChunk(只含 _stream_one_round 用到的字段)"""
    return SimpleNamespace(
        content_delta=content,
        tool_call_deltas=tool_deltas or [],
        finish_reason="stop",
    )


def _delta(index=0, id=None, name=None, args=""):
    return SimpleNamespace(
        index=index, id=id, name=name, arguments_fragment=args,
    )


class _FakeClient:
    """按轮次依次返回预设 chunk 序列,并记录每次调用"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    def chat_stream(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        return iter(self.rounds.pop(0))


def test_call_llm_no_workspace_single_call_without_tools():
    client = _FakeClient([[_chunk(content="[]")]])
    out = _call_llm(client, "sys", "finding 文本", "t1", "")
    assert out == "[]"
    assert len(client.calls) == 1
    assert client.calls[0]["kw"].get("tools") is None


def test_call_llm_tool_loop_executes_tool_then_finalizes(monkeypatch):
    """第一轮 LLM 要求 read_file,执行后第二轮直出题目"""
    read_calls = []

    def fake_read_file(repo_path, file_path, **kw):
        read_calls.append((repo_path, file_path))
        return {"path": file_path, "content": "def foo(): pass"}

    monkeypatch.setattr(gen.sandbox_tools, "read_file", fake_read_file)
    client = _FakeClient([
        [_chunk(tool_deltas=[
            _delta(id="call_1", name="read_file"),
            _delta(args='{"file_path": "src/a.py"}'),
        ])],
        [_chunk(content="[{\"qtype\": \"single_choice\"}]")],
    ])
    out = _call_llm(client, "sys", "finding 文本", "t1", "/repo")
    assert "single_choice" in out
    assert read_calls == [("/repo", "src/a.py")]
    # 第一轮带工具,第二轮收到 tool 结果消息
    assert client.calls[0]["kw"].get("tools") is not None
    msgs = client.calls[1]["messages"]
    assert any(m["role"] == "tool" and "src/a.py" in m["content"] for m in msgs)


def test_execute_practice_tool_unknown_and_truncate(monkeypatch):
    assert "未知工具" in _execute_practice_tool("t1", "/repo", "hack", {})
    monkeypatch.setattr(
        gen.sandbox_tools, "read_file",
        lambda *a, **k: {"content": "x" * 100000},
    )
    out = _execute_practice_tool("t1", "/repo", "read_file", {"file_path": "a.py"})
    assert len(out) == _MAX_TOOL_RESULT_CHARS


def test_execute_practice_tool_failure_returns_text(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("session gone")

    monkeypatch.setattr(gen.sandbox_tools, "search_code", _raise)
    out = _execute_practice_tool("t1", "/repo", "search_code", {"pattern": "x"})
    assert out.startswith("工具执行失败")


# ============================================================
# 出题前工作区保障(_ensure_workspace)
# ============================================================


def _task_with_repo():
    task = MagicMock()
    task.id = "t1"
    task.params = {"repo_url": "https://example.com/r.git", "branch": "dev"}
    return task


def test_ensure_workspace_alive_reuses_no_clone(monkeypatch):
    monkeypatch.setattr(
        gen.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo", "mode": "sandbox"},
    )
    clone_calls = []
    monkeypatch.setattr(
        gen.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **k: clone_calls.append(1),
    )
    info = _ensure_workspace(MagicMock(), _task_with_repo(), None)
    assert info["repo_path"] == "/repo"
    assert not clone_calls


def test_ensure_workspace_setting_off_no_clone(monkeypatch):
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    clone_calls = []
    monkeypatch.setattr(
        gen.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **k: clone_calls.append(1),
    )
    pref = MagicMock()
    pref.restore_workspace_for_practice = False
    assert _ensure_workspace(MagicMock(), _task_with_repo(), pref) is None
    assert not clone_calls


def test_ensure_workspace_restores_when_enabled(monkeypatch):
    """session 已清理 + 开关开启 → 重新 clone 并纳入 TTL 清理序列"""
    state = {"info": None}
    monkeypatch.setattr(
        gen.sandbox_tools, "get_workspace_info", lambda tid: state["info"],
    )
    clone_calls = []

    def fake_clone(repo_url, branch=None, task_id="", git_tokens=None, **kw):
        clone_calls.append((repo_url, branch, task_id))
        state["info"] = {"repo_path": "/repo-restored"}

    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", fake_clone)
    marked = []
    monkeypatch.setattr(
        gen.sandbox_tools, "mark_task_completed", lambda tid: marked.append(tid),
    )
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {"github": "tk"})
    pref = MagicMock()
    pref.restore_workspace_for_practice = True
    info = _ensure_workspace(MagicMock(), _task_with_repo(), pref)
    assert info == {"repo_path": "/repo-restored"}
    assert clone_calls == [("https://example.com/r.git", "dev", "t1")]
    assert marked == ["t1"]


def test_ensure_workspace_clone_failure_degrades(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("network error")

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", _raise)
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {})
    pref = MagicMock()
    pref.restore_workspace_for_practice = True
    # 失败静默降级返回 None(出题走无工具路径),不抛异常
    assert _ensure_workspace(MagicMock(), _task_with_repo(), pref) is None


def test_ensure_workspace_no_repo_url(monkeypatch):
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    clone_calls = []
    monkeypatch.setattr(
        gen.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **k: clone_calls.append(1),
    )
    task = MagicMock()
    task.id = "t1"
    task.params = {}
    pref = MagicMock()
    pref.restore_workspace_for_practice = True
    assert _ensure_workspace(MagicMock(), task, pref) is None
    assert not clone_calls
