"""memory_summarize 纯函数单元测试(不依赖 DB)。

覆盖 _merge_structured / _clean_items / _parse_summary_json。
"""
from app.services.memory_summarize import (
    PROJECT_CATEGORIES,
    _clean_items,
    _merge_structured,
    _parse_summary_json,
)


# ---------- _merge_structured ----------

def test_merge_structured_new_category_empty_existing():
    """空 existing + 新条目 → 正确分组,按固定类别顺序输出。"""
    new_items = [
        {"category": "Known Issues", "item": "codex exec needs bypass flag"},
        {"category": "Hard Constraints", "item": "wire_api must be responses"},
    ]
    result = _merge_structured("", new_items, PROJECT_CATEGORIES, 8000)
    assert "## Hard Constraints\n- wire_api must be responses" in result
    assert "## Known Issues\n- codex exec needs bypass flag" in result
    assert result.index("## Hard Constraints") < result.index("## Known Issues")


def test_merge_structured_dedup_same_category():
    """同类别重复 item → 精确去重。"""
    existing = "## Hard Constraints\n- wire_api must be responses"
    new_items = [
        {"category": "Hard Constraints", "item": "wire_api must be responses"},  # 重复
        {"category": "Hard Constraints", "item": "approval_policy must be never"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert result.count("wire_api must be responses") == 1
    assert "approval_policy must be never" in result


def test_merge_structured_preserves_free_text_no_headers():
    """无标题的 existing(用户自由笔记/旧格式残留)→ 作为 preamble 原样保留,新条目并入已知类别。"""
    existing = "用户手写的自由笔记,无标题\n第二行"
    new_items = [
        {"category": "Tech Stack", "item": "PostgreSQL"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert result.startswith("用户手写的自由笔记,无标题\n第二行")
    assert "## Tech Stack\n- PostgreSQL" in result
    assert "## Legacy Notes" not in result  # 不再为旧格式专门建块


def test_merge_structured_preserves_unknown_category():
    """用户自定义的未知类别块 → 原样保留,新条目并入已知类别。"""
    existing = (
        "## Tech Stack\n- PostgreSQL\n\n"
        "## My Custom Notes\n用户自定义内容\n保留原样"
    )
    new_items = [
        {"category": "Tech Stack", "item": "Redis"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert "## Tech Stack\n- PostgreSQL\n- Redis" in result
    assert "## My Custom Notes\n用户自定义内容\n保留原样" in result
    assert result.index("## Tech Stack") < result.index("## My Custom Notes")


def test_merge_structured_preserves_non_list_lines_in_category():
    """已知类别块内用户写的非列表行 → 原样保留,新条目追加到块末尾。"""
    existing = "## Hard Constraints\n这是用户写的说明段落\n- rule A"
    new_items = [
        {"category": "Hard Constraints", "item": "rule B"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert "这是用户写的说明段落" in result
    assert "- rule A" in result
    assert "- rule B" in result


def test_merge_structured_invalid_category_fallback():
    """非法 category → 兜底归入 Lessons Learned。"""
    new_items = [
        {"category": "Nonexistent Category", "item": "some lesson"},
    ]
    result = _merge_structured("", new_items, PROJECT_CATEGORIES, 8000)
    assert "## Lessons Learned\n- some lesson" in result


def test_merge_structured_truncate_drops_preamble_first():
    """超长时先删 preamble(游离文本相对最旧),保留类别块。"""
    big_preamble = "X" * 5000
    existing = big_preamble  # 无标题,整体作为 preamble
    new_items = [
        {"category": "Tech Stack", "item": "PostgreSQL"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 1000)
    assert "XXXX" not in result
    assert "## Tech Stack\n- PostgreSQL" in result


def test_merge_structured_no_change_when_all_dup():
    """所有新条目均已存在 → merged == existing(调用方据此跳过写入)。"""
    existing = "## Hard Constraints\n- rule A"
    new_items = [{"category": "Hard Constraints", "item": "rule A"}]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert result == existing


def test_merge_structured_only_outputs_nonempty_categories():
    """空类别不输出,避免空标题。"""
    new_items = [
        {"category": "Tech Stack", "item": "PostgreSQL"},
    ]
    result = _merge_structured("", new_items, PROJECT_CATEGORIES, 8000)
    assert "## Hard Constraints" not in result
    assert "## Known Issues" not in result
    assert "## Tech Stack" in result


# ---------- _clean_items ----------

def test_clean_items_filters_invalid():
    raw = [
        {"category": "Hard Constraints", "item": "valid"},
        {"category": "Tech Stack", "item": ""},      # 空 item 丢弃
        {"category": "", "item": "no cat"},           # 空 category 丢弃
        "not a dict",                                  # 非 dict 丢弃
        {"item": "no cat field"},                      # 无 category 丢弃
        {"category": "Known Issues", "item": "  spaced  "},  # 保留并 strip
    ]
    result = _clean_items(raw)
    assert result == [
        {"category": "Hard Constraints", "item": "valid"},
        {"category": "Known Issues", "item": "spaced"},
    ]


def test_clean_items_non_list_returns_empty():
    assert _clean_items(None) == []
    assert _clean_items("not a list") == []
    assert _clean_items({"a": 1}) == []


# ---------- _parse_summary_json ----------

def test_parse_summary_json_new_format():
    content = (
        '{"project_memory_update": [{"category": "Hard Constraints", "item": "rule"}], '
        '"global_memory_update": []}'
    )
    result = _parse_summary_json(content)
    assert result is not None
    assert result["project_memory_update"] == [{"category": "Hard Constraints", "item": "rule"}]
    assert result["global_memory_update"] == []


def test_parse_summary_json_markdown_fence():
    content = (
        '```json\n{"project_memory_update": [], "global_memory_update": '
        '[{"category": "Preferences", "item": "prefers English"}]}\n```'
    )
    result = _parse_summary_json(content)
    assert result is not None
    assert result["global_memory_update"] == [{"category": "Preferences", "item": "prefers English"}]


def test_parse_summary_json_invalid_returns_none():
    assert _parse_summary_json("") is None
    assert _parse_summary_json("not json at all") is None
    assert _parse_summary_json("[]") is None  # 非 dict
