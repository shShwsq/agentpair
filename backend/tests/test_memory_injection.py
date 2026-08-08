"""memory_injection 单元测试(用 MagicMock 模拟 DB Session,不连真实 DB)。

覆盖 build_react_agent_memory_section / build_user_agent_memory_section /
build_global_memory_section。
"""
from unittest.mock import MagicMock

from app.services.memory_injection import (
    build_global_memory_section,
    build_react_agent_memory_section,
    build_user_agent_memory_section,
)


def _mock_db(first_result=None):
    """构造 mock db:db.query(Model).filter(...).first() 统一返回 first_result。"""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = first_result
    return db


# ---------- build_react_agent_memory_section ----------

def test_react_section_empty_when_no_user():
    db = _mock_db()
    assert build_react_agent_memory_section(db, None, "https://github.com/a/b") == ""


def test_react_section_empty_when_no_repo():
    db = _mock_db()
    assert build_react_agent_memory_section(db, 1, "") == ""
    assert build_react_agent_memory_section(db, 1, None) == ""


def test_react_section_empty_when_no_project():
    db = _mock_db(first_result=None)  # Project 查不到
    assert build_react_agent_memory_section(db, 1, "https://github.com/a/b") == ""


def test_react_section_empty_when_no_memory_content():
    proj = MagicMock()
    proj.memory_content = ""
    proj.memory_summary = ""
    proj.alias = None
    db = _mock_db(first_result=proj)
    assert build_react_agent_memory_section(db, 1, "https://github.com/a/b") == ""


def test_react_section_uses_memory_summary():
    """memory_summary 非空 → 注入精简版(非 memory_content)+ 路径提示。"""
    proj = MagicMock()
    proj.memory_content = "## Hard Constraints\n- 这是很长的完整记忆不应出现"
    proj.memory_summary = "## Hard Constraints\n- 精简版摘要"
    proj.alias = "my-repo"
    db = _mock_db(first_result=proj)
    result = build_react_agent_memory_section(db, 1, "https://github.com/a/b")
    # 引导语开头(非 ## 标题,避免与内层 ## 类别层级冲突)
    assert result.startswith("以下是你对该项目的已知问题与历史记忆")
    assert "项目别名: my-repo" in result
    # 注入的是精简版,不是完整 memory_content
    assert "## Hard Constraints\n- 精简版摘要" in result
    assert "这是很长的完整记忆不应出现" not in result
    # 末尾有完整记忆文件路径提示
    assert "/home/user/.agent_memory/project_memory.md" in result


def test_react_section_summary_empty_falls_back_to_content():
    """memory_summary 为空 → 回退用 memory_content(截断)+ 路径提示(兼容旧数据)。"""
    proj = MagicMock()
    proj.memory_content = "## Hard Constraints\n- rule A\n\n## Known Issues\n- issue B"
    proj.memory_summary = ""
    proj.alias = None
    db = _mock_db(first_result=proj)
    result = build_react_agent_memory_section(db, 1, "https://github.com/a/b")
    assert result.startswith("以下是你对该项目的已知问题与历史记忆")
    # 回退用完整 memory_content
    assert "## Hard Constraints\n- rule A" in result
    assert "## Known Issues\n- issue B" in result
    # 末尾有路径提示
    assert "/home/user/.agent_memory/project_memory.md" in result


# ---------- build_user_agent_memory_section ----------

def test_user_agent_section_empty_when_no_user():
    db = _mock_db()
    assert build_user_agent_memory_section(db, None) == ""


def test_user_agent_section_empty_when_no_pref_no_memory():
    # UserPreference 和 UserMemory 都查不到 → first 均返回 None
    db = _mock_db(first_result=None)
    assert build_user_agent_memory_section(db, 1) == ""


def test_user_agent_section_pref_only():
    pref = MagicMock()
    pref.preferences = {
        "output_language": "中文",
        "focus_areas": ["security", "perf"],
        "style": "strict",
    }
    pref.custom_prompt = "be thorough"
    # 先查 UserPreference(返回 pref),再查 UserMemory(返回 None)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [pref, None]
    result = build_user_agent_memory_section(db, 1)
    assert "## 用户偏好" in result
    assert "输出语言: 中文" in result
    assert "重点关注领域: security, perf" in result
    assert "评判风格: strict" in result
    assert "自定义补充: be thorough" in result
    # 无全局记忆段
    assert "长期记忆" not in result


def test_user_agent_section_global_memory_with_categories():
    mem = MagicMock()
    mem.content = "## Hard Constraints\n- rule A\n\n## Preferences\n- prefers English"
    # 无 pref,有 mem
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [None, mem]
    result = build_user_agent_memory_section(db, 1)
    assert "以下是跨任务积累的长期记忆" in result
    assert "## Hard Constraints\n- rule A" in result
    assert "## Preferences\n- prefers English" in result


