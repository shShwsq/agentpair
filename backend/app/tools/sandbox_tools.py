"""沙箱版工具实现(阶段 2)

所有工具都通过 SandboxSession 执行,接口与 local_tools.py 保持一致。

mock 模式:沙箱会话的 run_command 走本地 subprocess,但 Windows 不支持
         mkdir -p / find / rg 等 Unix 命令,所以 mock 模式下直接用
         Python 实现,绕过 shell
sandbox 模式:走真实沙箱,在 Linux 容器里执行 Unix 命令
"""
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.sandbox.client import SandboxSession, create_sandbox

logger = logging.getLogger(__name__)


# 全局缓存 task_id -> (SandboxSession, repo_path, mock_local_dir)
# mock 模式下,mock_local_dir 是本地临时目录,工具用 Python 直接操作
# sandbox 模式下,repo_path 是沙箱内的路径
_sessions: dict[str, dict[str, Any]] = {}


def _get_or_create_session(task_id: str) -> dict[str, Any]:
    """获取或创建任务的沙箱上下文"""
    if task_id not in _sessions:
        session = create_sandbox()
        ctx = {"session": session, "repo_path": "", "mode": settings.SANDBOX_MODE}
        # mock 模式下额外维护一个本地临时目录
        if settings.SANDBOX_MODE == "mock":
            ctx["mock_dir"] = Path(tempfile.mkdtemp(prefix="sandbox_mock_"))
        _sessions[task_id] = ctx
    return _sessions[task_id]


def _set_repo_path(task_id: str, repo_path: str) -> None:
    if task_id in _sessions:
        _sessions[task_id]["repo_path"] = repo_path


def close_session(task_id: str) -> None:
    """任务结束后关闭沙箱,清理资源"""
    if task_id not in _sessions:
        return
    ctx = _sessions.pop(task_id)
    session: SandboxSession = ctx["session"]
    try:
        session.close()
    except Exception as e:
        logger.warning(f"[task={task_id}] 关闭沙箱失败: {e}")
    # mock 模式清理临时目录
    mock_dir = ctx.get("mock_dir")
    if mock_dir:
        shutil.rmtree(mock_dir, ignore_errors=True)


# ============================================================
# 工具 1:clone_repo
# ============================================================


def clone_repo(repo_url: str, branch: str | None = None, task_id: str = "") -> dict:
    """克隆 GitHub 仓库

    mock 模式:在本地临时目录用 git clone(SSH URL)
    sandbox 模式:在沙箱里用 git clone(SSH URL)
    """
    clone_url = _to_ssh_url(repo_url)

    # 从 URL 提取仓库名
    match = re.search(r"/([^/]+?)(?:\.git)?$", clone_url)
    if not match:
        raise ValueError(f"无法从 URL 解析仓库名: {repo_url}")
    repo_name = match.group(1)

    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "mock":
        return _clone_repo_mock(ctx, clone_url, repo_name, branch)
    else:
        return _clone_repo_sandbox(ctx, clone_url, repo_name, branch)


