"""沙箱客户端封装

使用 OpenSandbox 官方同步 API(SandboxSync / ConnectionConfigSync),
对齐 react_agent 的同步循环(基于 OpenAI SDK 同步调用),无需 asyncio 包装。

两种模式:
- sandbox:连真实 OpenSandbox Server(部署在 Linux 服务器上),走 SandboxSync
- mock:本地未部署 Server,用本地文件系统模拟,供开发期使用

对外接口(同步):
- create_sandbox() -> SandboxSession
- SandboxSession.run_command(cmd) -> str   返回 stdout
- SandboxSession.write_file(path, content)
- SandboxSession.read_file(path) -> str
- SandboxSession.close()

参考:https://github.com/alibaba/OpenSandbox
"""
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)


# ============================================================
# 沙箱会话抽象
# ============================================================


class SandboxSession:
    """沙箱会话,封装对单个沙箱实例的操作

    所有方法都是同步的:sandbox 模式走 SandboxSync 同步 API,mock 模式走本地文件系统
    """

    def __init__(self, mode: str, sandbox: Any = None, work_dir: str = "/home/user"):
        self.mode = mode
        self.sandbox = sandbox  # SandboxSync 对象(sandbox 模式)
        self.work_dir = work_dir
        # mock 模式下的本地临时目录
        self._mock_dir: Path | None = None
        if mode == "mock":
            self._mock_dir = Path(tempfile.mkdtemp(prefix="sandbox_mock_"))
            # mock 模式下 work_dir 映射到本地目录
            self.work_dir = str(self._mock_dir)
        self._closed = False

    # ---------- 通用 ----------

    def run_command(self, cmd: str, timeout: int = 60) -> str:
        """执行 shell 命令,返回 stdout

        sandbox 模式:在沙箱里执行(SandboxSync.commands.run)
        mock 模式:在本地临时目录里执行(用 subprocess)
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            return self._sandbox_run_command(cmd, timeout)
        else:
            return self._mock_run_command(cmd, timeout)

    def write_file(self, path: str, content: str) -> None:
        """写入文件"""
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            self._sandbox_write_file(path, content)
        else:
            self._mock_write_file(path, content)

    def read_file(self, path: str) -> str:
        """读取文件"""
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            return self._sandbox_read_file(path)
        else:
            return self._mock_read_file(path)

    def close(self) -> None:
        """关闭沙箱,释放资源

        sandbox 模式用 destroy()(kill + close 本地资源,避免 httpx 连接泄漏);
        destroy 不可用时回退到 kill()
        """
        if self._closed:
            return
        self._closed = True

        if self.mode == "sandbox" and self.sandbox:
            try:
                destroy = getattr(self.sandbox, "destroy", None)
                if destroy is not None:
                    destroy()
                else:
                    self.sandbox.kill()
            except Exception as e:
                logger.warning(f"关闭沙箱失败: {e}")
        elif self._mock_dir:
            # mock 模式:清理临时目录
            shutil.rmtree(self._mock_dir, ignore_errors=True)

    # ---------- sandbox 模式实现(SandboxSync 同步) ----------

    def _sandbox_run_command(self, cmd: str, timeout: int) -> str:
        """在真实沙箱里执行命令(SandboxSync.commands.run 同步调用)"""
        execution = self.sandbox.commands.run(cmd, timeout=timeout)
        # logs.stdout 是一个 list,每项有 .text
        stdout_parts = []
        for item in (execution.logs.stdout or []):
            text = getattr(item, "text", None) or str(item)
            stdout_parts.append(text)
        return "".join(stdout_parts)

    def _sandbox_write_file(self, path: str, content: str) -> None:
        """在真实沙箱里写文件"""
        from opensandbox.models.filesystem import WriteEntry

        self.sandbox.files.write_files([
            WriteEntry(path=path, data=content, mode=644)
        ])

    def _sandbox_read_file(self, path: str) -> str:
        """在真实沙箱里读文件"""
        content = self.sandbox.files.read_file(path)
        return content

    # ---------- mock 模式实现(本地文件系统) ----------

    def _mock_run_command(self, cmd: str, timeout: int) -> str:
        """mock 模式:用本地 subprocess 执行,把 work_dir 当作沙箱根"""
        assert self._mock_dir is not None
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=self._mock_dir,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"命令执行失败(code={result.returncode}): {result.stderr[:500]}"
            )
        return result.stdout

    def _mock_write_file(self, path: str, content: str) -> None:
        """mock 模式:直接在本地临时目录写文件"""
        assert self._mock_dir is not None
        # path 可能是绝对路径(/home/user/xxx)或相对路径
        # 在 mock 模式下,统一映射到 _mock_dir 下
        rel = path.lstrip("/")
        if rel.startswith("home/user/"):
            rel = rel[len("home/user/"):]
        target = self._mock_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _mock_read_file(self, path: str) -> str:
        """mock 模式:直接读本地临时目录"""
        assert self._mock_dir is not None
        rel = path.lstrip("/")
        if rel.startswith("home/user/"):
            rel = rel[len("home/user/"):]
        target = self._mock_dir / rel
        return target.read_text(encoding="utf-8")


# ============================================================
# 沙箱工厂
# ============================================================


def create_sandbox() -> SandboxSession:
    """创建一个沙箱会话

    根据 settings.SANDBOX_MODE 决定走真实沙箱还是 mock 模式
    """
    mode = settings.SANDBOX_MODE

    if mode == "mock":
        logger.info("[sandbox] 使用 mock 模式(本地文件系统模拟)")
        return SandboxSession(mode="mock")
    elif mode == "sandbox":
        return _create_real_sandbox()
    else:
        raise ValueError(f"未知 SANDBOX_MODE: {mode}")


def _parse_domain(server_url: str) -> str:
    """从 SANDBOX_SERVER_URL 提取 SDK 需要的 domain(host:port,无 scheme)

    SDK 的 ConnectionConfig.domain 接受 "host:port" 形式(无 http:// 前缀)
    """
    if "://" in server_url:
        parsed = urlparse(server_url)
        return parsed.netloc
    return server_url.lstrip("/")


def _build_volumes() -> list[Any]:
    """根据配置构建 SSH key 挂载卷(可选)

    沙箱默认用户是 user,把宿主机 SSH 目录只读挂载到 /home/user/.ssh,
    供 git clone git@github.com:... 使用。需在 server [storage].allowed_host_paths 放行。

    注意:SANDBOX_SSH_KEY_HOST_PATH 是 Server 宿主机上的路径(跨机部署时后端
    无法也不应本地验证),必须是绝对路径,不要用 ~。
    """
    if not settings.SANDBOX_SSH_KEY_HOST_PATH:
        return []
    from opensandbox.models.sandboxes import Host, Volume

    host_path = settings.SANDBOX_SSH_KEY_HOST_PATH
    return [
        Volume(
            name="ssh-keys",
            host=Host(path=host_path),
            mountPath="/home/user/.ssh",
            readOnly=True,
        )
    ]


def _build_resource() -> dict[str, str] | None:
    """根据配置构建资源限制(可选)"""
    resource: dict[str, str] = {}
    if settings.SANDBOX_CPU:
        resource["cpu"] = settings.SANDBOX_CPU
    if settings.SANDBOX_MEMORY:
        resource["memory"] = settings.SANDBOX_MEMORY
    return resource or None


def _create_real_sandbox() -> SandboxSession:
    """创建真实沙箱(同步)

    使用官方 SandboxSync 同步 API,无需 asyncio 包装。
    必须显式传 ConnectionConfigSync(domain + api_key),否则 SDK 只会连 localhost:8080。
    """
    from datetime import timedelta

    from opensandbox import SandboxSync
    from opensandbox.config import ConnectionConfigSync

    domain = _parse_domain(settings.SANDBOX_SERVER_URL)
    config = ConnectionConfigSync(
        domain=domain,
        api_key=settings.SANDBOX_API_KEY or None,
        # 跨机部署:走 Server 代理,后端只连 8080,无需放行容器端口范围
        use_server_proxy=settings.SANDBOX_USE_SERVER_PROXY,
        # 创建沙箱涉及拉镜像/启容器/等 healthy,首次尤慢,HTTP 请求超时给足 5 分钟
        request_timeout=timedelta(minutes=5),
    )
    volumes = _build_volumes()
    resource = _build_resource()

    kwargs: dict[str, Any] = {
        "image": settings.SANDBOX_IMAGE,
        "connection_config": config,
        "timeout": timedelta(minutes=settings.SANDBOX_TIMEOUT_MINUTES),
    }
    if volumes:
        kwargs["volumes"] = volumes
    if resource:
        kwargs["resource"] = resource
    sandbox = SandboxSync.create(**kwargs)
    return SandboxSession(mode="sandbox", sandbox=sandbox)
