"""沙箱版工具实现(阶段 2 起)

所有工具都通过 SandboxSession 执行,接口与 local_tools.py 保持一致。

mock 模式:沙箱会话的 run_command 走本地 subprocess,但 Windows 不支持
         mkdir -p / find / rg 等 Unix 命令,所以 mock 模式下直接用
         Python 实现,绕过 shell
sandbox 模式:走真实沙箱,在 Linux 容器里执行 Unix 命令
"""
import json
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


# 噪声目录:列出仓库结构时跳过(参考 Claude Code LS 的 ignore 设计)
_SKIP_DIRS_LIST = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target",
}


# ============================================================
# 工具 2:list_files(参考 Claude Code LS:单层列出,不递归)
# ============================================================


def list_files(
    repo_path: str,
    subdir: str = "",
    max_entries: int = 200,
    task_id: str = "",
) -> dict:
    """列出仓库内某目录下的文件和子目录(单层,不递归)

    参考 Claude Code 的 LS 工具设计:
    - 单层列出指定目录的内容,不递归整树(避免大仓库撑爆上下文)
    - 跳过噪声目录(.git / node_modules / __pycache__ / venv 等)
    - 区分 file / dir,便于 LLM 决定下一步进哪个子目录或读哪个文件
    - 目录排前、文件排后,各自按名字排序
    - 限制返回条数(max_entries),超出则 truncated=true

    参数:
        repo_path: clone_repo 返回的 path
        subdir: 仓库内相对路径,默认根目录。如 "src"、"tests/unit"
        max_entries: 最多返回条目数,默认 200

    返回:{
        "path": "src/",          # 本次列出的目录(相对仓库)
        "entries": [
            {"name": "main.py", "type": "file", "size": 1024},
            {"name": "utils", "type": "dir", "size": 0},
            ...
        ],
        "total": int,
        "truncated": bool,
    }
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "mock":
        return _list_files_mock(repo_path, subdir, max_entries)
    else:
        return _list_files_sandbox(ctx, repo_path, subdir, max_entries)


def _list_files_mock(repo_path: str, subdir: str, max_entries: int) -> dict:
    """mock 模式:用 Path.iterdir 直接列"""
    root = Path(repo_path).resolve()
    target = (root / subdir).resolve() if subdir else root

    # 防路径穿越
    if not target.is_relative_to(root):
        raise ValueError("非法路径:不能超出仓库根目录")
    if not target.is_dir():
        raise FileNotFoundError(f"目录不存在: {subdir or '(根)'}")

    entries = []
    for entry in target.iterdir():
        # 跳过噪声目录(只跳目录,不跳同名文件)
        if entry.is_dir() and entry.name in _SKIP_DIRS_LIST:
            continue
        if entry.is_dir():
            entries.append({"name": entry.name, "type": "dir", "size": 0})
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            entries.append({"name": entry.name, "type": "file", "size": size})

    # 排序:目录在前、文件在后;各自按名字大小写不敏感排序
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

    truncated = len(entries) > max_entries
    entries = entries[:max_entries]

    return {
        "path": (subdir.rstrip("/") + "/") if subdir else ".",
        "entries": entries,
        "total": len(entries),
        "truncated": truncated,
    }


def _list_files_sandbox(
    ctx: dict, repo_path: str, subdir: str, max_entries: int
) -> dict:
    """sandbox 模式:用 ls -Ap1 单层列出

    -A:列出除 . 和 .. 外的所有条目(含隐藏文件)
    -p:目录名末尾加 /(便于解析)
    -1:每行一个
    """
    session: SandboxSession = ctx["session"]
    full_path = (
        f"{repo_path.rstrip('/')}/{subdir.lstrip('/')}"
        if subdir else repo_path
    )

    # 检查目录是否存在
    check = session.run_command(
        f"test -d {shlex.quote(full_path)} && echo OK || echo MISSING"
    )
    if "MISSING" in check:
        raise FileNotFoundError(f"目录不存在: {subdir or '(根)'}")

    # 单层列出
    output = session.run_command(f"ls -Ap1 {shlex.quote(full_path)}")

    entries = []
    for line in output.splitlines():
        name = line.strip()
        if not name:
            continue
        is_dir = name.endswith("/")
        name = name.rstrip("/")
        if is_dir and name in _SKIP_DIRS_LIST:
            continue
        if is_dir:
            entries.append({"name": name, "type": "dir", "size": 0})
        else:
            # 不查文件大小(避免 N 次 stat,LLM 不需要精确大小)
            entries.append({"name": name, "type": "file", "size": 0})

    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

    truncated = len(entries) > max_entries
    entries = entries[:max_entries]

    return {
        "path": (subdir.rstrip("/") + "/") if subdir else ".",
        "entries": entries,
        "total": len(entries),
        "truncated": truncated,
    }


# ============================================================
# 工具 3:read_file
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
# 工具 4:search_code
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


# ============================================================
# 工具 5:run_semgrep(阶段 3)
# ============================================================


def run_semgrep(
    repo_path: str,
    config: str = "auto",
    task_id: str = "",
) -> dict:
    """在沙箱里运行 Semgrep 静态分析

    参数:
        repo_path: clone_repo 返回的 path
        config: semgrep 配置,默认 "auto"(自动选规则集)。
                也可指定 "p/python"、"p/javascript" 等

    返回:{
        "findings": [
            {
                "rule_id": "python.lang.security...",
                "severity": "HIGH",
                "file": "src/main.py",
                "line": 42,
                "message": "..."
            },
            ...
        ],
        "total": int,
        "truncated": bool
    }

    mock 模式:返回提示让 LLM 知道本工具不可用
    sandbox 模式:在沙箱里执行 semgrep
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "mock":
        # mock 模式:semgrep 需要沙箱,跳过
        return {
            "findings": [],
            "total": 0,
            "truncated": False,
            "note": (
                "mock 模式不支持 semgrep(需要 Linux 沙箱环境)。",
                "请通过其他工具(search_code + read_file)进行手动 SAST 检查,",
                "或切换 SANDBOX_MODE=sandbox 启用此工具。"
            ),
        }

    return _run_semgrep_sandbox(ctx, repo_path, config)


