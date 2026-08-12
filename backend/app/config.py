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
    # 日志级别(DEBUG/INFO/WARNING/ERROR),留空则按 APP_DEBUG 决定(DEBUG 时 DEBUG,否则 INFO)
    LOG_LEVEL: str = ""
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

    # Gitee OAuth(留空则 /auth/oauth/gitee 与 /git/gitee/* 报错)
    # 在 https://gitee.com/oauth/applications 创建应用获取,回调地址用 /auth/gitee/callback
    GITEE_OAUTH_CLIENT_ID: str = ""
    GITEE_OAUTH_CLIENT_SECRET: str = ""
    GITEE_OAUTH_REDIRECT_URI: str = "http://localhost:5173/auth/gitee/callback"

    # LLM(阶段 1:开发期单 provider 配置)
    LLM_PROVIDER: str = "dashscope"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen3.6-flash"
    LLM_ENABLE_THINKING: bool = True

    # 仓库克隆临时目录
    REPO_CLONE_DIR: str = "./_repos"
    # 仓库克隆深度:0=完整克隆(默认,保留 git 历史供 agent 追溯);>0=浅克隆 --depth N(超大仓库可设 1/50 加速)
    REPO_CLONE_DEPTH: int = 0
    # 仓库克隆超时(秒)。完整克隆比浅克隆慢,默认 600s;超大仓库可调大
    REPO_CLONE_TIMEOUT: int = 600

    # 用户上传 skill 存储目录(默认相对后端运行目录)
    # 生产环境可指向独立可写 volume(如 /data/agentpair/user_skills);
    # 内置 skill 始终在代码目录 backend/skills/,不经过此配置
    USER_SKILLS_DIR: str = "./user_skills"

    # 用户 skill 上传限制(单位 MB / 条,安全边界,详见 app/skills/uploader.py)
    # zip 本体上限(默认 50MB)
    SKILL_MAX_ZIP_SIZE_MB: int = 50
    # 解压后总大小上限(默认 200MB,为 50MB zip 预留约 4 倍解压空间,文本类内容压缩率高)
    SKILL_MAX_EXTRACT_SIZE_MB: int = 200
    # zip 内单文件上限(默认 20MB)
    SKILL_MAX_SINGLE_FILE_SIZE_MB: int = 20
    # 条目数上限(含附加资源,默认 100)
    SKILL_MAX_FILES: int = 100
    # 管理界面单文件内容读取预览上限(默认 20MB,与单文件上传上限对齐)
    SKILL_MAX_READ_SIZE_MB: int = 20
    # 管理界面文件列表条目数上限(防御异常目录,默认 200)
    SKILL_MAX_LISTED_FILES: int = 200
    # 附加资源额外允许的扩展名(逗号分隔,追加到内置白名单之上)
    # 内置白名单见 app/skills/uploader.py 的 _BASE_ALLOWED_EXTENSIONS;
    # 默认放行常见位图(png/jpg/jpeg/webp/gif,无可执行风险);
    # 注意:不建议追加 .svg(可内嵌脚本,有 XSS 风险)
    SKILL_ALLOWED_EXTENSIONS_EXTRA: str = ".png,.jpg,.jpeg,.webp,.gif"

    # 沙箱配置(阶段 2 起)
    # mode: local(本地模式,不用沙箱,在宿主机文件系统直接执行)/ sandbox(连真实 OpenSandbox Server)
    SANDBOX_MODE: str = "local"
    # OpenSandbox Server 地址,形如 http://your-server-ip:8080
    SANDBOX_SERVER_URL: str = "http://localhost:8080"
    # Server 鉴权 API Key(对应 server 配置 [server].api_key,留空则不鉴权)
    SANDBOX_API_KEY: str = ""
    # 沙箱镜像:必须预装 git / ripgrep(rg) / python3 / awk / coreutils
    # 官方 ubuntu 镜像不含 git 和 rg,需按 docs/opensandbox-deploy.md 构建自定义镜像
    SANDBOX_IMAGE: str = "ubuntu"
    # 沙箱超时(分钟)
    SANDBOX_TIMEOUT_MINUTES: int = 30
    # 是否走 Server 代理访问沙箱(跨机部署必须开;本机部署开了也能用)
    # True=所有沙箱请求经 Server 8080 端口转发,后端只需连 Server 一个端口
    # False=SDK 直连沙箱容器端口(需后端能访问 Server 的容器端口范围)
    SANDBOX_USE_SERVER_PROXY: bool = True
    # Server 宿主机上的 SSH key 目录(可选,只读挂载到沙箱 /home/user/.ssh 供 git clone SSH 协议用)
    # 这是 Server 机器上的路径,不是后端本地路径!必须用绝对路径(如 /home/admin/.ssh),不要用 ~
    # 留空不挂载;需在 server [storage].allowed_host_paths 放行该路径前缀
    SANDBOX_SSH_KEY_HOST_PATH: str = ""
    # 沙箱资源限制(可选,传给 SDK resource 参数,如 cpu="2" memory="4Gi")
    SANDBOX_CPU: str = ""
    SANDBOX_MEMORY: str = ""

    # ---- local 模式安全策略(路径权限 + 命令白名单) ----
    # local 模式下 .git 目录写保护(防 LLM 篡改 git 历史),对齐 TRAE 沙箱路径策略
    SANDBOX_LOCAL_PROTECT_GIT: bool = True
    # local 模式下额外的只读路径(逗号分隔,写操作拒绝,读操作允许)
    # 默认保护 .vscode / .trae / .idea 等编辑器配置目录
    SANDBOX_LOCAL_READONLY_PATHS: str = ".vscode,.trae,.idea"
    # local 模式下命令安全策略:
    #   safe: 安全命令前缀列表(逗号分隔,直接执行不拦截)
    #   dangerous: 危险命令正则列表(逗号分隔,匹配时推前端确认)
    #   其他命令: 执行但记录 INFO 日志
    SANDBOX_LOCAL_SAFE_COMMANDS: str = (
        "git status,git diff,git log,git show,git branch,git remote,"
        "ls,cat,head,tail,wc,grep,find,rg,fd,"
        "python,python3,pip,pip3,node,npm,npx,"
        "echo,printf,test,"
        "mkdir -p,touch,cp -r,mv"
    )
    # fork bomb 正则等,逗号在引号内作为分隔符
    SANDBOX_LOCAL_DANGEROUS_COMMANDS: str = (
        r"rm\s+-rf\s+/,"
        r"rm\s+-rf\s+~/,"
        r"rm\s+-rf\s+\*,"
        r"mkfs,dd\s+if=,"
        r":\(\)\{\s*:\|:\s*&\s*\};:,"
        r"curl\s+.*\|\s*(ba)?sh,"
        r"wget\s+.*\|\s*(ba)?sh,"
        r"chmod\s+777\s+/,"
        r"netcat|nc\s+-l,"
        r"sudo\s+,"
        r"shutdown|reboot|halt|poweroff"
    )
    # local 模式下是否启用平台原生隔离(macOS: sandbox-exec / Linux: bwrap)
    # True=检测到工具时自动包装命令(只读系统目录 + 读写工作区 + 禁外网)
    # False=不做原生隔离,仅靠路径策略 + 命令白名单(软隔离)
    # Windows 无原生沙箱,此配置项无效
    SANDBOX_LOCAL_NATIVE_ISOLATION: bool = True

    # Qoder CLI 配置(qoder_cli executor 用,国际版)
    # qodercli 可执行文件名/路径(沙箱内 PATH 查找或绝对路径)
    QODER_CLI_BIN: str = "qodercli"
    # qodercli 安装命令(沙箱内未检测到 qodercli 时执行,留空则不自动安装)
    QODER_CLI_INSTALL_CMD: str = "npm install -g @qoder-ai/qodercli"

    # Qoder CN CLI 配置(qoder_cli_cn executor 用,国内版/原通义灵码)
    # qoderclicn 可执行文件名/路径
    QODER_CLI_CN_BIN: str = "qoderclicn"
    # qoderclicn 安装命令(官方安装脚本)
    QODER_CLI_CN_INSTALL_CMD: str = "curl -fsSL https://qoder.cn/install | bash"

    # Kimi Code CLI 配置(kimi_cli executor 用,开源 https://github.com/MoonshotAI/kimi-code)
    # kimi 可执行文件名/路径(沙箱内 PATH 查找或绝对路径)
    KIMI_CLI_BIN: str = "kimi"
    # kimi 安装命令(沙箱内未检测到 kimi 时执行,需沙箱镜像有 Node.js >= 20)
    # 推荐在镜像中预装 kimi,避免每次任务都拉 npm 包
    KIMI_CLI_INSTALL_CMD: str = "npm install -g @moonshot-ai/kimi-code"

    # Hermes CLI 配置(hermes_cli executor 用,开源 https://github.com/NousResearch/hermes-agent)
    # hermes 可执行文件名/路径(沙箱内 PATH 查找或绝对路径)
    HERMES_CLI_BIN: str = "hermes"
    # hermes 安装命令(沙箱内未检测到 hermes 时执行)。
    # 注意:hermes-agent 未发布到 PyPI,`pip install hermes-agent` 会失败;
    # 用官方 install.sh(同 README):自动装 uv + Python 3.11 + 源码 + 依赖,符号链接 hermes 命令。
    # 非 root 运行时装到 ~/.hermes/hermes-agent + ~/.local/bin/hermes(root 装 FHS: /usr/local/bin/hermes)。
    # 强烈推荐用 build-sandbox-image.sh 在镜像中预装(root FHS → /usr/local/bin,PATH 必达):
    # 运行时源码安装较慢(uv sync 含较多依赖,1-5 分钟),120s 安装超时 / BRIDGE_STARTUP_TIMEOUT 可能不够。
    HERMES_CLI_INSTALL_CMD: str = (
        'curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh '
        '&& bash /tmp/hermes-install.sh --skip-setup --skip-browser --non-interactive '
        '&& rm -f /tmp/hermes-install.sh '
        # install.sh 不装 [anthropic] extra;anthropic_messages 模式的 provider
        # (anthropic/minimax)需要 anthropic 包,这里一并补装
        # uv venv 默认不含 pip,先 ensurepip 引导
        '&& /usr/local/lib/hermes-agent/venv/bin/python -m ensurepip '
        '&& /usr/local/lib/hermes-agent/venv/bin/python -m pip install "anthropic==0.87.0"'
    )

    # Codex CLI 配置(codex_cli executor 用,开源 https://github.com/openai/codex,Apache-2.0)
    # codex 可执行文件名/路径(沙箱内 PATH 查找或绝对路径)
    CODEX_CLI_BIN: str = "codex"
    # codex 安装命令(沙箱内未检测到 codex 时执行,需沙箱镜像有 Node.js >= 16)
    CODEX_CLI_INSTALL_CMD: str = "npm install -g @openai/codex"


settings = Settings()