def _clone_repo_mock(ctx: dict, clone_url: str, repo_name: str, branch: str | None) -> dict:
    """mock 模式:本地 git clone"""
    mock_dir: Path = ctx["mock_dir"]
    repo_dir = mock_dir / repo_name

    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([clone_url, str(repo_dir)])

    logger.info(f"[mock] git clone: {clone_url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"git clone 失败: {result.stderr[:500]}")

    files_count = sum(
        1
        for _ in repo_dir.rglob("*")
        if _.is_file() and ".git" not in _.parts
    )
    # mock 模式下,path 返回本地路径(后续 read/search 工具会用 Python 直接读)
    return {"path": str(repo_dir), "files_count": files_count}


def _clone_repo_sandbox(ctx: dict, clone_url: str, repo_name: str, branch: str | None) -> dict:
    """sandbox 模式:在沙箱里 git clone"""
    session: SandboxSession = ctx["session"]
    repo_dir = f"/home/user/repos/{repo_name}"
    session.run_command(f"mkdir -p {shlex.quote(repo_dir)}")

    cmd = "git clone --depth 1"
    if branch:
        cmd += f" --branch {shlex.quote(branch)}"
    cmd += f" {shlex.quote(clone_url)} {shlex.quote(repo_dir)}"

    logger.info(f"[sandbox] git clone: {clone_url}")
    session.run_command(cmd, timeout=120)

    count_cmd = f"find {shlex.quote(repo_dir)} -type f -not -path '*/.git/*' | wc -l"
    files_count = int(session.run_command(count_cmd).strip() or "0")

    return {"path": repo_dir, "files_count": files_count}


# ============================================================
# 工具 2:read_file
# ============================================================


def read_file(repo_path: str, file_path: str, max_lines: int = 500, task_id: str = "") -> dict:
    """读取仓库内文件"""
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "mock":
        return _read_file_mock(repo_path, file_path, max_lines)
    else:
        return _read_file_sandbox(ctx, repo_path, file_path, max_lines)


def _read_file_mock(repo_path: str, file_path: str, max_lines: int) -> dict:
    """mock 模式:直接用 Python 读"""
    full_path = Path(repo_path) / file_path
    # 防路径穿越
    if not full_path.resolve().is_relative_to(Path(repo_path).resolve()):
        raise ValueError("非法路径:不能超出仓库根目录")

    if not full_path.is_file():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    try:
        content = full_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {
            "path": file_path,
            "content": "(二进制文件,无法显示)",
            "total_lines": 0,
            "truncated": False,
        }

    lines = content.splitlines()
    total_lines = len(lines)
    truncated = total_lines > max_lines
    if truncated:
        lines = lines[:max_lines]

    return {
        "path": file_path,
        "content": "\n".join(lines),
        "total_lines": total_lines,
        "truncated": truncated,
    }


def _read_file_sandbox(
    ctx: dict, repo_path: str, file_path: str, max_lines: int
) -> dict:
    """sandbox 模式:在沙箱里用 head/wc 读"""
    session: SandboxSession = ctx["session"]
    full_path = f"{repo_path.rstrip('/')}/{file_path.lstrip('/')}"

    check = session.run_command(f"test -f {shlex.quote(full_path)} && echo OK || echo MISSING")
    if "MISSING" in check:
        raise FileNotFoundError(f"文件不存在: {file_path}")

    total_lines_str = session.run_command(f"wc -l < {shlex.quote(full_path)}").strip()
    total_lines = int(total_lines_str) if total_lines_str.isdigit() else 0

    truncated = total_lines > max_lines
    content = session.run_command(f"head -n {max_lines} {shlex.quote(full_path)}")

    return {
        "path": file_path,
        "content": content,
        "total_lines": total_lines,
        "truncated": truncated,
    }


# ============================================================
# 工具 3:search_code
# ============================================================


def search_code(
    repo_path: str,
    pattern: str,
    *,
    file_glob: str | None = None,
    case_sensitive: bool = False,
    max_matches: int = 50,
    task_id: str = "",
) -> dict:
    """在仓库里搜索代码"""
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "mock":
        return _search_code_mock(repo_path, pattern, file_glob, case_sensitive, max_matches)
    else:
        return _search_code_sandbox(ctx, repo_path, pattern, file_glob, case_sensitive, max_matches)


def _search_code_mock(
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_matches: int,
) -> dict:
    """mock 模式:用 Python 实现搜索(复用阶段 1 的纯 Python grep)"""
    import fnmatch

    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)

    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    text_exts = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
        ".kt", ".scala", ".sh", ".bash", ".yaml", ".yml", ".json",
        ".xml", ".html", ".css", ".scss", ".md", ".txt", ".toml",
        ".cfg", ".ini", ".env",
    }

    matches = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in text_exts:
                continue
            if file_glob and not fnmatch.fnmatch(fname, file_glob):
                continue

            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            rel = os.path.relpath(fpath, repo_path)
                            matches.append(
                                {"file": rel, "line": i, "content": line.rstrip()}
                            )
                            if len(matches) >= max_matches:
                                return {
                                    "matches": matches,
                                    "total_matches": len(matches),
                                    "truncated": True,
                                }
            except (PermissionError, OSError):
                continue

    return {
        "matches": matches,
        "total_matches": len(matches),
        "truncated": False,
    }


def _search_code_sandbox(
    ctx: dict,
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_matches: int,
) -> dict:
    """sandbox 模式:用 ripgrep"""
    session: SandboxSession = ctx["session"]

    cmd_parts = ["rg", "--line-number", "--no-heading", "--color=never"]
    cmd_parts.extend(["--max-count", str(max_matches)])
    if not case_sensitive:
        cmd_parts.append("-i")
    if file_glob:
        cmd_parts.extend(["--glob", shlex.quote(file_glob)])
    cmd_parts.extend(["-e", shlex.quote(pattern), shlex.quote(repo_path)])

    cmd = " ".join(cmd_parts)
    logger.info(f"[sandbox] search: {cmd}")
    output = session.run_command(f"{cmd} || true")

    matches = _parse_search_output(output, repo_path)
    total = len(matches)
    truncated = total >= max_matches
    return {
        "matches": matches[:max_matches],
        "total_matches": total,
        "truncated": truncated,
    }


def _parse_search_output(output: str, repo_path: str) -> list[dict]:
    """解析 rg/grep 的输出(file:line:content)"""
    matches = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file, line_no, content = parts
        if file.startswith(repo_path):
            file = file[len(repo_path):].lstrip("/")
        matches.append({
            "file": file,
            "line": int(line_no) if line_no.isdigit() else 0,
            "content": content,
        })
    return matches


# ============================================================
# 辅助:URL 转换
# ============================================================


def _to_ssh_url(repo_url: str) -> str:
    """把 GitHub HTTPS URL 转成 SSH URL"""
    if repo_url.startswith("git@"):
        return repo_url

    m = re.match(r"^https?://github\.com/(.+?)(?:\.git)?/?$", repo_url)
    if m:
        path = m.group(1)
        return f"git@github.com:{path}.git"

    return repo_url
