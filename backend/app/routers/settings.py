"""模型设置路由

阶段 6:用户可在前端配置自己的 LLM / Embedding 模型,配置存于数据库。

端点:
- GET  /settings/catalog        厂商与模型清单(无需登录,前端选厂商用)
- GET  /settings/models         当前用户已保存的配置(鉴权)
- PUT  /settings/models         保存配置(鉴权,api_key 空串=保留)
- POST /settings/llm/test       测试 LLM 连通性(鉴权,用已存配置)
- POST /settings/embedding/test 测试 Embedding 连通性(鉴权,用已存配置)

安全约定见 schemas/settings.py 模块文档。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.llm.client import LLMClient, _load_catalog
from app.llm.embedding import EmbeddingClient
from app.models.user import User
from app.models.user_llm_config import UserLLMConfig
from app.schemas.settings import (
    LLMConfigOut,
    EmbeddingConfigOut,
    SaveModelsRequest,
    TestResponse,
    UserModelsResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/catalog")
def get_catalog() -> dict:
    """厂商与模型清单(前端用于填充厂商下拉、模型下拉)

    返回 models_catalog.json 原文,含 llmProviders 与 embeddingProviders。
    """
    return _load_catalog()


@router.get("/models", response_model=UserModelsResponse)
def get_my_models(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserModelsResponse:
    """获取当前用户已保存的模型配置(不返回 api_key 原文)"""
    cfg = _get_or_none(db, current_user.id)
    if cfg is None:
        return UserModelsResponse(llm=None, embedding=None)

    llm_out = _to_llm_out(cfg.llm_config) if cfg.llm_config else None
    emb_out = _to_embedding_out(cfg.embedding_config) if cfg.embedding_config else None
    return UserModelsResponse(llm=llm_out, embedding=emb_out)


@router.put("/models", response_model=UserModelsResponse)
def save_models(
    req: SaveModelsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserModelsResponse:
    """保存模型配置

    api_key 约定:空串 "" 表示保留已存的 key,非空串表示更新。
    两区(llm / embedding)可独立保存,未传的一侧保持不变。
    """
    cfg = _get_or_none(db, current_user.id)

    if cfg is None:
        # 首次保存:api_key 必填
        if req.llm is not None and not req.llm.api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="首次保存 LLM 配置需提供 api_key",
            )
        if req.embedding is not None and not req.embedding.api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="首次保存 Embedding 配置需提供 api_key",
            )
        cfg = UserLLMConfig(user_id=current_user.id)
        db.add(cfg)
    else:
        # 更新:api_key 空串 = 保留已存
        if req.llm is not None and not req.llm.api_key:
            existing_api_key = (
                cfg.llm_config.get("api_key", "") if cfg.llm_config else ""
            )
            if not existing_api_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="无已存的 LLM api_key 可保留,请填写 api_key",
                )
        if req.embedding is not None and not req.embedding.api_key:
            existing_api_key = (
                cfg.embedding_config.get("api_key", "") if cfg.embedding_config else ""
            )
            if not existing_api_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="无已存的 Embedding api_key 可保留,请填写 api_key",
                )

    if req.llm is not None:
        cfg.llm_config = _merge_llm_config(cfg.llm_config, req.llm)
    if req.embedding is not None:
        cfg.embedding_config = _merge_embedding_config(cfg.embedding_config, req.embedding)

    db.commit()
    db.refresh(cfg)

    llm_out = _to_llm_out(cfg.llm_config) if cfg.llm_config else None
    emb_out = _to_embedding_out(cfg.embedding_config) if cfg.embedding_config else None
    return UserModelsResponse(llm=llm_out, embedding=emb_out)


@router.post("/llm/test", response_model=TestResponse)
def test_llm(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TestResponse:
    """测试 LLM 连通性(使用已保存的配置)"""
    cfg = _get_or_none(db, current_user.id)
    if cfg is None or not cfg.llm_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="尚未保存 LLM 配置,请先保存",
        )
    try:
        client = LLMClient.from_user_config(cfg)
    except Exception as e:
        return TestResponse(success=False, message=f"客户端构造失败: {e}")

    result = client.test()
    return TestResponse(
        success=result["success"],
        message=result["message"],
        latency_ms=result.get("latency_ms"),
    )


@router.post("/embedding/test", response_model=TestResponse)
def test_embedding(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TestResponse:
    """测试 Embedding 连通性(使用已保存的配置)"""
    cfg = _get_or_none(db, current_user.id)
    if cfg is None or not cfg.embedding_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="尚未保存 Embedding 配置,请先保存",
        )

    emb_cfg = cfg.embedding_config
    try:
        client = EmbeddingClient(
            provider_id=emb_cfg.get("provider", ""),
            api_key=emb_cfg.get("api_key", ""),
            model=emb_cfg.get("model", ""),
            base_url=emb_cfg.get("base_url"),
        )
    except Exception as e:
        return TestResponse(success=False, message=f"客户端构造失败: {e}")

    result = client.test()
    return TestResponse(
        success=result["success"],
        message=result["message"],
        latency_ms=result.get("latency_ms"),
        dimension=result.get("dimension"),
    )


# ============================================================
# 辅助函数
# ============================================================


def _get_or_none(db: Session, user_id) -> UserLLMConfig | None:
    """按 user_id 查 UserLLMConfig(可能不存在)"""
    return db.query(UserLLMConfig).filter(UserLLMConfig.user_id == user_id).first()


def _merge_llm_config(existing: dict | None, new) -> dict:
    """合并 LLM 配置

    api_key 为空串时保留已存的 key(更新语义);非空时用新值。
    """
    existing = existing or {}
    api_key = new.api_key if new.api_key else existing.get("api_key", "")
    return {
        "provider": new.provider,
        "api_key": api_key,
        "model": new.model,
        "enable_thinking": new.enable_thinking,
        "base_url": new.base_url,
    }


def _merge_embedding_config(existing: dict | None, new) -> dict:
    """合并 Embedding 配置(api_key 约定同 _merge_llm_config)"""
    existing = existing or {}
    api_key = new.api_key if new.api_key else existing.get("api_key", "")
    return {
        "provider": new.provider,
        "api_key": api_key,
        "model": new.model,
        "base_url": new.base_url,
        "dimension": new.dimension,
    }


def _to_llm_out(cfg: dict) -> LLMConfigOut:
    """dict → LLMConfigOut(剔除 api_key,转 has_api_key)"""
    return LLMConfigOut(
        provider=cfg.get("provider"),
        model=cfg.get("model"),
        enable_thinking=cfg.get("enable_thinking", True),
        base_url=cfg.get("base_url"),
        has_api_key=bool(cfg.get("api_key")),
    )


def _to_embedding_out(cfg: dict) -> EmbeddingConfigOut:
    """dict → EmbeddingConfigOut(剔除 api_key,转 has_api_key)"""
    return EmbeddingConfigOut(
        provider=cfg.get("provider"),
        model=cfg.get("model"),
        base_url=cfg.get("base_url"),
        dimension=cfg.get("dimension", 1024),
        has_api_key=bool(cfg.get("api_key")),
    )
