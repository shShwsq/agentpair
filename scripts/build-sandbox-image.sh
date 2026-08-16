#!/usr/bin/env bash
# 构建 AgentPair 沙箱镜像(预装 git / ripgrep / python3 / awk / find,Semgrep 默认预装可 --no-semgrep 省略)
#
# 默认同时预装五款 CLI 执行器:
#   - Qoder CLI(国际版):Node.js + @qoder-ai/qodercli,需 qoder.com 账号
#   - Qoder CN CLI(国内版,原通义灵码):零依赖二进制,仅需 curl,需 qoder.cn 账号
#   - Kimi Code CLI(开源):Node.js + @moonshot-ai/kimi-code,需 LLM API Key
#   - Hermes CLI(开源):Python 包,官方 install.sh 安装(需 Python >=3.11),需 LLM API Key(支持多供应商)
#   - Codex CLI(OpenAI 官方,开源):Node.js + @openai/codex,需 LLM API Key(支持自定义端点)
#
# 支持 task.executor=qoder_cli / qoder_cli_cn / kimi_cli / hermes_cli / codex_cli。
#
# Node 版本策略:qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,codex 要求 >= 16。
# 只要任一 Node 类 CLI 启用(qoder_cli / kimi_cli / codex_cli),统一装 Node 22.x(三者都兼容)。
# Hermes CLI 是 Python 包(未发布 PyPI,用官方 install.sh 装 uv+Python 3.11+源码),不需要 Node.js。
#
# 用法:在装好 docker 的 Linux 服务器上执行
#   bash scripts/build-sandbox-image.sh                                       # 默认装五款 CLI
#   bash scripts/build-sandbox-image.sh --no-qoder-cli                        # 不装国际版
#   bash scripts/build-sandbox-image.sh --no-qoder-cli-cn                     # 不装国内版
#   bash scripts/build-sandbox-image.sh --no-kimi-cli                         # 不装 kimi
#   bash scripts/build-sandbox-image.sh --no-hermes-cli                       # 不装 hermes
#   bash scripts/build-sandbox-image.sh --no-codex-cli                        # 不装 codex
#   bash scripts/build-sandbox-image.sh --no-qoder-cli --no-kimi-cli          # 仅国内版 + hermes + codex
#   bash scripts/build-sandbox-image.sh --no-qoder-cli --no-qoder-cli-cn --no-kimi-cli --no-codex-cli  # 仅 hermes
#   bash scripts/build-sandbox-image.sh --no-semgrep                          # 不预装 Semgrep
#
# 国内镜像加速(服务器在国内时推荐,避免 docker.io 拉取超时):
#   bash scripts/build-sandbox-image.sh --cn-mirror                           # 一键国内源(Docker+apt+npm+PyPI)
#   bash scripts/build-sandbox-image.sh --registry docker.m.daocloud.io       # 仅换 Docker 基础镜像源
#
# 构建 agentpair-sandbox:latest 后,在 AgentPair backend/.env 设:
#   SANDBOX_IMAGE=agentpair-sandbox:latest

set -euo pipefail

IMAGE_NAME="agentpair-sandbox"
IMAGE_TAG="latest"
DOCKERFILE="Dockerfile.sandbox"

