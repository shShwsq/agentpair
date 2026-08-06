#!/usr/bin/env bash
# 构建 AgentPair 沙箱镜像(预装 git / ripgrep / python3 / awk / find)
#
# 默认同时预装五款 CLI 执行器:
#   - Qoder CLI(国际版):Node.js + @qoder-ai/qodercli,需 qoder.com 账号
#   - Qoder CN CLI(国内版,原通义灵码):零依赖二进制,仅需 curl,需 qoder.cn 账号
#   - Kimi Code CLI(开源):Node.js + @moonshot-ai/kimi-code,需 LLM API Key
#   - Hermes CLI(开源):Python + pip install hermes-agent,需 LLM API Key(支持多供应商)
#   - Codex CLI(OpenAI 官方,开源):Node.js + @openai/codex,需 LLM API Key(支持自定义端点)
#
# 支持 task.executor=qoder_cli / qoder_cli_cn / kimi_cli / hermes_cli / codex_cli。
#
# Node 版本策略:qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,codex 要求 >= 16。
# 只要任一 Node 类 CLI 启用(qoder_cli / kimi_cli / codex_cli),统一装 Node 22.x(三者都兼容)。
# Hermes CLI 是纯 Python 包,不需要 Node.js(镜像已含 python3 + pip)。
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
# - Hermes CLI:纯 Python 包(pip install,镜像已含 python3 + pip,体积增量取决于依赖)
# - Codex CLI:Node.js + npm(OpenAI 官方,与 Node 类 CLI 共享 Node 22.x 运行时)
WITH_QODER_CLI=1
WITH_QODER_CLI_CN=1
WITH_KIMI_CLI=1
WITH_HERMES_CLI=1
WITH_CODEX_CLI=1
for arg in "$@"; do
    case "$arg" in
        --with-qoder-cli)
            WITH_QODER_CLI=1
            ;;
        --no-qoder-cli)
            WITH_QODER_CLI=0
            ;;
        --with-qoder-cli-cn)
            WITH_QODER_CLI_CN=1
            ;;
        --no-qoder-cli-cn)
            WITH_QODER_CLI_CN=0
            ;;
        --with-kimi-cli)
            WITH_KIMI_CLI=1
            ;;
        --no-kimi-cli)
            WITH_KIMI_CLI=0
            ;;
        --with-hermes-cli)
            WITH_HERMES_CLI=1
            ;;
        --no-hermes-cli)
            WITH_HERMES_CLI=0
            ;;
        --with-codex-cli)
            WITH_CODEX_CLI=1
            ;;
        --no-codex-cli)
            WITH_CODEX_CLI=0
            ;;
        -h|--help)
            echo "用法:bash $0 [--with-qoder-cli|--no-qoder-cli] [--with-qoder-cli-cn|--no-qoder-cli-cn] [--with-kimi-cli|--no-kimi-cli] [--with-hermes-cli|--no-hermes-cli] [--with-codex-cli|--no-codex-cli]"
            echo ""
            echo "选项(均可组合,默认全部启用):"
            echo "  --with-qoder-cli       预装 Qoder CLI 国际版(Node.js + npm,需 qoder.com 账号)"
            echo "  --no-qoder-cli         不装国际版"
            echo "  --with-qoder-cli-cn    预装 Qoder CN CLI 国内版(零依赖二进制,需 qoder.cn 账号)"
            echo "  --no-qoder-cli-cn      不装国内版"
            echo "  --with-kimi-cli        预装 Kimi Code CLI(Node.js + npm,需 LLM API Key)"
            echo "  --no-kimi-cli          不装 kimi"
            echo "  --with-hermes-cli      预装 Hermes CLI(Python pip,需 LLM API Key,支持多供应商)"
            echo "  --no-hermes-cli        不装 hermes"
            echo "  --with-codex-cli       预装 Codex CLI(Node.js + npm,OpenAI 官方,需 LLM API Key)"
            echo "  --no-codex-cli         不装 codex"
            echo ""
            echo "基础工具(git/rg/python3/awk/find/curl)始终预装。"
            echo "Node.js 版本:只要 qoder_cli / kimi_cli / codex_cli 任一启用,统一装 Node 22.x(三者都兼容)。"
            echo "Hermes CLI 是纯 Python 包,不需要 Node.js(镜像已含 python3 + pip)。"
            exit 0
            ;;
        *)
            echo "[FAIL] 未知参数: $arg(用 -h 查看帮助)"
            exit 1
            ;;
    esac
