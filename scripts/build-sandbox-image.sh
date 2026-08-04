#!/usr/bin/env bash
# 构建 AgentPair 沙箱镜像(预装 git / ripgrep / python3 / awk / find)
#
# 用法:在装好 docker 的 Linux 服务器上执行
#   bash scripts/build-sandbox-image.sh
#
# 构建 agentpair-sandbox:latest 后,在 AgentPair backend/.env 设:
#   SANDBOX_IMAGE=agentpair-sandbox:latest

set -euo pipefail

IMAGE_NAME="agentpair-sandbox"
IMAGE_TAG="latest"
DOCKERFILE="Dockerfile.sandbox"

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
if [ ! -f "$DOCKERFILE" ]; then
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
    echo "[OK] 已生成 $DOCKERFILE"
else
    echo "[INFO] $DOCKERFILE 已存在,直接使用(如需重新生成请先删除)"
fi

# ---------- 构建镜像 ----------
echo "[INFO] 开始构建 $IMAGE_NAME:$IMAGE_TAG ..."
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

if [ "$MISSING" -ne 0 ]; then
    echo "[FAIL] 镜像缺少必要工具,请检查 $DOCKERFILE"
    exit 1
fi

# ---------- 完成 ----------
echo ""
echo "[OK] 全部就绪。在 AgentPair backend/.env 设:"
echo "    SANDBOX_IMAGE=$IMAGE_NAME:$IMAGE_TAG"
