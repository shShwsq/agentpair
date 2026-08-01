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
    # 显式开启才会 drop_all + create_all 重建表,避免每次启动丢数据
    DB_REBUILD_ON_START: bool = False

    # 应用
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    # 邮件链接的基础 URL(开发期指向前端 dev server 或后端)
    APP_BASE_URL: str = "http://localhost:5173"

    # JWT(阶段 6 用)
    JWT_SECRET: str = "change_me_in_production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # GitHub access_token 加密密钥(Fernet,32 字节 base64)
    # 留空则启动时自动生成(开发期方便,生产必须固定)
    GITHUB_TOKEN_SECRET: str = ""

    # GitHub OAuth(阶段 6 用,留空则 /auth/oauth/github 报错)
    GITHUB_OAUTH_CLIENT_ID: str = ""
    GITHUB_OAUTH_CLIENT_SECRET: str = ""
    GITHUB_OAUTH_REDIRECT_URI: str = "http://localhost:5173/auth/github/callback"

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