def _run_semgrep_sandbox(ctx: dict, repo_path: str, config: str) -> dict:
    """sandbox 模式:在沙箱里运行 semgrep"""
    session: SandboxSession = ctx["session"]

    # 先检查 semgrep 是否已安装
    check = session.run_command("which semgrep || echo MISSING")
    if "MISSING" in check:
        # 尝试 pip 安装
        logger.info("[sandbox] semgrep 未安装,尝试 pip install semgrep")
        install_result = session.run_command(
            "pip install semgrep 2>&1 | tail -5", timeout=180
        )
        # 再次检查
        check2 = session.run_command("which semgrep || echo MISSING")
        if "MISSING" in check2:
            return {
                "findings": [],
                "total": 0,
                "truncated": False,
                "error": "semgrep 安装失败,请检查沙箱镜像或手动安装",
            }

    # 运行 semgrep,输出 JSON
    # --json 输出到 stdout
    # --quiet 只输出结果,不输出 banner
    # --config auto 自动选规则
    cmd = f"semgrep --json --quiet --config {shlex.quote(config)} {shlex.quote(repo_path)}"
    logger.info(f"[sandbox] semgrep: {cmd}")
    output = session.run_command(cmd, timeout=300)  # semgrep 可能慢,5 分钟超时

    # 解析 JSON
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        return {
            "findings": [],
            "total": 0,
            "truncated": False,
            "error": f"semgrep 输出解析失败: {e}",
        }

    results = data.get("results", [])
    findings = []
    for r in results[:100]:  # 限制最多 100 个,防超长
        # 提取信息
        path = r.get("path", "")
        # 去掉 repo_path 前缀
        if path.startswith(repo_path):
            path = path[len(repo_path):].lstrip("/")

        findings.append({
            "rule_id": r.get("check_id", ""),
            "severity": _map_semgrep_severity(r.get("extra", {}).get("severity", "")),
            "file": path,
            "line": r.get("start", {}).get("line", 0),
            "message": r.get("extra", {}).get("message", "")[:200],
        })

    total = len(findings)
    return {
        "findings": findings,
        "total": total,
        "truncated": total >= 100,
    }


def _map_semgrep_severity(sev: str) -> str:
    """把 semgrep 的 severity 映射到统一格式"""
    mapping = {
        "ERROR": "HIGH",
        "WARNING": "MEDIUM",
        "INFO": "LOW",
    }
    return mapping.get(sev.upper(), sev.upper())
