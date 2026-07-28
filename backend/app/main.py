"""FastAPI 应用入口"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.database import Base, engine
from app.routers import health, tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期:启动时建表

    阶段 0 用 create_all 快速建表
    后续会切换到 Alembic 迁移管理 schema 变更
    """
    # 导入所有模型,确保 Base.metadata 里都有
    from app.models import task, user  # noqa: F401

    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="AgentPair",
    description="双智能体代码安全审计系统",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(tasks.router)


@app.get("/")
def root() -> dict:
    return {"name": "AgentPair", "version": "0.1.0", "docs": "/docs"}