def test_user_agent_section_both_pref_and_memory():
    pref = MagicMock()
    pref.preferences = {"output_language": "English"}
    pref.custom_prompt = ""
    mem = MagicMock()
    mem.content = "## Tech Stack\n- PostgreSQL"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [pref, mem]
    result = build_user_agent_memory_section(db, 1)
    # 两段都有,用 \n\n 拼接
    assert "## 用户偏好" in result
    assert "以下是跨任务积累的长期记忆" in result
    assert "## Tech Stack\n- PostgreSQL" in result


def test_user_agent_section_with_project_memory():
    """repo_url 非空 + 有 Project.memory_summary → 追加项目记忆精简版段。"""
    proj = MagicMock()
    proj.memory_summary = "## Hard Constraints\n- wire_api 必须 responses"
    proj.memory_content = "完整记忆不应被使用"
    # 无 pref,无 global mem,有 project(三次查询:Pref / UserMemory / Project)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [None, None, proj]
    result = build_user_agent_memory_section(db, 1, "https://github.com/a/b")
    assert "以下是对该项目的已知问题与历史记忆摘要" in result
    assert "## Hard Constraints\n- wire_api 必须 responses" in result
    assert "完整记忆不应被使用" not in result  # 用 summary 不用 content


def test_user_agent_section_project_falls_back_to_content():
    """memory_summary 为空 → 回退用 memory_content 截断(兼容旧数据)。"""
    proj = MagicMock()
    proj.memory_summary = ""
    proj.memory_content = "## Known Issues\n- issue X"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [None, None, proj]
    result = build_user_agent_memory_section(db, 1, "https://github.com/a/b")
    assert "## Known Issues\n- issue X" in result


def test_user_agent_section_no_project_when_no_repo_url():
    """repo_url 为 None → 不查 Project,无项目记忆段(向后兼容)。"""
    mem = MagicMock()
    mem.content = "## Tech Stack\n- PG"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [None, mem]
    result = build_user_agent_memory_section(db, 1)  # 不传 repo_url
    assert "以下是跨任务积累的长期记忆" in result
    assert "项目记忆" not in result


def test_user_agent_section_pref_global_and_project_combined():
    """三段共存:用户偏好 + 全局记忆 + 项目记忆精简版。"""
    pref = MagicMock()
    pref.preferences = {"output_language": "中文"}
    pref.custom_prompt = ""
    mem = MagicMock()
    mem.content = "## Tech Stack\n- PG"
    proj = MagicMock()
    proj.memory_summary = "## Hard Constraints\n- rule A"
    proj.memory_content = ""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [pref, mem, proj]
    result = build_user_agent_memory_section(db, 1, "https://github.com/a/b")
    assert "## 用户偏好" in result
    assert "以下是跨任务积累的长期记忆" in result
    assert "以下是对该项目的已知问题与历史记忆摘要" in result
    assert "## Hard Constraints\n- rule A" in result


# ---------- build_global_memory_section ----------

def test_global_section_empty_when_no_user():
    db = _mock_db(first_result=MagicMock())
    assert build_global_memory_section(db, None) == ""


def test_global_section_empty_when_no_memory():
    db = _mock_db(first_result=None)  # UserMemory 查不到
    assert build_global_memory_section(db, 1) == ""


def test_global_section_empty_when_blank_content():
    mem = MagicMock()
    mem.content = "   \n  "
    db = _mock_db(first_result=mem)
    assert build_global_memory_section(db, 1) == ""


def test_global_section_returns_content_with_header():
    mem = MagicMock()
    mem.content = "## Hard Constraints\n- wire_api 必须 responses\n\n## Lessons Learned\n- codex bridge 必须 SSE"
    db = _mock_db(first_result=mem)
    result = build_global_memory_section(db, 1)
    # 执行侧 header(区别于 user_agent 的"长期记忆"措辞)
    assert result.startswith("以下是跨任务积累的通用经验,按类别组织(执行时遵循):")
    assert "## Hard Constraints\n- wire_api 必须 responses" in result
    assert "## Lessons Learned\n- codex bridge 必须 SSE" in result


def test_global_section_truncates_long_content():
    """超长全局记忆截断到注入上限 + 尾部截断标记(不调 LLM,直接截断)。"""
    from app.services.memory_injection import MAX_GLOBAL_MEM_CHARS

    mem = MagicMock()
    mem.content = "X" * (MAX_GLOBAL_MEM_CHARS + 500)
    db = _mock_db(first_result=mem)
    result = build_global_memory_section(db, 1)
    # header 后接截断内容
    body = result.split("\n", 1)[1]
    # 截断保留头部内容 + 加截断标记,整体比原文短
    assert body.startswith("X" * 100)
    assert "[...已截断...]" in body
    assert len(body) < len(mem.content)
