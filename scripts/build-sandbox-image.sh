#!/usr/bin/env bash
# 构建 AgentPair 沙箱镜像(预装 git / ripgrep / python3 / awk / find)
#
# 默认同时预装 Qoder CLI(Node.js >= 20.0.0 + @qoder-ai/qodercli),
# 支持 Qoder CLI 执行器(task.executor=qoder_cli)。
#
# 用法:在装好 docker 的 Linux 服务器上执行
#   bash scripts/build-sandbox-image.sh                     # 默认含 Qoder CLI
#   bash scripts/build-sandbox-image.sh --no-qoder-cli      # 不装 Qoder CLI(仅基础工具)
#   bash scripts/build-sandbox-image.sh --with-qoder-cli    # 显式指定含 Qoder CLI(默认行为)
#
# 构建 agentpair-sandbox:latest 后,在 AgentPair backend/.env 设:
#   SANDBOX_IMAGE=agentpair-sandbox:latest

set -euo pipefail

IMAGE_NAME="agentpair-sandbox"
IMAGE_TAG="latest"
DOCKERFILE="Dockerfile.sandbox"

# ---------- 参数解析 ----------
WITH_QODER_CLI=1  # 默认安装 Qoder CLI
for arg in "$@"; do
    case "$arg" in
        --with-qoder-cli)
            WITH_QODER_CLI=1
            ;;
        --no-qoder-cli)
            WITH_QODER_CLI=0
            ;;
        -h|--help)
            echo "用法:bash $0 [--with-qoder-cli|--no-qoder-cli]"
            echo "  --with-qoder-cli  预装 Qoder CLI(默认)"
            echo "  --no-qoder-cli    不装 Qoder CLI(仅基础工具 git/rg/python3/awk/find)"
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

# ---------- 生成 Dockerfile(若不存在) ----------
# 若已存在但不含 Qoder CLI 段,本次又要求装 Qoder CLI,则追加(兼容旧脚本生成的文件)
NEED_REGEN=0
if [ ! -f "$DOCKERFILE" ]; then
    NEED_REGEN=1
elif [ "$WITH_QODER_CLI" -eq 1 ] && ! grep -q "qodercli" "$DOCKERFILE"; then
    echo "[INFO] $DOCKERFILE 已存在但不含 Qoder CLI,将追加 Qoder CLI 安装段"
    NEED_REGEN=2  # 已存在但需追加
fi

if [ "$NEED_REGEN" -eq 1 ]; then
    # 全新生成
    if [ "$WITH_QODER_CLI" -eq 1 ]; then
        cat > "$DOCKERFILE" <<'EOF'
FROM ubuntu:22.04

# 避免 tzdata 等交互式安装卡住
ENV DEBIAN_FRONTEND=noninteractive

# 基础工具:git / ripgrep / python3 / awk / find
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

# Node.js 20.x(Qoder CLI 要求 >= 20.0.0,见 https://docs.qoder.com/cli/install)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 Qoder CLI(官方 npm 包,见 https://docs.qoder.com/cli/install)
RUN npm install -g @qoder-ai/qodercli

# 沙箱默认非 root 用户 user,确保 home 目录存在
RUN useradd -m -s /bin/bash user
USER user
WORKDIR /home/user
EOF
    else
        cat > "$DOCKERFILE" <<'EOF'
FROM ubuntu:22.04

# 避免 tzdata 等交互式安装卡住
ENV DEBIAN_FRONTEND=noninteractive

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
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# 沙箱默认非 root 用户 user,确保 home 目录存在
RUN useradd -m -s /bin/bash user
USER user
WORKDIR /home/user
EOF
    fi
    echo "[OK] 已生成 $DOCKERFILE"
elif [ "$NEED_REGEN" -eq 2 ]; then
    # 已存在但不含 Qoder CLI,追加 Node.js + qodercli 安装段
    # 找到 "USER user" 之前的位置插入,否则 npm 装到非 root 用户会失败
    # 用 sed 在 "# 沙箱默认非 root 用户 user" 之前插入新内容
    sed -i '/^# 沙箱默认非 root 用户 user/i\
# Node.js 20.x(Qoder CLI 要求 >= 20.0.0,见 https://docs.qoder.com/cli/install)\
USER root\
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \\\
    \&\& apt-get install -y --no-install-recommends nodejs \\\
    \&\& rm -rf /var/lib/apt/lists/*\
\
# 全局安装 Qoder CLI(官方 npm 包,见 https://docs.qoder.com/cli/install)\
RUN npm install -g @qoder-ai/qodercli\
\
' "$DOCKERFILE"
    echo "[OK] 已在 $DOCKERFILE 追加 Qoder CLI 安装段"
else
    echo "[INFO] $DOCKERFILE 已存在且符合要求,直接使用(如需重新生成请先删除)"
fi

# ---------- 构建镜像 ----------
echo "[INFO] 开始构建 $IMAGE_NAME:$IMAGE_TAG ..."
if [ "$WITH_QODER_CLI" -eq 1 ]; then
    echo "       (含 Qoder CLI 执行器依赖)"
else
    echo "       (不含 Qoder CLI)"
fi
docker build -f "$DOCKERFILE" -t "$IMAGE_NAME:$IMAGE_TAG" .
echo "[OK] 镜像构建完成"

# ---------- 验证镜像内工具 ----------
echo "[INFO] 验证镜像内必要工具 ..."
MISSING=0
for cmd in git rg python3 awk find; do
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v $cmd" >/dev/null 2>&1; then
        echo "  [OK]   $cmd"
    else
        echo "  [FAIL] $cmd 缺失"
        MISSING=1
    fi
done

if [ "$WITH_QODER_CLI" -eq 1 ]; then
    echo "[INFO] 验证 Qoder CLI 执行器依赖 ..."
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

if [ "$MISSING" -ne 0 ]; then
    echo "[FAIL] 镜像缺少必要工具,请检查 $DOCKERFILE"
    exit 1
fi

# ---------- 完成 ----------
echo ""
echo "[OK] 全部就绪。在 AgentPair backend/.env 设:"
echo "    SANDBOX_IMAGE=$IMAGE_NAME:$IMAGE_TAG"
if [ "$WITH_QODER_CLI" -eq 1 ]; then
    echo ""
    echo "[OK] Qoder CLI 已预装,支持 task.executor=qoder_cli"
    echo "     凭证配置:用户在「智能体配置」中填入 Qoder PAT 即可"
fi