# ---------- 参数解析 ----------
# 五款 CLI 独立开关,默认都装
# - 国际版 qodercli:需 Node.js + npm(镜像体积较大,约 +200MB)
# - 国内版 qoderclicn:零依赖二进制(仅需 curl,体积忽略不计)
# - Kimi Code CLI:需 Node.js + npm(与国际版共享 Node 22.x 运行时)
# - Hermes CLI:Python 包(未发布 PyPI,官方 install.sh 自动装 uv+Python 3.11+源码;体积增量取决于依赖)
# - Codex CLI:Node.js + npm(OpenAI 官方,与 Node 类 CLI 共享 Node 22.x 运行时)
WITH_QODER_CLI=1
WITH_QODER_CLI_CN=1
WITH_KIMI_CLI=1
WITH_HERMES_CLI=1
WITH_CODEX_CLI=1
# Semgrep(内置 react_agent 的 run_semgrep 工具依赖),默认也装,可用 --no-semgrep 省略
WITH_SEMGREP=1
# SSH 转发(Hermes install.sh clone 源码用):--ssh 启用 BuildKit SSH agent 挂载,
# 构建时 docker build --ssh default 传入宿主机 SSH key,GitHub SSH clone 直接成功(免 HTTPS fallback)
WITH_SSH=0
# --ssh-key 指定 key 文件路径(如 ~/.ssh/id_ed25519);不指定则走 SSH agent(docker build --ssh default 语义)
SSH_KEY=""
# GitHub 加速镜像前缀(ghproxy 风格,如 https://ghfast.top):把 install.sh 的 github.com HTTPS/SSH clone
# 重写为 <base>/https://github.com/...,并给 uv 的 Python 下载配同源镜像。适合国内服务器直连 GitHub 超时。
# 留空 = 直连 GitHub。仅在 Hermes install.sh 所在 RUN 内临时生效,不写入最终镜像。
GITHUB_MIRROR=""
# Hermes 源码预克隆 URL:非空时先 git clone 到 /usr/local/lib/hermes-agent(与 install.sh 的 FHS root 布局一致),
# 再跑 install.sh —— install.sh 检测到目录已存在且是 git 仓库会跳过 SSH/HTTPS clone,直接装依赖。
# 适合镜像 clone 能通但 install.sh 内部 clone 不稳定的场景。URL 会成为 origin remote(install.sh 的 fetch 也走它),
# 建议直接给镜像 URL(如 https://ghfast.top/https://github.com/NousResearch/hermes-agent.git)。
# 分支必须 main(install.sh 写死 BRANCH=main),否则 checkout 失败。
HERMES_CLONE_URL=""
# Hermes 源码本地目录(--hermes-local-dir):直接用宿主机已 clone 的仓库(如 /usr/local/lib/hermes-agent),
# 构建时 COPY 进容器(含 .git,install.sh 检测到已有 git 仓库会跳过 clone,直接装依赖)。
# 注意:install.sh 的 update 流程会 git fetch origin(走 origin URL),容器内必须可达——
# 建议搭配 --github-mirror(把 SSH/HTTPS clone 都重写到镜像)。与 --hermes-clone-url 互斥。
HERMES_LOCAL_DIR=""
# 镜像源(国内加速),默认空 = 用官方源
# - REGISTRY:Docker 基础镜像源前缀,如 docker.m.daocloud.io(非空时 FROM $REGISTRY/ubuntu:24.04)
# - APT_MIRROR:apt 源,目前支持 aliyun(空 = 不换)
# - NPM_MIRROR:npm 源,目前支持 npmmirror(空 = 不换)
# - PYPI_MIRROR:PyPI 源(uv/pip),目前支持 aliyun(空 = 不换;Hermes install.sh 用 uv 拉 Python 依赖时生效)
REGISTRY=""
APT_MIRROR=""
NPM_MIRROR=""
PYPI_MIRROR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --with-qoder-cli)
            WITH_QODER_CLI=1
            shift
            ;;
        --no-qoder-cli)
            WITH_QODER_CLI=0
            shift
            ;;
        --with-qoder-cli-cn)
            WITH_QODER_CLI_CN=1
            shift
            ;;
        --no-qoder-cli-cn)
            WITH_QODER_CLI_CN=0
            shift
            ;;
        --with-kimi-cli)
            WITH_KIMI_CLI=1
            shift
            ;;
        --no-kimi-cli)
            WITH_KIMI_CLI=0
            shift
            ;;
        --with-hermes-cli)
            WITH_HERMES_CLI=1
            shift
            ;;
        --no-hermes-cli)
            WITH_HERMES_CLI=0
            shift
            ;;
        --with-codex-cli)
            WITH_CODEX_CLI=1
            shift
            ;;
        --no-codex-cli)
            WITH_CODEX_CLI=0
            shift
            ;;
        --with-semgrep)
            WITH_SEMGREP=1
            shift
            ;;
        --no-semgrep)
            WITH_SEMGREP=0
            shift
            ;;
        --ssh)
            WITH_SSH=1
            shift
            ;;
        --ssh-key)
            # 下一个参数为 key 文件路径
            if [ $# -lt 2 ]; then
                echo "[FAIL] --ssh-key 需要一个参数(如 --ssh-key ~/.ssh/id_ed25519)"
                exit 1
            fi
            WITH_SSH=1
            SSH_KEY="$2"
            shift 2
            ;;
        --github-mirror)
            # 下一个参数为镜像前缀(ghproxy 风格,不带尾斜杠)
            if [ $# -lt 2 ]; then
                echo "[FAIL] --github-mirror 需要一个参数(如 --github-mirror https://ghfast.top)"
                exit 1
            fi
            GITHUB_MIRROR="$2"
            shift 2
            ;;
        --hermes-clone-url)
            # 下一个参数为预克隆 URL(建议镜像 URL)
            if [ $# -lt 2 ]; then
                echo "[FAIL] --hermes-clone-url 需要一个参数(如 --hermes-clone-url https://ghfast.top/https://github.com/NousResearch/hermes-agent.git)"
                exit 1
            fi
            HERMES_CLONE_URL="$2"
            shift 2
            ;;
        --hermes-local-dir)
            # 下一个参数为宿主机已 clone 的 Hermes 源码目录
            if [ $# -lt 2 ]; then
                echo "[FAIL] --hermes-local-dir 需要一个参数(如 --hermes-local-dir /usr/local/lib/hermes-agent)"
                exit 1
            fi
            HERMES_LOCAL_DIR="$2"
            shift 2
            ;;
        --registry)
            # 下一个参数为镜像源前缀
            if [ $# -lt 2 ]; then
                echo "[FAIL] --registry 需要一个参数(如 --registry docker.m.daocloud.io)"
                exit 1
            fi
            REGISTRY="$2"
            shift 2
            ;;
        --cn-mirror)
            # 一键国内加速:Docker 用 DaoCloud 镜像 + apt 阿里云 + npm npmmirror + PyPI 阿里云
            REGISTRY="docker.m.daocloud.io"
            APT_MIRROR="aliyun"
            NPM_MIRROR="npmmirror"
            PYPI_MIRROR="aliyun"
            shift
            ;;
        -h|--help)
            echo "用法:bash $0 [选项](均可组合,默认全部启用)"
            echo ""
            echo "CLI 开关(默认全装):"
            echo "  --with-qoder-cli       预装 Qoder CLI 国际版(Node.js + npm,需 qoder.com 账号)"
            echo "  --no-qoder-cli         不装国际版"
            echo "  --with-qoder-cli-cn    预装 Qoder CN CLI 国内版(零依赖二进制,需 qoder.cn 账号)"
            echo "  --no-qoder-cli-cn      不装国内版"
            echo "  --with-kimi-cli        预装 Kimi Code CLI(Node.js + npm,需 LLM API Key)"
            echo "  --no-kimi-cli          不装 kimi"
            echo "  --with-hermes-cli      预装 Hermes CLI(官方 install.sh,需 Python >=3.11,支持多供应商)"
            echo "  --no-hermes-cli        不装 hermes"
            echo "  --with-codex-cli       预装 Codex CLI(Node.js + npm,OpenAI 官方,需 LLM API Key)"
            echo "  --no-codex-cli         不装 codex"
            echo "  --with-semgrep         预装 Semgrep(内置 react_agent 的 run_semgrep 工具用,默认装)"
            echo "  --no-semgrep           不装 semgrep(不预装时 run_semgrep 首次运行会兜底自动安装,但耗时长)"
            echo "  --ssh                  Hermes install.sh clone 源码时启用 SSH 转发(docker build --ssh),用宿主机 SSH key 免 HTTPS fallback"
            echo "  --ssh-key <path>       配合 --ssh 指定 key 文件(如 ~/.ssh/id_ed25519);不指定则走 SSH agent"
            echo "  --github-mirror <base>  GitHub 加速:把 github.com 的 HTTPS/SSH clone 重写到镜像前缀"
            echo "                          (ghproxy 风格:<base>/https://github.com/...,如 https://ghfast.top),"
            echo "                          并给 uv 的 Python 下载配同源镜像;适合国内直连 GitHub 超时"
            echo "                          (用了它就不用 --ssh;仅 Hermes 安装段临时生效,不进最终镜像)"
            echo "  --hermes-clone-url <url> Hermes 源码预克隆:先 git clone 到 /usr/local/lib/hermes-agent 再跑"
            echo "                          install.sh(它检测到已有 git 仓库会跳过 clone 直接装依赖);"
            echo "                          URL 即 origin remote(install.sh 的 fetch 也走它),建议给镜像 URL;"
            echo "                          分支必须 main。与 --github-mirror/--ssh 可叠加"
            echo "  --hermes-local-dir <dir> 直接用宿主机已 clone 的 Hermes 源码目录(如 /usr/local/lib/hermes-agent),"
            echo "                          构建时 COPY 进容器(保留 .git,install.sh 跳过 clone 直接装依赖);"
            echo "                          install.sh 的 fetch 仍走 origin URL,建议搭配 --github-mirror;"
            echo "                          与 --hermes-clone-url 互斥"
            echo ""
            echo "镜像源(服务器在国内时推荐,避免 docker.io 拉取超时):"
            echo "  --cn-mirror            一键国内加速(Docker DaoCloud + apt 阿里云 + npm npmmirror + PyPI 阿里云)"
            echo "  --registry <prefix>    仅换 Docker 基础镜像源前缀(如 docker.m.daocloud.io)"
            echo "                         非空时 FROM <prefix>/ubuntu:24.04;阿里云需带 library/ 前缀"
            echo ""
            echo "基础工具(git/rg/python3/awk/find/curl)始终预装。"
            echo "Node.js 版本:只要 qoder_cli / kimi_cli / codex_cli 任一启用,统一装 Node 22.x(三者都兼容)。"
            echo "Hermes CLI 是 Python 包(未发布 PyPI,官方 install.sh 装 uv+Python 3.11+源码),不需要 Node.js。"
            exit 0
            ;;
        *)
            echo "[FAIL] 未知参数: $1(用 -h 查看帮助)"
            exit 1
            ;;
    esac
