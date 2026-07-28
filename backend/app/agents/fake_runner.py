"""假的 Agent Runner

阶段 0 占位实现,返回硬编码结果,用于跑通 API 链路
阶段 1 起会替换为真实 react_agent
"""
from sqlalchemy.orm import Session

from app.models.task import Conversation, Finding, Task, TaskStatus


def run_fake_audit(task: Task, db: Session) -> None:
    """跑一个假的安全审计,直接写入硬编码的发现与对话

    阶段 0 用,模拟 react_agent 跑完后的结果
    """
    # 1. 模拟一段对话记录
    conversations = [
        Conversation(
            task_id=task.id,
            role="user",
            type="question",
            content=f"请审计这个仓库: {task.repo_url}",
        ),
        Conversation(
            task_id=task.id,
            role="react_agent",
            type="finding",
            content="在 db.py 第 42 行发现 SQL 注入漏洞",
        ),
        Conversation(
            task_id=task.id,
            role="react_agent",
            type="finding",
            content="在 config.py 第 15 行发现硬编码 API key",
        ),
    ]
    db.add_all(conversations)

    # 2. 模拟发现两个漏洞
    findings = [
        Finding(
            task_id=task.id,
            category="CWE-89",  # SQL Injection
            severity="high",
            file_path="db.py",
            line_range="42-45",
            description="使用字符串拼接构造 SQL 查询,用户输入未参数化",
            remediation="改用参数化查询或 ORM 的占位符机制",
            verified="unverified",
        ),
        Finding(
            task_id=task.id,
            category="CWE-798",  # Hardcoded Credentials
            severity="high",
            file_path="config.py",
            line_range="15-15",
            description="代码中硬编码了 API key",
            remediation="将密钥移到环境变量或密钥管理服务",
            verified="unverified",
        ),
    ]
    db.add_all(findings)

    # 3. 更新任务状态
    task.status = TaskStatus.COMPLETED
    task.current_stage = "审计完成(假数据)"
    from datetime import datetime, timezone

    task.completed_at = datetime.now(timezone.utc)

    db.commit()
