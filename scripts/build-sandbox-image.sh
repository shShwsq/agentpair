#!/usr/bin/env bash
# 构建 AgentPair 沙箱镜像(预装 git / ripgrep / python3 / awk / find)
#
# 默认同时预装两版 Qoder CLI:
#   - Qoder CLI(国际版):Node.js >= 20.0.0 + @qoder-ai/qodercli,需 qoder.com 账号
#   - Qoder CN CLI(国内版,原通义灵码):零依赖二进制,仅需 curl,需 qoder.cn 账号
# 支持 task.executor=qoder_cli / qoder_cli_cn。
#
# 用法:在装好 docker 的 Linux 服务器上执行
#   bash scripts/build-sandbox-image.sh                              # 默认装两版 CLI
#   bash scripts/build-sandbox-image.sh --no-qoder-cli               # 不装国际版
#   bash scripts/build-sandbox-image.sh --no-qoder-cli-cn            # 不装国内版
#   bash scripts/build-sandbox-image.sh --no-qoder-cli --no-qoder-cli-cn  # 仅基础工具
#   bash scripts/build-sandbox-image.sh --with-qoder-cli             # 显式指定(默认行为)
#
# 构建 agentpair-sandbox:latest 后,在 AgentPair backend/.env 设:
#   SANDBOX_IMAGE=agentpair-sandbox:latest

set -euo pipefail

IMAGE_NAME="agentpair-sandbox"
IMAGE_TAG="latest"
DOCKERFILE="Dockerfile.sandbox"

# ---------- 参数解析 ----------
# 两个 CLI 独立开关,默认都装
# - 国际版 qodercli:需 Node.js + npm(镜像体积较大,约 +200MB)
# - 国内版 qoderclicn:零依赖二进制(仅需 curl,体积忽略不计)
WITH_QODER_CLI=1
WITH_QODER_CLI_CN=1
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
        -h|--help)
            echo "用法:bash $0 [--with-qoder-cli|--no-qoder-cli] [--with-qoder-cli-cn|--no-qoder-cli-cn]"
            echo ""
            echo "选项(均可组合,默认全部启用):"
            echo "  --with-qoder-cli       预装 Qoder CLI 国际版(Node.js + npm,需 qoder.com 账号)"
            echo "  --no-qoder-cli         不装国际版"
            echo "  --with-qoder-cli-cn    预装 Qoder CN CLI 国内版(零依赖二进制,需 qoder.cn 账号)"
            echo "  --no-qoder-cli-cn      不装国内版"
            echo ""
            echo "基础工具(git/rg/python3/awk/find/curl)始终预装。"
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

