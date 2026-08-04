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
import threading
import uuid
from datetime import timedelta
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
        # mock 模式:后台进程跟踪 {execution_id: (Popen, [stdout_lines])}
        self._mock_bg_procs: dict[str, tuple[subprocess.Popen, list[str]]] = {}

    # ---------- 通用 ----------

    def run_command(self, cmd: str, timeout: int = 60, check: bool = False) -> str:
        """执行 shell 命令,返回 stdout

        sandbox 模式:在沙箱里执行(SandboxSync.commands.run)
        mock 模式:在本地临时目录里执行(用 subprocess)

        check=True 时,退出码非零抛 RuntimeError(含 stderr),用于 git clone 等必须成功的命令。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            return self._sandbox_run_command(cmd, timeout, check=check)
        else:
            return self._mock_run_command(cmd, timeout, check=check)

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

    def get_endpoint(self, port: int) -> tuple[str, dict[str, str]]:
        """获取沙箱内端口的外部访问端点(端口转发)

        sandbox 模式:通过 SDK 的 get_endpoint(port) 获取转发 URL + 必需 headers
        mock 模式:返回 localhost:port(本地调试用)

        返回 (endpoint_url, headers):
            - endpoint_url:可直接 HTTP 请求的完整 URL(含 scheme)
            - headers:请求时必须携带的 headers(server proxy 路由/鉴权用)
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            ep = self.sandbox.get_endpoint(port)
            return ep.endpoint, dict(ep.headers or {})
        else:
            # mock 模式:直接用 localhost
            return f"http://127.0.0.1:{port}", {}

    def run_command_background(
        self,
        cmd: str,
        envs: dict[str, str] | None = None,
        work_dir: str | None = None,
    ) -> str:
        """后台启动命令(非阻塞),返回 execution_id 供后续查询日志/中断

        用于启动 ACP bridge 等长驻服务。命令在沙箱内 detached 运行,
        本方法立即返回,不等待命令结束。

        envs: 注入命令进程的环境变量(如 QODER_PERSONAL_ACCESS_TOKEN)
        work_dir: 工作目录(沙箱内绝对路径)
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            return self._sandbox_run_background(cmd, envs, work_dir)
        else:
            return self._mock_run_background(cmd, envs, work_dir)

    def get_background_logs(self, execution_id: str, cursor: int | None = None) -> tuple[str, int | None]:
        """获取后台命令的累积日志

        返回 (logs_text, next_cursor)。next_cursor 为 None 表示无更多日志。
        mock 模式返回 (stdout_so_far, None)。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            logs = self.sandbox.commands.get_background_command_logs(
                execution_id, cursor=cursor
            )
            return logs.content, logs.cursor
        else:
            return self._mock_get_background_logs(execution_id)

    def interrupt_command(self, execution_id: str) -> None:
        """中断后台命令"""
        if self._closed:
            return

        if self.mode == "sandbox":
            self.sandbox.commands.interrupt(execution_id)
        else:
            proc = self._mock_bg_procs.pop(execution_id, None)
            if proc:
                proc.terminate()

    def close(self) -> None:
        """关闭沙箱,释放资源

        sandbox 模式用 destroy()(kill + close 本地资源,避免 httpx 连接泄漏);
        destroy 不可用时回退到 kill()。
        mock 模式清理临时目录 + 终止后台进程。
        """
        if self._closed:
            return
        self._closed = True

        # mock 模式:终止所有后台进程
        if self._mock_bg_procs:
            for proc in self._mock_bg_procs.values():
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._mock_bg_procs.clear()

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

    def _sandbox_run_command(self, cmd: str, timeout: int, *, check: bool = False) -> str:
        """在真实沙箱里执行命令(SandboxSync.commands.run 同步调用)

        SDK 的 run() 不接受 timeout kwarg,超时通过 RunCommandOpts(timeout=timedelta) 传入。
        check=True 时,退出码非零抛 RuntimeError(含 stderr)。
        """
        from opensandbox.models.execd import RunCommandOpts

        opts = RunCommandOpts(timeout=timedelta(seconds=timeout))
        execution = self.sandbox.commands.run(cmd, opts=opts)

        # SDK 的 Execution.text 属性已按 \n 正确拼接 stdout(每条 OutputMessage 是一行)
        stdout = execution.text or ""
        if check:
            exit_code = getattr(execution, "exit_code", None)
            if exit_code not in (None, 0):
                stderr = "\n".join(
                    msg.text.rstrip("\n") for msg in (execution.logs.stderr or [])
                )
                raise RuntimeError(
                    f"命令退出码 {exit_code}: {cmd}\nstdout: {stdout}\nstderr: {stderr}"
                )
        return stdout

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

    def _sandbox_run_background(
        self,
        cmd: str,
        envs: dict[str, str] | None,
        work_dir: str | None,
    ) -> str:
        """sandbox 模式:后台启动命令(SandboxSync.commands.run + background=True)

        返回 execution_id(str),供 get_background_logs / interrupt_command 使用。
        """
        from opensandbox.models.execd import RunCommandOpts

        opts = RunCommandOpts(
            background=True,
            working_directory=work_dir,
            envs=envs,
        )
        execution = self.sandbox.commands.run(cmd, opts=opts)
        if not execution.id:
            raise RuntimeError(f"后台命令启动失败,无 execution_id: {cmd}")
        logger.info(f"[sandbox] 后台命令已启动: execution_id={execution.id}, cmd={cmd[:100]}")
        return execution.id

    # ---------- mock 模式实现(本地文件系统) ----------

    def _mock_run_command(self, cmd: str, timeout: int, *, check: bool = False) -> str:
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
        if check and result.returncode != 0:
            raise RuntimeError(
                f"命令退出码 {result.returncode}: {cmd}\nstdout: {result.stdout}\nstderr: {result.stderr[:500]}"
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

    def _mock_run_background(
        self,
        cmd: str,
        envs: dict[str, str] | None,
        work_dir: str | None,
    ) -> str:
        """mock 模式:用 subprocess.Popen 后台启动,跟踪进程"""
        assert self._mock_dir is not None
        exec_id = f"mock_bg_{uuid.uuid4().hex[:8]}"
        merged_env = {**os.environ, **(envs or {})}
        cwd = work_dir or str(self._mock_dir)
        # mock 模式下 work_dir 可能是 /home/user/xxx,映射到本地
        if cwd.startswith("/home/user"):
            cwd = str(self._mock_dir / cwd[len("/home/user/"):])
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd if Path(cwd).exists() else str(self._mock_dir),
            env=merged_env,
        )
        self._mock_bg_procs[exec_id] = (proc, [])
        # 启动后台线程持续读取 stdout(避免 pipe 满死锁)
        def _drain():
            try:
                for line in proc.stdout:
                    self._mock_bg_procs[exec_id][1].append(line)
            except Exception:
                pass
        threading.Thread(target=_drain, daemon=True).start()
        return exec_id

    def _mock_get_background_logs(self, execution_id: str) -> tuple[str, int | None]:
        """mock 模式:返回已累积的 stdout 行"""
        entry = self._mock_bg_procs.get(execution_id)
        if entry is None:
            return "", None
        _proc, lines = entry
        return "".join(lines), None


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
