"""FastAPI 应用入口"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.database import Base, engine
from app.routers import health, tasks

# 导入场景模块,触发注册
from app.scenarios import security_audit  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期:启动时建表

    开发期用 drop_all + create_all 快速重建(数据会丢)
    生产环境应切换到 Alembic 迁移管理 schema 变更
    """
    from app.models import task, user  # noqa: F401

    # 开发期:重建表(字段变更时需要)
    if settings.DEBUG:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="AgentPair",
    description="双智能体协作系统",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(tasks.router)


@app.get("/")
def root() -> dict:
    return {"name": "AgentPair", "version": "0.2.0", "docs": "/docs"}