done

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
NEED_REGEN=0
REGEN_REASON=""
expect_qoder_cli_marker=$([ "$WITH_QODER_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_qoder_cli_cn_marker=$([ "$WITH_QODER_CLI_CN" -eq 1 ] && echo "yes" || echo "no")
expect_kimi_cli_marker=$([ "$WITH_KIMI_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_hermes_cli_marker=$([ "$WITH_HERMES_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_codex_cli_marker=$([ "$WITH_CODEX_CLI" -eq 1 ] && echo "yes" || echo "no")

if [ ! -f "$DOCKERFILE" ]; then
    NEED_REGEN=1
    REGEN_REASON="文件不存在,全新生成"
else
    cur_qoder_cli=$(grep -E "^# @qoder-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_qoder_cli_cn=$(grep -E "^# @qoder-cli-cn:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_kimi_cli=$(grep -E "^# @kimi-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_hermes_cli=$(grep -E "^# @hermes-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_codex_cli=$(grep -E "^# @codex-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    if [ "$cur_qoder_cli" != "$expect_qoder_cli_marker" ]; then
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
    elif [ "$WITH_QODER_CLI" -eq 1 ] && ! grep -q "qodercli" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含国际版但缺 qodercli 安装行,需重新生成"
    elif [ "$WITH_QODER_CLI_CN" -eq 1 ] && ! grep -q "qoder.cn/install" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含国内版但缺 qoder.cn/install 安装行,需重新生成"
    elif [ "$WITH_KIMI_CLI" -eq 1 ] && ! grep -q "@moonshot-ai/kimi-code" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Kimi 但缺 @moonshot-ai/kimi-code 安装行,需重新生成"
    elif [ "$WITH_HERMES_CLI" -eq 1 ] && ! grep -q "hermes-agent" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Hermes 但缺 hermes-agent 安装行,需重新生成"
    elif [ "$WITH_CODEX_CLI" -eq 1 ] && ! grep -q "@openai/codex" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Codex 但缺 @openai/codex 安装行,需重新生成"
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
FROM ubuntu:22.04

# 避免 tzdata 等交互式安装卡住
ENV DEBIAN_FRONTEND=noninteractive

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

    # ---- 追加 Node.js(若任一 Node 类 CLI 启用)----
    # 统一装 Node 22.x:qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,22.x 两者都兼容
    if [ "$NEED_NODE" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Node.js 22.x(qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,统一用 22.x)----
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
EOF
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

    # ---- 追加 Hermes CLI(纯 Python 包,pip 安装)----
    if [ "$WITH_HERMES_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Hermes CLI(开源 https://github.com/NousResearch/hermes-agent)----
# PyPI 包 hermes-agent,bin 名 hermes(hermes acp 启动 ACP 服务)
# 纯 Python 包,镜像已含 python3 + pip,不需要 Node.js
# 支持 7 种 LLM 供应商(OpenRouter/Anthropic/OpenAI/GLM/Kimi/MiniMax/Gemini)
# --no-cache-dir 减小镜像体积(不缓存 pip 下载)
USER root
RUN pip3 install --no-cache-dir hermes-agent \
    && hermes --version
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

    # 替换标记占位符为实际值
    sed -i \
        -e "s/__QODER_CLI_MARKER__/$expect_qoder_cli_marker/" \
        -e "s/__QODER_CLI_CN_MARKER__/$expect_qoder_cli_cn_marker/" \
        -e "s/__KIMI_CLI_MARKER__/$expect_kimi_cli_marker/" \
        -e "s/__HERMES_CLI_MARKER__/$expect_hermes_cli_marker/" \
        -e "s/__CODEX_CLI_MARKER__/$expect_codex_cli_marker/" \
        "$DOCKERFILE"

    echo "[OK] 已生成 $DOCKERFILE($REGEN_REASON)"
    echo "     国际版(qodercli):$([ "$WITH_QODER_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     国内版(qoderclicn):$([ "$WITH_QODER_CLI_CN" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Kimi(kimi):$([ "$WITH_KIMI_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Hermes(hermes):$([ "$WITH_HERMES_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Codex(codex):$([ "$WITH_CODEX_CLI" -eq 1 ] && echo '装' || echo '不装')"
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
docker build -f "$DOCKERFILE" -t "$IMAGE_NAME:$IMAGE_TAG" .
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