done

# ---------- Hermes 源码来源校验 ----------
# --hermes-local-dir 与 --hermes-clone-url 互斥(一个用本地目录,一个容器内 clone)
if [ -n "$HERMES_LOCAL_DIR" ] && [ -n "$HERMES_CLONE_URL" ]; then
    echo "[FAIL] --hermes-local-dir 与 --hermes-clone-url 互斥,只能选一个"
    exit 1
fi
if [ -n "$HERMES_LOCAL_DIR" ]; then
    if [ ! -d "$HERMES_LOCAL_DIR/.git" ]; then
        echo "[FAIL] $HERMES_LOCAL_DIR 不是 git 仓库(缺 .git),install.sh 需要它识别已有安装"
        exit 1
    fi
    # 分支提示(install.sh 写死 BRANCH=main;fetch 成功后 checkout 也能自动建 main,这里仅提醒)
    CUR_BRANCH=$(git -C "$HERMES_LOCAL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached")
    if [ "$CUR_BRANCH" != "main" ]; then
        echo "[WARN] $HERMES_LOCAL_DIR 当前分支是 $CUR_BRANCH(install.sh 写死 BRANCH=main)"
        echo "       建议先执行:git -C $HERMES_LOCAL_DIR checkout main"
    fi
fi

# ---------- 前置检查 ----------
if ! command -v docker >/dev/null 2>&1; then
    echo "[FAIL] docker 未安装或未运行,请先安装 Docker Engine 20.10+"
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "[FAIL] docker daemon 不可用(可能当前用户不在 docker 组,或 docker 未启动)"
    echo "       解决:sudo usermod -aG docker \$USER 然后重登,或 sudo systemctl start docker"
    exit 1
fi

# ---------- 是否需要装 Node.js ----------
# qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,codex 要求 >= 16,统一用 Node 22.x(三者都兼容)
NEED_NODE=0
if [ "$WITH_QODER_CLI" -eq 1 ] || [ "$WITH_KIMI_CLI" -eq 1 ] || [ "$WITH_CODEX_CLI" -eq 1 ]; then
    NEED_NODE=1
fi

# ---------- 基础镜像源 ----------
# REGISTRY 非空时用 $REGISTRY/ubuntu:24.04,否则用官方 ubuntu:24.04
if [ -n "$REGISTRY" ]; then
    BASE_IMAGE="$REGISTRY/ubuntu:24.04"
else
    BASE_IMAGE="ubuntu:24.04"
fi
# registry marker(检测配置变更用),空 = default
expect_registry_marker="${REGISTRY:-default}"
# pypi mirror marker(检测配置变更用),空 = default
expect_pypi_mirror_marker="${PYPI_MIRROR:-default}"

