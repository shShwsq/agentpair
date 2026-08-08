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
    # Hard Constraints 应在 Known Issues 之前(固定顺序)
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


def test_merge_structured_legacy_compat_old_format():
    """旧 \\n---\\n 格式 existing → 整体归入 ## Legacy Notes,分隔符清理,新条目并入。"""
    existing = "旧的中文记忆块1\n---\n旧的中文记忆块2"
    new_items = [
        {"category": "Tech Stack", "item": "PostgreSQL"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert "## Legacy Notes" in result
    assert "旧的中文记忆块1" in result
    assert "旧的中文记忆块2" in result
    assert "\n---\n" not in result  # 旧分隔符已清理
    assert "## Tech Stack\n- PostgreSQL" in result
    # Legacy 块在最末
    assert result.index("## Tech Stack") < result.index("## Legacy Notes")


def test_merge_structured_legacy_block_preserved_when_new_format():
    """已含 Legacy 块的新格式 existing → Legacy 块原样保留,新条目并入对应类别。"""
    existing = (
        "## Tech Stack\n- PostgreSQL\n\n"
        "## Legacy Notes\n旧记忆原样保留"
    )
    new_items = [
        {"category": "Tech Stack", "item": "Redis"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert "## Tech Stack\n- PostgreSQL\n- Redis" in result
    assert "## Legacy Notes\n旧记忆原样保留" in result


def test_merge_structured_invalid_category_fallback():
    """非法 category → 兜底归入 Lessons Learned。"""
    new_items = [
        {"category": "Nonexistent Category", "item": "some lesson"},
    ]
    result = _merge_structured("", new_items, PROJECT_CATEGORIES, 8000)
    assert "## Lessons Learned\n- some lesson" in result


def test_merge_structured_truncate_drops_legacy_first():
    """超长时先删 Legacy 块以腾出空间。"""
    big_legacy = "X" * 5000
    existing = f"## Legacy Notes\n{big_legacy}"
    new_items = [
        {"category": "Tech Stack", "item": "PostgreSQL"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 1000)
    # Legacy 块应被删除
    assert "## Legacy Notes" not in result
    assert "## Tech Stack\n- PostgreSQL" in result


def test_merge_structured_no_change_when_all_dup():
    """所有新条目均已存在 → merged == existing(调用方据此跳过写入)。"""
    existing = "## Hard Constraints\n- rule A"
    new_items = [{"category": "Hard Constraints", "item": "rule A"}]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert result == existing


def test_merge_structured_only_outputs_nonempty_categories():
    """空类别不输出,避免空 ## 标题。"""
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
