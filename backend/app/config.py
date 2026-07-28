"""应用配置加载"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置,从 .env 读取"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 数据库
    DATABASE_URL: str = "postgresql+psycopg://localhost/agentpair"

    # 应用
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # JWT(阶段 6 用)
    JWT_SECRET: str = "change_me_in_production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # LLM(阶段 1:开发期单 provider 配置)
    LLM_PROVIDER: str = "dashscope"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen3.6-flash"
    LLM_ENABLE_THINKING: bool = True

    # 仓库克隆临时目录
    REPO_CLONE_DIR: str = "./_repos"

    # 沙箱配置(阶段 2 起)
    # mode: mock(本地未部署 Server)/ sandbox(连真实 OpenSandbox Server)
    SANDBOX_MODE: str = "mock"
    SANDBOX_SERVER_URL: str = "http://localhost:8080"
    SANDBOX_API_KEY: str = ""
    SANDBOX_IMAGE: str = "opensandbox/code-interpreter:v1.0.2"
    SANDBOX_TIMEOUT_MINUTES: int = 30


settings = Settings()
