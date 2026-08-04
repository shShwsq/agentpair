"""沙箱客户端封装

OpenSandbox 的 Python SDK 是 async/await 风格,但我们的 react_agent 是同步循环
(基于 OpenAI SDK 同步调用)。本模块对外提供同步接口,内部用 asyncio 跑异步调用。

两种模式:
- sandbox:连真实 OpenSandbox Server(部署在 Linux 服务器上)
- mock:本地未部署 Server,用本地文件系统模拟,供开发期使用

对外接口(同步):
- create_sandbox() -> SandboxSession
- SandboxSession.run_command(cmd) -> str   返回 stdout
- SandboxSession.write_file(path, content)
- SandboxSession.read_file(path) -> str
- SandboxSession.close()

参考:https://github.com/alibaba/OpenSandbox
"""
import asyncio
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

    所有方法都是同步的,内部异步部分由 _run_async 调度
    """

    def __init__(self, mode: str, sandbox: Any = None, work_dir: str = "/home/user"):
        self.mode = mode
        self.sandbox = sandbox  # OpenSandbox Sandbox 对象(sandbox 模式)
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

        sandbox 模式:在沙箱里执行
        mock 模式:在本地临时目录里执行(用 subprocess)
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            return self._run_async(self._sandbox_run_command(cmd, timeout))
        else:
            return self._mock_run_command(cmd, timeout)

    def write_file(self, path: str, content: str) -> None:
        """写入文件"""
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            self._run_async(self._sandbox_write_file(path, content))
        else:
            self._mock_write_file(path, content)

    def read_file(self, path: str) -> str:
        """读取文件"""
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            return self._run_async(self._sandbox_read_file(path))
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
                    self._run_async(destroy())
                else:
                    self._run_async(self.sandbox.kill())
            except Exception as e:
                logger.warning(f"关闭沙箱失败: {e}")
        elif self._mock_dir:
            # mock 模式:清理临时目录
            shutil.rmtree(self._mock_dir, ignore_errors=True)

    # ---------- sandbox 模式实现(异步) ----------

    async def _sandbox_run_command(self, cmd: str, timeout: int) -> str:
        """在真实沙箱里执行命令"""
        execution = await self.sandbox.commands.run(cmd, timeout=timeout)
        # logs.stdout 是一个 list,每项有 .text
        stdout_parts = []
        for item in (execution.logs.stdout or []):
            text = getattr(item, "text", None) or str(item)
            stdout_parts.append(text)
        return "".join(stdout_parts)

    async def _sandbox_write_file(self, path: str, content: str) -> None:
        """在真实沙箱里写文件"""
        from opensandbox.models import WriteEntry

        await self.sandbox.files.write_files([
            WriteEntry(path=path, data=content, mode=644)
        ])

    async def _sandbox_read_file(self, path: str) -> str:
        """在真实沙箱里读文件"""
        content = await self.sandbox.files.read_file(path)
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

    # ---------- 异步调度 ----------

    def _run_async(self, coro):
        """在同步上下文里跑异步协程

        FastAPI 路由虽然是 async,但 react_agent 内部循环是同步的(OpenAI SDK 同步调用),
        所以这里用一个独立的事件循环跑沙箱调用

        注意:如果当前已有运行中的事件循环(比如在 async 函数里调用),
        asyncio.run 会报错。这种情况用 asyncio.run_coroutine_threadsafe 兜底
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的循环,直接用 asyncio.run
            return asyncio.run(coro)
        # 有运行中的循环(在 async 上下文里),用新线程跑
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


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
    """
    if not settings.SANDBOX_SSH_KEY_HOST_PATH:
        return []
    from opensandbox.models.sandboxes import Host, Volume

    host_path = os.path.expanduser(settings.SANDBOX_SSH_KEY_HOST_PATH)
    if not os.path.isdir(host_path):
        logger.warning(
            f"[sandbox] SANDBOX_SSH_KEY_HOST_PATH 不存在或不是目录,跳过挂载: {host_path}"
        )
        return []
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
    """创建真实沙箱

    OpenSandbox SDK 是 async/await 风格,这里用 asyncio.run 启动。
    必须显式传 ConnectionConfig(domain + api_key),否则 SDK 只会连 localhost:8080。
    """
    from datetime import timedelta

    from opensandbox import Sandbox
    from opensandbox.config import ConnectionConfig

    domain = _parse_domain(settings.SANDBOX_SERVER_URL)
    config = ConnectionConfig(
        domain=domain,
        api_key=settings.SANDBOX_API_KEY or None,
        request_timeout=timedelta(seconds=30),
    )
    volumes = _build_volumes()
    resource = _build_resource()

    async def _create():
        kwargs: dict[str, Any] = {
            "image": settings.SANDBOX_IMAGE,
            "connection_config": config,
            "timeout": timedelta(minutes=settings.SANDBOX_TIMEOUT_MINUTES),
        }
        if volumes:
            kwargs["volumes"] = volumes
        if resource:
            kwargs["resource"] = resource
        sandbox = await Sandbox.create(**kwargs)
        return sandbox

    sandbox = SandboxSession(mode="sandbox")._run_async(_create())
    session = SandboxSession(mode="sandbox", sandbox=sandbox)
    return session
