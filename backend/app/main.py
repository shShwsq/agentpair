"""FastAPI 应用入口"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.database import Base, engine
from app.routers import agent_configs, auth, health, skills, tasks
from app.routers import git_provider as git_provider_router
from app.routers import model_configs as model_configs_router
from app.routers import workspace as workspace_router
from app.routers import memory as memory_router

# 日志配置:开发期 DEBUG,生产期 INFO
# 通过 LOG_LEVEL 环境变量覆盖(默认按 APP_ENV 决定)
_log_level = getattr(settings, "LOG_LEVEL", None) or (
    "DEBUG" if settings.APP_DEBUG else "INFO"
)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# 导入场景模块,触发注册(general 放首位 → 前端新建任务默认选中"通用")
from app.scenarios import general  # noqa: F401
from app.scenarios import code_review  # noqa: F401
from app.scenarios import security_audit  # noqa: F401

# 阶段 5:启动时扫描所有 SKILL.md,加载到进程级注册表
from app.skills.loader import reload_registry
reload_registry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期:启动时建表

    默认仅 create_all(幂等,不会重建已存在的表)。
    需要 schema 变更时,在 .env 设置 DB_REBUILD_ON_START=true 触发 drop_all,
    生产环境应切换到 Alembic 迁移管理 schema 变更。
    """
    from app.models import email_token, task, user  # noqa: F401
    from app.models import task_artifact  # noqa: F401
    from app.models import user_agent_config  # noqa: F401
    from app.models import user_git_binding  # noqa: F401
    from app.models import user_llm_config  # noqa: F401
    from app.models import project  # noqa: F401

    if settings.DB_REBUILD_ON_START:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # 一次性迁移:把旧 users.github_id/github_access_token 搬到 user_git_bindings
    from app.models.user_git_binding import (
        add_login_avatar_columns,
        add_refresh_token_columns,
        migrate_legacy_github_bindings,
    )

    migrate_legacy_github_bindings()
    # 加 refresh_token / expires_at 列(Gitee access_token 刷新机制)
    add_refresh_token_columns()
    # 加 provider_login / avatar_url 列(status 接口缓存 login+avatar,避免每次调 /user)
    add_login_avatar_columns()
    # 加 projects.memory_summary 列(精简版记忆,注入 system prompt 用)
    from app.models.project import migrate_project_memory_summary

    migrate_project_memory_summary()
    # 迁移 user_preferences:删遗留 preferences 列,custom_prompt 改名 user_profile
    from app.models.user_preference import migrate_user_preference_columns

    migrate_user_preference_columns()
    # 加 conversations.tool_call_id 列(tool_result 关联对应 tool_call,并行调用时前端精确配对)
    from app.models.task import migrate_conversation_tool_call_id

    migrate_conversation_tool_call_id()
    yield


app = FastAPI(
    title="AgentPair",
    description="双智能体协作系统",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(tasks.router)
app.include_router(skills.router)
app.include_router(auth.router)
app.include_router(model_configs_router.router)
app.include_router(git_provider_router.router)
app.include_router(workspace_router.router)
app.include_router(agent_configs.router)
app.include_router(memory_router.router)


@app.get("/")
def root() -> dict:
    return {"name": "AgentPair", "version": "0.2.0", "docs": "/docs"}