# ---------- 生成 Dockerfile(若不存在或配置不符) ----------
# 检测:Dockerfile 现状与本次期望的 CLI 组合是否一致,不一致则备份重生成。
# 不用 sed 追加(反引号/转义/位置错乱等问题太多),直接按期望状态覆盖最可靠。
#
# 期望标记(在 Dockerfile 中以注释形式存在,便于检测):
#   # @qoder-cli:yes / # @qoder-cli:no      国际版状态
#   # @qoder-cli-cn:yes / # @qoder-cli-cn:no 国内版状态
NEED_REGEN=0
REGEN_REASON=""
expect_qoder_cli_marker=$([ "$WITH_QODER_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_qoder_cli_cn_marker=$([ "$WITH_QODER_CLI_CN" -eq 1 ] && echo "yes" || echo "no")

if [ ! -f "$DOCKERFILE" ]; then
    NEED_REGEN=1
    REGEN_REASON="文件不存在,全新生成"
else
    cur_qoder_cli=$(grep -E "^# @qoder-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_qoder_cli_cn=$(grep -E "^# @qoder-cli-cn:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    if [ "$cur_qoder_cli" != "$expect_qoder_cli_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="国际版配置变更($cur_qoder_cli → $expect_qoder_cli_marker)"
    elif [ "$cur_qoder_cli_cn" != "$expect_qoder_cli_cn_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="国内版配置变更($cur_qoder_cli_cn → $expect_qoder_cli_cn_marker)"
    elif [ "$WITH_QODER_CLI" -eq 1 ] && ! grep -q "qodercli" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含国际版但缺 qodercli 安装行,需重新生成"
    elif [ "$WITH_QODER_CLI_CN" -eq 1 ] && ! grep -q "qoder.cn/install" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含国内版但缺 qoder.cn/install 安装行,需重新生成"
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
FROM ubuntu:22.04

# 避免 tzdata 等交互式安装卡住
ENV DEBIAN_FRONTEND=noninteractive

# 基础工具:git / ripgrep / python3 / awk / find / curl
# curl 用于:NodeSource 安装脚本(国际版)+ qoder.cn/install(国内版)
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

    # ---- 追加国际版 Qoder CLI(Node.js + npm)----
    if [ "$WITH_QODER_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Qoder CLI 国际版(qodercli,需 Node.js >= 20.0.0)----
# 见 https://docs.qoder.com/cli/install
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @qoder-ai/qodercli
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
        "$DOCKERFILE"

    echo "[OK] 已生成 $DOCKERFILE($REGEN_REASON)"
    echo "     国际版(qodercli):$([ "$WITH_QODER_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     国内版(qoderclicn):$([ "$WITH_QODER_CLI_CN" -eq 1 ] && echo '装' || echo '不装')"
else
    echo "[INFO] $DOCKERFILE 已存在且符合要求,直接使用(如需重新生成请先删除)"
fi

# ---------- 构建镜像 ----------
echo "[INFO] 开始构建 $IMAGE_NAME:$IMAGE_TAG ..."
echo "       国际版(qodercli):$([ "$WITH_QODER_CLI" -eq 1 ] && echo '含' || echo '不含')"
echo "       国内版(qoderclicn):$([ "$WITH_QODER_CLI_CN" -eq 1 ] && echo '含' || echo '不含')"
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

if [ "$WITH_QODER_CLI" -eq 1 ]; then
    echo "[INFO] 验证 Qoder CLI 国际版依赖 ..."
    for cmd in node npm qodercli; do
        if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v $cmd" >/dev/null 2>&1; then
            echo "  [OK]   $cmd"
        else
            echo "  [FAIL] $cmd 缺失"
            MISSING=1
        fi
    done
    # 额外验证 Qoder CLI 版本(能正常执行即说明安装成功)
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "qodercli --version" >/dev/null 2>&1; then
        echo "  [OK]   qodercli --version 可执行"
    else
        echo "  [WARN] qodercli --version 执行失败(可能是首次需登录,不影响镜像可用性)"
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

if [ "$MISSING" -ne 0 ]; then
    echo "[FAIL] 镜像缺少必要工具,请检查 $DOCKERFILE"
    exit 1
fi

# ---------- 完成 ----------
echo ""
echo "[OK] 全部就绪。在 AgentPair backend/.env 设:"
echo "    SANDBOX_IMAGE=$IMAGE_NAME:$IMAGE_TAG"
if [ "$WITH_QODER_CLI" -eq 1 ] || [ "$WITH_QODER_CLI_CN" -eq 1 ]; then
    echo ""
    [ "$WITH_QODER_CLI" -eq 1 ] && echo "[OK] Qoder CLI 国际版已预装,支持 task.executor=qoder_cli"
    [ "$WITH_QODER_CLI_CN" -eq 1 ] && echo "[OK] Qoder CN CLI 国内版已预装,支持 task.executor=qoder_cli_cn"
    echo "     凭证配置:用户在「智能体配置」中填入对应 PAT 即可"
    echo "       - 国际版 PAT:qoder.com/account/integrations 生成"
    echo "       - 国内版 PAT:qoder.cn/account/integrations 生成"
fi
