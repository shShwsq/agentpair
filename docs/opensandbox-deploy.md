# OpenSandbox 部署指南(Linux 服务器)

本文档指导如何在 Linux 服务器上部署 OpenSandbox Server,并让 AgentPair 后端连接到它。

## 前置条件

- **Linux 服务器**(Ubuntu 20.04+ / CentOS 7+ / Debian 11+ 推荐)
- **Docker** 已安装并运行(`docker --version` 能输出版本号)
- **Python 3.10+**(`python3 --version`)
- **pip / uv** 任一即可
- **服务器对外开放端口 8080**(或你自定义的端口),供后端连接

## 一、安装 OpenSandbox Server

OpenSandbox 提供两种安装方式,推荐用 `uvx` 方式(最简单):

### 1.1 安装 uv(若已装可跳过)

```bash
# 一键安装 uv(Python 包管理器,OpenSandbox 文档用它)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv --version  # 验证
```

### 1.2 启动 Server

```bash
# 初始化配置文件
uvx opensandbox-server init-config ~/.sandbox.toml --example docker

# 启动 Server(前台运行,看日志)
uvx opensandbox-server

# 或后台运行 + 日志
nohup uvx opensandbox-server > ~/opensandbox.log 2>&1 &

# 验证:返回 JSON 即成功
curl http://localhost:8080/health
```

启动后,Server 监听 `http://0.0.0.0:8080`。

### 1.3 配置 systemd(可选,生产推荐)

让服务开机自启 + 崩溃自动重启:

```bash
sudo tee /etc/systemd/system/opensandbox.service > /dev/null <<EOF
[Unit]
Description=OpenSandbox Server
After=docker.service network.target
Requires=docker.service

[Service]
Type=simple
User=$(whoami)
ExecStart=$(which uvx) opensandbox-server
Restart=on-failure
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now opensandbox
sudo systemctl status opensandbox
```

## 二、配置 SSH Key(给沙箱用)

沙箱里执行 `git clone git@github.com:...` 需要 SSH 凭证。否则 clone 会失败。

### 2.1 生成专用 SSH Key

```bash
ssh-keygen -t ed25519 -C "opensandbox@your-server" -f ~/.ssh/id_ed25519_opensandbox -N ""
cat ~/.ssh/id_ed25519_opensandbox.pub
```

### 2.2 添加到 GitHub

- 打开 https://github.com/settings/keys
- 点 "New SSH key",把上一步输出的公钥粘贴进去
- Title 随意,比如 `OpenSandbox Server`

### 2.3 配置 SSH 自动用这个 key

```bash
cat >> ~/.ssh/config <<EOF

Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_opensandbox
    StrictHostKeyChecking no
EOF

chmod 600 ~/.ssh/config

# 测试
ssh -T git@github.com
# 看到 "Hi xxx! You've successfully authenticated" 即成功
```

### 2.4 让沙箱能读到 SSH Key

OpenSandbox 的沙箱默认是非 root 用户执行。需要把 SSH key 挂载或复制到容器可访问的位置。最简单做法:**在 server 的 config 里配置 volume 挂载**。

编辑 `~/.sandbox.toml`,找到 `[docker]` 段(若没有则加上):

```toml
[docker]
# 把宿主机的 SSH key 和 git 配置挂载到沙箱
volumes = [
    "/home/youruser/.ssh:/home/user/.ssh:ro",
]
```

把 `youruser` 换成你的实际用户名。

重启 Server:

```bash
sudo systemctl restart opensandbox
# 或 nohup 方式:kill 后重新启动
```

## 三、后端连接配置

在你的开发机或部署后端的服务器上,修改 AgentPair 的 `backend/.env`:

```bash
# 切换到真实沙箱模式
SANDBOX_MODE=sandbox

# Linux 服务器地址(若 Server 部署在远程)
SANDBOX_SERVER_URL=http://your-server-ip:8080

# API Key(若 Server 开启鉴权则填,否则留空)
SANDBOX_API_KEY=

# 沙箱镜像(默认官方 code-interpreter,预装 Python runtime)
SANDBOX_IMAGE=opensandbox/code-interpreter:v1.0.2

# 沙箱超时
SANDBOX_TIMEOUT_MINUTES=30
```

重启后端:

```bash
uvicorn app.main:app --reload
```

## 四、验证

提交一个审计任务,看日志里是否出现 `[sandbox] git clone` 而不是 `[mock] git clone`:

```bash
# 后端日志里应该看到
[sandbox] git clone: git@github.com:xxx/xxx.git
[sandbox] search: rg --line-number ...
```

如果看到 `[mock]`,说明 `SANDBOX_MODE` 没切到 `sandbox`。

## 五、常见问题

### 5.1 沙箱里 git clone 失败:Permission denied (publickey)

**原因**:沙箱没读到 SSH key,或 key 没添加到 GitHub。

**排查**:
1. 在后端日志看 clone 失败的 stderr
2. 临时把 `SANDBOX_MODE=sandbox` 改成在沙箱里跑 `ssh -T git@github.com` 验证

### 5.2 沙箱里 rg 命令不存在

**原因**:官方 code-interpreter 镜像可能没装 ripgrep。

**解决**:要么用 grep fallback(改 sandbox_tools.py 的搜索实现),要么自定义镜像。

自定义镜像方式:在仓库根目录写 `Dockerfile.sandbox`:

```dockerfile
FROM opensandbox/code-interpreter:v1.0.2
RUN apt-get update && apt-get install -y ripgrep && rm -rf /var/lib/apt/lists/*
```

构建并推到你的镜像仓库,然后改 `.env` 的 `SANDBOX_IMAGE`。

### 5.3 沙箱创建失败:image pull 超时

官方镜像在国内拉取可能慢。可以:
- 配置 Docker 镜像加速器(阿里云 ACR 等)
- 或预先 `docker pull opensandbox/code-interpreter:v1.0.2`

### 5.4 沙箱执行命令超时

`SANDBOX_TIMEOUT_MINUTES` 是整个沙箱的生命周期超时,单个命令超时在 `sandbox_tools.py` 里写死(120s for clone, 60s for others)。如有需要调整。

### 5.5 沙箱内存/CPU 不够

在 `~/.sandbox.toml` 配置资源限制:

```toml
[docker]
memory = "2g"
cpus = "2.0"
```

## 六、生产环境注意事项

1. **API Key 鉴权**:生产环境一定要给 Server 加鉴权,否则任何人都能创建沙箱。在 `~/.sandbox.toml` 配置 `[auth] api_key = "xxx"`,然后 `SANDBOX_API_KEY=xxx`
2. **网络隔离**:Server 端口只对后端服务开放,不要暴露到公网
3. **资源配额**:限制单用户/单任务的沙箱数量,防止恶意消耗
4. **日志留存**:Server 日志要收集,便于排查沙箱执行问题
5. **定期清理**:沙箱意外退出可能留下 dangling 容器,定期 `docker container prune`

## 参考链接

- 官方仓库:https://github.com/alibaba/OpenSandbox
- SDK 文档:见仓库 `sdks/` 目录
- CLI 工具:`pip install opensandbox-cli`,命令 `osb`