# ---------- 生成 Dockerfile(若不存在或配置不符) ----------
# 检测:Dockerfile 现状与本次期望的 CLI 组合是否一致,不一致则备份重生成。
# 不用 sed 追加(反引号/转义/位置错乱等问题太多),直接按期望状态覆盖最可靠。
#
# 期望标记(在 Dockerfile 中以注释形式存在,便于检测):
#   # @qoder-cli:yes / # @qoder-cli:no        国际版状态
#   # @qoder-cli-cn:yes / # @qoder-cli-cn:no  国内版状态
#   # @kimi-cli:yes / # @kimi-cli:no          kimi 状态
#   # @hermes-cli:yes / # @hermes-cli:no      hermes 状态
#   # @codex-cli:yes / # @codex-cli:no        codex 状态
#   # @semgrep:yes / # @semgrep:no            semgrep 状态
NEED_REGEN=0
REGEN_REASON=""
expect_qoder_cli_marker=$([ "$WITH_QODER_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_qoder_cli_cn_marker=$([ "$WITH_QODER_CLI_CN" -eq 1 ] && echo "yes" || echo "no")
expect_kimi_cli_marker=$([ "$WITH_KIMI_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_hermes_cli_marker=$([ "$WITH_HERMES_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_codex_cli_marker=$([ "$WITH_CODEX_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_semgrep_marker=$([ "$WITH_SEMGREP" -eq 1 ] && echo "yes" || echo "no")
expect_ssh_marker=$([ "$WITH_SSH" -eq 1 ] && echo "yes" || echo "no")
expect_github_mirror_marker="${GITHUB_MIRROR:-default}"
expect_hermes_clone_url_marker="${HERMES_CLONE_URL:-default}"
expect_hermes_local_marker=$([ -n "$HERMES_LOCAL_DIR" ] && echo "yes" || echo "no")

if [ ! -f "$DOCKERFILE" ]; then
    NEED_REGEN=1
    REGEN_REASON="文件不存在,全新生成"
else
    cur_qoder_cli=$(grep -E "^# @qoder-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_qoder_cli_cn=$(grep -E "^# @qoder-cli-cn:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_kimi_cli=$(grep -E "^# @kimi-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_hermes_cli=$(grep -E "^# @hermes-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_codex_cli=$(grep -E "^# @codex-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_semgrep=$(grep -E "^# @semgrep:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_ssh=$(grep -E "^# @ssh:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_github_mirror=$(grep -E "^# @github-mirror:" "$DOCKERFILE" | head -1 | sed 's/^# @github-mirror://' || echo "")
    cur_hermes_clone_url=$(grep -E "^# @hermes-clone-url:" "$DOCKERFILE" | head -1 | sed 's/^# @hermes-clone-url://' || echo "")
    cur_hermes_local=$(grep -E "^# @hermes-local:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_registry=$(grep -E "^# @registry:" "$DOCKERFILE" | head -1 | sed 's/^# @registry://' || echo "")
    cur_pypi_mirror=$(grep -E "^# @pypi-mirror:" "$DOCKERFILE" | head -1 | sed 's/^# @pypi-mirror://' || echo "")
    if [ "$cur_registry" != "$expect_registry_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="镜像源变更($cur_registry → $expect_registry_marker)"
    elif [ "$cur_pypi_mirror" != "$expect_pypi_mirror_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="PyPI 镜像源变更($cur_pypi_mirror → $expect_pypi_mirror_marker)"
    elif [ "$cur_qoder_cli" != "$expect_qoder_cli_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="国际版配置变更($cur_qoder_cli → $expect_qoder_cli_marker)"
    elif [ "$cur_qoder_cli_cn" != "$expect_qoder_cli_cn_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="国内版配置变更($cur_qoder_cli_cn → $expect_qoder_cli_cn_marker)"
    elif [ "$cur_kimi_cli" != "$expect_kimi_cli_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="Kimi 配置变更($cur_kimi_cli → $expect_kimi_cli_marker)"
    elif [ "$cur_hermes_cli" != "$expect_hermes_cli_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="Hermes 配置变更($cur_hermes_cli → $expect_hermes_cli_marker)"
    elif [ "$cur_codex_cli" != "$expect_codex_cli_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="Codex 配置变更($cur_codex_cli → $expect_codex_cli_marker)"
    elif [ "$cur_semgrep" != "$expect_semgrep_marker" ]; then
        # 旧版 Dockerfile 无 @semgrep marker(cur_semgrep 为空)也会命中这里,自动重生成补上/去掉 semgrep
        NEED_REGEN=1
        REGEN_REASON="Semgrep 配置变更(${cur_semgrep:-无标记} → $expect_semgrep_marker)"
    elif [ "$cur_ssh" != "$expect_ssh_marker" ]; then
        # 旧版 Dockerfile 无 @ssh marker(cur_ssh 为空)也会命中这里,自动重生成补上/去掉 SSH 挂载
        NEED_REGEN=1
        REGEN_REASON="SSH 配置变更(${cur_ssh:-无标记} → $expect_ssh_marker)"
    elif [ "$cur_github_mirror" != "$expect_github_mirror_marker" ]; then
        # 旧版 Dockerfile 无 @github-mirror marker(cur_github_mirror 为空)也会命中这里,自动重生成
        NEED_REGEN=1
        REGEN_REASON="GitHub 镜像配置变更(${cur_github_mirror:-无标记} → $expect_github_mirror_marker)"
    elif [ "$cur_hermes_clone_url" != "$expect_hermes_clone_url_marker" ]; then
        # 旧版 Dockerfile 无 @hermes-clone-url marker(cur_hermes_clone_url 为空)也会命中这里,自动重生成
        NEED_REGEN=1
        REGEN_REASON="Hermes 预克隆配置变更(${cur_hermes_clone_url:-无标记} → $expect_hermes_clone_url_marker)"
    elif [ "$cur_hermes_local" != "$expect_hermes_local_marker" ]; then
        # 旧版 Dockerfile 无 @hermes-local marker(cur_hermes_local 为空)也会命中这里,自动重生成
        NEED_REGEN=1
        REGEN_REASON="Hermes 本地目录配置变更(${cur_hermes_local:-无标记} → $expect_hermes_local_marker)"
    elif grep -q '\\n' "$DOCKERFILE" 2>/dev/null; then
        # 旧版生成的 RUN 续行用 printf '\\n' 输出了字面 \n(sh 报 bad variable name),兜底重生成
        NEED_REGEN=1
        REGEN_REASON="检测到 RUN 续行 bug(字面 \\n),需重新生成"
    elif [ "$WITH_QODER_CLI" -eq 1 ] && ! grep -q "qodercli" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含国际版但缺 qodercli 安装行,需重新生成"
    elif [ "$WITH_QODER_CLI_CN" -eq 1 ] && ! grep -q "qoder.cn/install" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含国内版但缺 qoder.cn/install 安装行,需重新生成"
    elif [ "$WITH_KIMI_CLI" -eq 1 ] && ! grep -q "@moonshot-ai/kimi-code" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Kimi 但缺 @moonshot-ai/kimi-code 安装行,需重新生成"
    elif [ "$WITH_HERMES_CLI" -eq 1 ] && ! grep -q "hermes-agent.nousresearch.com/install.sh" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Hermes 但缺官方 install.sh 安装行(旧版用 pip install hermes-agent 会失败),需重新生成"
    elif [ "$WITH_CODEX_CLI" -eq 1 ] && ! grep -q "@openai/codex" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Codex 但缺 @openai/codex 安装行,需重新生成"
    elif [ "$WITH_SEMGREP" -eq 1 ] && ! grep -q "pip install.*semgrep" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Semgrep 但缺 semgrep 安装行,需重新生成"
    fi
fi

# 已存在且需要重新生成 → 备份原文件(不丢用户自定义内容)
if [ "$NEED_REGEN" -eq 1 ] && [ -f "$DOCKERFILE" ]; then
    BACKUP="${DOCKERFILE}.bak"
    cp "$DOCKERFILE" "$BACKUP"
    echo "[INFO] $DOCKERFILE 已存在,$REGEN_REASON"
    echo "       原文件备份到 $BACKUP(含用户自定义内容,可手动合并回新 Dockerfile)"
fi

if [ "$NEED_REGEN" -eq 1 ]; then
    # ---- 生成 Dockerfile:基础部分 ----
    # 基础工具始终含 curl(国内版安装脚本 + NodeSource 都需要)
    cat > "$DOCKERFILE" <<'EOF'
# @qoder-cli:__QODER_CLI_MARKER__
# @qoder-cli-cn:__QODER_CLI_CN_MARKER__
# @kimi-cli:__KIMI_CLI_MARKER__
# @hermes-cli:__HERMES_CLI_MARKER__
# @codex-cli:__CODEX_CLI_MARKER__
# @semgrep:__SEMGREP_MARKER__
# @ssh:__SSH_MARKER__
# @github-mirror:__GITHUB_MIRROR_MARKER__
# @hermes-clone-url:__HERMES_CLONE_URL_MARKER__
# @hermes-local:__HERMES_LOCAL_MARKER__
# @registry:__REGISTRY_MARKER__
# @pypi-mirror:__PYPI_MIRROR_MARKER__
FROM __BASE_IMAGE__

# 避免 tzdata 等交互式安装卡住
ENV DEBIAN_FRONTEND=noninteractive
EOF

    # ---- 国内 apt 源(可选,--cn-mirror 时启用)----
    if [ "$APT_MIRROR" = "aliyun" ]; then
        cat >> "$DOCKERFILE" <<'EOF'
# 国内 apt 源加速(阿里云;ubuntu 24.04 DEB822 格式 + 旧 sources.list 兼容)
RUN sed -i 's@//.*archive.ubuntu.com@//mirrors.aliyun.com@g; s@//.*security.ubuntu.com@//mirrors.aliyun.com@g' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true \
    && sed -i 's@//.*archive.ubuntu.com@//mirrors.aliyun.com@g; s@//.*security.ubuntu.com@//mirrors.aliyun.com@g' \
        /etc/apt/sources.list 2>/dev/null || true
EOF
    fi

    # ---- 基础工具 ----
    cat >> "$DOCKERFILE" <<'EOF'
# 基础工具:git / ripgrep / python3 / awk / find / curl
# curl 用于:NodeSource 安装脚本(国际版/kimi)+ qoder.cn/install(国内版)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        python3 \
        python3-pip \
        ca-certificates \
        openssh-client \
        coreutils \
        findutils \
        gawk \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python
EOF

    # ---- 追加 Semgrep(内置 react_agent 的 run_semgrep 工具依赖,默认装,--no-semgrep 可省略)----
    if [ "$WITH_SEMGREP" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Semgrep(内置 react_agent 的 run_semgrep 工具依赖)----
# 预装进镜像,避免首次任务运行时 pip install(慢 + 非 root 用户撞 PEP 668)。
# 未预装时 run_semgrep 也会兜底自动安装(--user --break-system-packages),但首次耗时长。
RUN pip install --no-cache-dir --break-system-packages semgrep \
    && semgrep --version
EOF
    fi

    # ---- 追加 Node.js(若任一 Node 类 CLI 启用)----
    # 统一装 Node 22.x:qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,22.x 两者都兼容
    if [ "$NEED_NODE" -eq 1 ]; then
        if [ "$NPM_MIRROR" = "npmmirror" ]; then
            cat >> "$DOCKERFILE" <<'EOF'

# ---- Node.js 22.x(qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,统一用 22.x)----
# npm 全局源换成 npmmirror(国内加速 qodercli/kimi/codex 的 npm install -g)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set registry https://registry.npmmirror.com
EOF
        else
            cat >> "$DOCKERFILE" <<'EOF'

# ---- Node.js 22.x(qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,统一用 22.x)----
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
EOF
        fi
    fi

    # ---- 追加国际版 Qoder CLI ----
    if [ "$WITH_QODER_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Qoder CLI 国际版(qodercli,官方 npm 包 @qoder-ai/qodercli)----
# 见 https://docs.qoder.com/cli/install
# Node.js 已由上面的 setup_22.x 安装(qodercli 兼容 Node 22)
USER root
RUN npm install -g @qoder-ai/qodercli
EOF
    fi

    # ---- 追加 Kimi Code CLI ----
    if [ "$WITH_KIMI_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Kimi Code CLI(开源 https://github.com/MoonshotAI/kimi-code)----
# 官方 npm 包 @moonshot-ai/kimi-code,bin 名 kimi
# 见 docs/opensandbox-deploy.md 2.4 节
# Node.js 已由上面的 setup_22.x 安装(kimi 要求 >= 22.19)
USER root
RUN npm install -g @moonshot-ai/kimi-code \
    && kimi --version
EOF
    fi

    # ---- 追加国内版 Qoder CN CLI(零依赖二进制)----
    if [ "$WITH_QODER_CLI_CN" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Qoder CN CLI 国内版(qoderclicn,原通义灵码,零依赖二进制)----
# 见 https://qoder.cn(install 脚本自动适配 arm64/amd64)
# install 脚本把版本化二进制装到 ~/.qoder-cn/bin/qoderclicn/qoderclicn-<ver>,
# 并在 ~/.local/bin/qoderclicn 创建 symlink。root 执行时入口点在 /root/.local/bin,
# 切到非 root 用户后 PATH 不含该路径。用 cp -L 跟随 symlink 复制实际二进制到
# /usr/local/bin 确保所有用户可用(不依赖 symlink 目标的版本号路径)。
USER root
RUN curl -fsSL https://qoder.cn/install | bash \
    && test -e /root/.local/bin/qoderclicn \
    && cp -L /root/.local/bin/qoderclicn /usr/local/bin/qoderclicn \
    && chmod +x /usr/local/bin/qoderclicn \
    && /usr/local/bin/qoderclicn --version
EOF
    fi

    # ---- 追加 Hermes CLI(官方 install.sh 安装,需 Python >=3.11)----
    if [ "$WITH_HERMES_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Hermes CLI(开源 https://github.com/NousResearch/hermes-agent)----
# 注意:hermes-agent 未发布到 PyPI,`pip install hermes-agent` 会失败;改用官方 install.sh(同 README)。
# 脚本自动:装 uv + Python 3.11 → clone 源码 → 建 venv → 装依赖 → 符号链接 hermes 命令。
# 需 Python >=3.11:基础镜像 24.04 自带系统 python3 = 3.12(供 apt + 智能体脚本用);
# install.sh 通用路径仍用 uv 装隔离的 3.11 给 Hermes venv(两者均 >=3.11,互不冲突)。
# bin 名 hermes(hermes acp 启动 ACP)。
#
# root 安装走 FHS 布局:代码 /usr/local/lib/hermes-agent,命令 /usr/local/bin/hermes(全用户 PATH 可达),
# uv 管理的 Python 放 /usr/local/share(世界可读,避免 venv 解释器符号链接被困在 /root,非 root user 无权访问)。
#
# --skip-setup      跳过交互式配置向导(API Key 等运行时由 hermes_cli_agent.py 注入)
# --skip-browser    跳过 Playwright/Node 浏览器依赖(hermes acp 不需要,减小镜像体积)
# --non-interactive 非 tty 下防止任何提示卡住(curl|bash 风格管道时尤其重要)
# 两步「先下载再执行」比 curl|bash 更安全、可审计(避免 pipefail 缺失掩盖 curl 失败)。
EOF
        if [ "$PYPI_MIRROR" = "aliyun" ]; then
            cat >> "$DOCKERFILE" <<'EOF'
# PyPI 国内源加速(--cn-mirror 时启用):Hermes install.sh 用 uv 拉 Python 依赖,
# 默认走 pypi.org 国内极慢(尤其 uv 解析 [all] extras + 下载大包如 playwright)。
# 设 UV_INDEX_URL + PIP_INDEX_URL,让 uv 和子进程 pip 都走阿里云 PyPI 镜像。
USER root
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_DISABLE_PIP_VERSION_CHECK=1
EOF
        else
            cat >> "$DOCKERFILE" <<'EOF'
USER root
EOF
        fi
        # ---- 宿主机预克隆源码(--hermes-local-dir):COPY 进容器,install.sh 跳过 clone ----
        if [ -n "$HERMES_LOCAL_DIR" ]; then
            cat >> "$DOCKERFILE" <<'EOF'
# ---- Hermes 源码(宿主机预克隆目录,--hermes-local-dir)----
# 直接把宿主机 clone 好的仓库 COPY 进镜像(含 .git),install.sh 检测到已有 git 仓库会跳过 clone,
# 走 update 流程(stash → fetch → checkout → pull → 装依赖)。
# 注意:update 的 git fetch origin 走 origin URL,容器内必须可达——建议搭配 --github-mirror
# (insteadOf 会把 SSH/HTTPS 都重写到镜像,见下方 RUN)。
# .git 单独 COPY:构建上下文里改名 hermes-git-meta,绕开 .dockerignore 的 .git 排除规则。
COPY hermes-local/ /usr/local/lib/hermes-agent/
COPY hermes-git-meta/ /usr/local/lib/hermes-agent/.git/
EOF
        fi
        # ---- Hermes RUN:前置配置按开关动态生成(SSH known_hosts / GitHub 镜像加速)----
        # 镜像加速用 GIT_CONFIG_GLOBAL 指向临时配置文件,只在本 RUN 内生效,
        # 不写进最终镜像(避免运行时沙箱 clone 私有仓库被镜像劫持,认证走不了)。
        HERMES_RUN_PREFIX=""
        if [ -n "$HERMES_CLONE_URL" ]; then
            # 预克隆到 FHS root 布局的代码目录(与 install.sh 解析结果一致):
            # - 目录已存在(层缓存/重跑)时跳过 clone,install.sh 会走已有仓库的更新流程
            # - 分支固定 main(install.sh 写死 BRANCH=main)
            # - clone URL 即 origin remote,install.sh 的 fetch/pull 也走它,所以给镜像 URL 才能全程避开直连
            HERMES_RUN_PREFIX="if [ ! -d /usr/local/lib/hermes-agent/.git ]; then git clone --depth 1 --branch main \"${HERMES_CLONE_URL}\" /usr/local/lib/hermes-agent; fi && "
        fi
        if [ "$WITH_SSH" -eq 1 ]; then
            HERMES_RUN_PREFIX="${HERMES_RUN_PREFIX}mkdir -p /root/.ssh && (ssh-keyscan -T 5 github.com >> /root/.ssh/known_hosts 2>/dev/null || true) && "
        fi
        if [ -n "$GITHUB_MIRROR" ]; then
            # HTTPS 与 SSH 两种 URL 都重写到镜像前缀(ghproxy 风格:<base>/<原URL>);
            # install.sh 的 SSH clone 也会被重写为镜像 HTTPS,直接跳过 SSH 尝试
            HERMES_RUN_PREFIX="${HERMES_RUN_PREFIX}export GIT_CONFIG_GLOBAL=/tmp/git-github-mirror.conf && git config --global url.\"${GITHUB_MIRROR}/https://github.com/\".insteadOf \"https://github.com/\" && git config --global url.\"${GITHUB_MIRROR}/https://github.com/\".insteadOf \"git@github.com:\" && export UV_PYTHON_INSTALL_MIRROR=${GITHUB_MIRROR}/https://github.com/astral-sh/python-build-standalone/releases/download && "
        fi
        # RUN 指令头(--ssh 时挂 SSH agent)
        cat >> "$DOCKERFILE" <<'EOF'
# install.sh 默认不装 [anthropic] extra;anthropic_messages 模式的 provider
# (anthropic/minimax)需要 anthropic Python 包,这里补装(版本与 pyproject.toml 对齐)。
# uv venv 默认不含 pip,先 ensurepip 引导。本 RUN 内不写 # 注释行(续行合并后 # 会吞后续命令)。
#
# sed 容忍 npm 失败:install.sh 的 install_node_deps 给 hermes-agent monorepo 跑 npm install
# (含 electron/agent-browser 等浏览器工具依赖,二进制从 github releases 下载,国内必挂),
# 且新版 install.sh 中 npm 失败会直接退出(--skip-browser 只管 Playwright 不管 npm install)。
# AgentPair 用 hermes acp(纯 Python ACP)不需要浏览器工具,改成 || true 容忍失败继续安装。
EOF
        if [ "$WITH_SSH" -eq 1 ]; then
            cat >> "$DOCKERFILE" <<'EOF'
# SSH 转发(--ssh):BuildKit 把宿主机 SSH agent/key 挂进构建容器,install.sh 的 GitHub SSH clone 直接成功。
# 预写 known_hosts:install.sh 用 GIT_SSH_COMMAND="ssh -o BatchMode=yes" 克隆,
# BatchMode 禁止交互确认,host key 未知会直接失败,必须提前写入。
EOF
            echo "RUN --mount=type=ssh \\" >> "$DOCKERFILE"
        else
            cat >> "$DOCKERFILE" <<'EOF'
EOF
            echo "RUN \\" >> "$DOCKERFILE"
        fi
        if [ -n "$HERMES_RUN_PREFIX" ]; then
            # 末尾已带 " && ",直接续行连接下一条命令(curl 行);echo 输出行尾续行符 "\"
            echo "    ${HERMES_RUN_PREFIX}\\" >> "$DOCKERFILE"
        fi
        cat >> "$DOCKERFILE" <<'EOF'
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh \
    && sed -i 's/install_node_deps || return/install_node_deps || true/g' /tmp/hermes-install.sh \
    && bash /tmp/hermes-install.sh --skip-setup --skip-browser --skip-computer-use --non-interactive \
    && rm -f /tmp/hermes-install.sh \
    && hermes --version \
    && /usr/local/lib/hermes-agent/venv/bin/python -m ensurepip \
    && /usr/local/lib/hermes-agent/venv/bin/python -m pip install 'anthropic==0.87.0'
EOF
    fi

    # ---- 追加 Codex CLI(OpenAI 官方,npm 安装)----
    if [ "$WITH_CODEX_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Codex CLI(OpenAI 官方,开源 https://github.com/openai/codex)----
# 官方 npm 包 @openai/codex,bin 名 codex(codex exec --json 非交互模式)
# 不原生支持 ACP,通过 codex_bridge.py 翻译 codex exec --json JSONL → ACP
# Node.js 已由上面的 setup_22.x 安装(codex 要求 >= 16,Node 22 兼容)
USER root
RUN npm install -g @openai/codex \
    && codex --version
EOF
    fi

    # ---- 追加非 root 用户 ----
    cat >> "$DOCKERFILE" <<'EOF'

# 沙箱默认非 root 用户 user,确保 home 目录存在
RUN useradd -m -s /bin/bash user
USER user
WORKDIR /home/user
EOF

    # 替换标记占位符为实际值(BASE_IMAGE 含 /,用 # 作 sed 分隔符)
    sed -i \
        -e "s/__QODER_CLI_MARKER__/$expect_qoder_cli_marker/" \
        -e "s/__QODER_CLI_CN_MARKER__/$expect_qoder_cli_cn_marker/" \
        -e "s/__KIMI_CLI_MARKER__/$expect_kimi_cli_marker/" \
        -e "s/__HERMES_CLI_MARKER__/$expect_hermes_cli_marker/" \
        -e "s/__CODEX_CLI_MARKER__/$expect_codex_cli_marker/" \
        -e "s/__SEMGREP_MARKER__/$expect_semgrep_marker/" \
        -e "s/__SSH_MARKER__/$expect_ssh_marker/" \
        -e "s|__GITHUB_MIRROR_MARKER__|$expect_github_mirror_marker|" \
        -e "s|__HERMES_CLONE_URL_MARKER__|$expect_hermes_clone_url_marker|" \
        -e "s/__HERMES_LOCAL_MARKER__/$expect_hermes_local_marker/" \
        -e "s/__REGISTRY_MARKER__/$expect_registry_marker/" \
        -e "s/__PYPI_MIRROR_MARKER__/$expect_pypi_mirror_marker/" \
        -e "s#__BASE_IMAGE__#$BASE_IMAGE#" \
        "$DOCKERFILE"

    echo "[OK] 已生成 $DOCKERFILE($REGEN_REASON)"
    echo "     国际版(qodercli):$([ "$WITH_QODER_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     国内版(qoderclicn):$([ "$WITH_QODER_CLI_CN" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Kimi(kimi):$([ "$WITH_KIMI_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Hermes(hermes):$([ "$WITH_HERMES_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Codex(codex):$([ "$WITH_CODEX_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Semgrep(semgrep):$([ "$WITH_SEMGREP" -eq 1 ] && echo '装' || echo '不装')"
    echo "     SSH 转发:$([ "$WITH_SSH" -eq 1 ] && echo '启用(--ssh)' || echo '关闭')"
    echo "     GitHub 镜像:${GITHUB_MIRROR:-关闭(直连)}"
    echo "     Hermes 预克隆:${HERMES_CLONE_URL:-关闭(install.sh 自 clone)}"
    echo "     Hermes 本地目录:${HERMES_LOCAL_DIR:-关闭(install.sh 自 clone)}"
    echo "     镜像源:${REGISTRY:-默认(docker.io)}${APT_MIRROR:+ / apt=$APT_MIRROR}${NPM_MIRROR:+ / npm=$NPM_MIRROR}${PYPI_MIRROR:+ / pypi=$PYPI_MIRROR}"
else
    echo "[INFO] $DOCKERFILE 已存在且符合要求,直接使用(如需重新生成请先删除)"
fi

# ---------- 构建镜像 ----------
echo "[INFO] 开始构建 $IMAGE_NAME:$IMAGE_TAG ..."
echo "       国际版(qodercli):$([ "$WITH_QODER_CLI" -eq 1 ] && echo '含' || echo '不含')"
echo "       国内版(qoderclicn):$([ "$WITH_QODER_CLI_CN" -eq 1 ] && echo '含' || echo '不含')"
echo "       Kimi(kimi):$([ "$WITH_KIMI_CLI" -eq 1 ] && echo '含' || echo '不含')"
echo "       Hermes(hermes):$([ "$WITH_HERMES_CLI" -eq 1 ] && echo '含' || echo '不含')"
echo "       Codex(codex):$([ "$WITH_CODEX_CLI" -eq 1 ] && echo '含' || echo '不含')"
echo "       Semgrep(semgrep):$([ "$WITH_SEMGREP" -eq 1 ] && echo '含' || echo '不含')"
echo "       SSH 转发:$([ "$WITH_SSH" -eq 1 ] && echo '启用' || echo '关闭')"
echo "       GitHub 镜像:${GITHUB_MIRROR:-关闭(直连)}"
echo "       Hermes 预克隆:${HERMES_CLONE_URL:-关闭(install.sh 自 clone)}"
echo "       Hermes 本地目录:${HERMES_LOCAL_DIR:-关闭(install.sh 自 clone)}"
echo "       镜像源:${REGISTRY:-默认(docker.io)}${APT_MIRROR:+ / apt=$APT_MIRROR}${NPM_MIRROR:+ / npm=$NPM_MIRROR}${PYPI_MIRROR:+ / pypi=$PYPI_MIRROR}"

# --ssh 启用时把宿主机 SSH key/agent 传进构建(RUN --mount=type=ssh 才能用)
# DOCKER_BUILDKIT=1 兼容旧版 Docker(23+ 默认已启用,该变量无害)
SSH_BUILD_ARGS=""
if [ "$WITH_SSH" -eq 1 ]; then
    if [ -n "$SSH_KEY" ]; then
        SSH_BUILD_ARGS="--ssh default=$SSH_KEY"
    else
        SSH_BUILD_ARGS="--ssh default"
    fi
    echo "[INFO] SSH 转发已启用($SSH_BUILD_ARGS),Hermes install.sh 将用宿主机 SSH key clone"
fi
if [ -n "$HERMES_LOCAL_DIR" ]; then
    # 复制到构建上下文(上下文是脚本运行目录):hermes-local 装源码,hermes-git-meta 装 .git
    # (改名的原因:.dockerignore 的 .git 规则会排除构建上下文里所有 .git 目录)
    echo "[INFO] 复制 Hermes 源码到构建上下文(hermes-local/ + hermes-git-meta/,保留 .git)..."
    rm -rf hermes-local hermes-git-meta
    cp -a "$HERMES_LOCAL_DIR" hermes-local
    cp -a "$HERMES_LOCAL_DIR/.git" hermes-git-meta
    SRC_SIZE=$(du -sh "$HERMES_LOCAL_DIR" 2>/dev/null | cut -f1)
    echo "       源码体积: ${SRC_SIZE:-?}(上下文打包会多花一些时间)"
fi
DOCKER_BUILDKIT=1 docker build $SSH_BUILD_ARGS -f "$DOCKERFILE" -t "$IMAGE_NAME:$IMAGE_TAG" .
if [ -n "$HERMES_LOCAL_DIR" ]; then
    # 清理上下文中的临时副本(避免下次构建把大目录打进上下文)
    rm -rf hermes-local hermes-git-meta
fi
echo "[OK] 镜像构建完成"

# ---------- 验证镜像内工具 ----------
echo "[INFO] 验证镜像内必要工具 ..."
MISSING=0
for cmd in git rg python3 awk find curl; do
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v $cmd" >/dev/null 2>&1; then
        echo "  [OK]   $cmd"
    else
        echo "  [FAIL] $cmd 缺失"
        MISSING=1
    fi
done

if [ "$WITH_SEMGREP" -eq 1 ]; then
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v semgrep" >/dev/null 2>&1; then
        echo "  [OK]   semgrep"
    else
        echo "  [FAIL] semgrep 缺失"
        MISSING=1
    fi
fi

# Node 类 CLI 共享 Node.js 运行时,任一启用就验证 node/npm
if [ "$NEED_NODE" -eq 1 ]; then
    echo "[INFO] 验证 Node.js 运行时(qodercli / kimi / codex 共用)..."
    for cmd in node npm; do
        if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v $cmd" >/dev/null 2>&1; then
            echo "  [OK]   $cmd"
        else
            echo "  [FAIL] $cmd 缺失"
            MISSING=1
        fi
    done
    # 验证 Node 版本 >= 22(kimi 硬要求)
    NODE_VER=$(docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "node --version" 2>/dev/null || echo "v0.0.0")
    NODE_MAJOR=$(echo "$NODE_VER" | sed -E 's/v([0-9]+)\..*/\1/')
    if [ "$NODE_MAJOR" -ge 22 ] 2>/dev/null; then
        echo "  [OK]   Node 版本 $NODE_VER(>= 22,满足 kimi 要求)"
    else
        echo "  [FAIL] Node 版本 $NODE_VER 过低(kimi 要求 >= 22.19)"
        MISSING=1
    fi
fi

if [ "$WITH_QODER_CLI" -eq 1 ]; then
    echo "[INFO] 验证 Qoder CLI 国际版依赖 ..."
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v qodercli" >/dev/null 2>&1; then
        echo "  [OK]   qodercli"
    else
        echo "  [FAIL] qodercli 缺失"
        MISSING=1
    fi
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "qodercli --version" >/dev/null 2>&1; then
        echo "  [OK]   qodercli --version 可执行"
    else
        echo "  [WARN] qodercli --version 执行失败(可能是首次需登录,不影响镜像可用性)"
    fi
fi

if [ "$WITH_KIMI_CLI" -eq 1 ]; then
    echo "[INFO] 验证 Kimi Code CLI 依赖 ..."
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v kimi" >/dev/null 2>&1; then
        echo "  [OK]   kimi"
    else
        echo "  [FAIL] kimi 缺失"
        MISSING=1
    fi
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "kimi --version" >/dev/null 2>&1; then
        echo "  [OK]   kimi --version 可执行"
    else
        echo "  [WARN] kimi --version 执行失败(可能是首次需登录,不影响镜像可用性)"
    fi
fi

if [ "$WITH_QODER_CLI_CN" -eq 1 ]; then
    echo "[INFO] 验证 Qoder CN CLI 国内版依赖 ..."
    # qoderclicn 是零依赖二进制,只需验证命令本身存在
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v qoderclicn" >/dev/null 2>&1; then
        echo "  [OK]   qoderclicn"
    else
        echo "  [FAIL] qoderclicn 缺失"
        MISSING=1
    fi
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "qoderclicn --version" >/dev/null 2>&1; then
        echo "  [OK]   qoderclicn --version 可执行"
    else
        echo "  [WARN] qoderclicn --version 执行失败(可能是首次需登录,不影响镜像可用性)"
    fi
fi

if [ "$WITH_HERMES_CLI" -eq 1 ]; then
    echo "[INFO] 验证 Hermes CLI 依赖 ..."
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v hermes" >/dev/null 2>&1; then
        echo "  [OK]   hermes"
    else
        echo "  [FAIL] hermes 缺失"
        MISSING=1
    fi
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "hermes --version" >/dev/null 2>&1; then
        echo "  [OK]   hermes --version 可执行"
    else
        echo "  [WARN] hermes --version 执行失败(不影响镜像可用性,运行时按需初始化)"
    fi
fi

if [ "$WITH_CODEX_CLI" -eq 1 ]; then
    echo "[INFO] 验证 Codex CLI 依赖 ..."
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v codex" >/dev/null 2>&1; then
        echo "  [OK]   codex"
    else
        echo "  [FAIL] codex 缺失"
        MISSING=1
    fi
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "codex --version" >/dev/null 2>&1; then
        echo "  [OK]   codex --version 可执行"
    else
        echo "  [WARN] codex --version 执行失败(不影响镜像可用性,运行时按需初始化)"
    fi
fi

if [ "$MISSING" -ne 0 ]; then
    echo "[FAIL] 镜像缺少必要工具,请检查 $DOCKERFILE"
    exit 1
fi

# ---------- 完成 ----------
echo ""
echo "[OK] 全部就绪。在 AgentPair backend/.env 设:"
echo "    SANDBOX_IMAGE=$IMAGE_NAME:$IMAGE_TAG"
if [ "$WITH_QODER_CLI" -eq 1 ] || [ "$WITH_QODER_CLI_CN" -eq 1 ] || [ "$WITH_KIMI_CLI" -eq 1 ] || [ "$WITH_HERMES_CLI" -eq 1 ] || [ "$WITH_CODEX_CLI" -eq 1 ]; then
    echo ""
    [ "$WITH_QODER_CLI" -eq 1 ] && echo "[OK] Qoder CLI 国际版已预装,支持 task.executor=qoder_cli"
    [ "$WITH_QODER_CLI_CN" -eq 1 ] && echo "[OK] Qoder CN CLI 国内版已预装,支持 task.executor=qoder_cli_cn"
    [ "$WITH_KIMI_CLI" -eq 1 ] && echo "[OK] Kimi Code CLI 已预装,支持 task.executor=kimi_cli"
    [ "$WITH_HERMES_CLI" -eq 1 ] && echo "[OK] Hermes CLI 已预装,支持 task.executor=hermes_cli"
    [ "$WITH_CODEX_CLI" -eq 1 ] && echo "[OK] Codex CLI 已预装,支持 task.executor=codex_cli"
    echo ""
    echo "     凭证配置:用户在「智能体配置」中填入对应凭证即可"
    [ "$WITH_QODER_CLI" -eq 1 ] && echo "       - 国际版 PAT:qoder.com/account/integrations 生成"
    [ "$WITH_QODER_CLI_CN" -eq 1 ] && echo "       - 国内版 PAT:qoder.cn/account/integrations 生成"
    [ "$WITH_KIMI_CLI" -eq 1 ] && echo "       - Kimi:LLM API Key(如 platform.moonshot.cn 申请)或自部署端点"
    [ "$WITH_HERMES_CLI" -eq 1 ] && echo "       - Hermes:LLM API Key(OpenRouter/Anthropic/OpenAI/GLM/Kimi/MiniMax/Gemini 任选)"
    [ "$WITH_CODEX_CLI" -eq 1 ] && echo "       - Codex:OpenAI API Key 或自定义 OpenAI 兼容端点(含 base_url + wire_api)"
fi
