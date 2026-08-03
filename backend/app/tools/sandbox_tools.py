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


# 全局缓存 task_id -> (SandboxSession, repo_path, mock_local_dir, completed_at)
# mock 模式下,mock_local_dir 是本地临时目录,工具用 Python 直接操作
# sandbox 模式下,repo_path 是沙箱内的路径
# completed_at: 任务完成时间(用于延迟清理,任务结束后保留 session 供前端浏览工作区)
_sessions: dict[str, dict[str, Any]] = {}

# 任务完成后保留 session 的时间(秒),超时后自动清理
_SESSION_TTL_AFTER_COMPLETE = 3600  # 1 小时


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


def mark_task_completed(task_id: str) -> None:
    """标记任务完成(不关闭 session,延迟清理供前端浏览工作区)

    orchestrator 在任务结束后调用此方法而非 close_session,
    保留 session 让用户能在前端查看工作区文件结构。
    实际清理由 cleanup_expired_sessions() 在后续请求中惰性触发。
    """
    if task_id in _sessions:
        _sessions[task_id]["completed_at"] = time.time()


def cleanup_expired_sessions() -> int:
    """清理过期的已完成 session(TTL 超时)

    在 workspace 路由每次访问时调用,惰性清理。
    返回清理的 session 数。
    """
    now = time.time()
    expired = [
        tid for tid, ctx in _sessions.items()
        if ctx.get("completed_at") and now - ctx["completed_at"] > _SESSION_TTL_AFTER_COMPLETE
    ]
    for tid in expired:
        close_session(tid)
    return len(expired)


def close_session(task_id: str) -> None:
    """关闭沙箱,清理资源"""
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


def get_workspace_info(task_id: str) -> dict[str, Any] | None:
    """获取任务的工作区信息(供前端浏览)

    返回 None 表示 session 不存在(任务未执行 clone 或已清理)。
    返回 dict: { repo_path, mode, completed }
    """
    ctx = _sessions.get(task_id)
    if ctx is None:
        return None
    return {
        "repo_path": ctx.get("repo_path", ""),
        "mode": ctx.get("mode", ""),
        "completed": "completed_at" in ctx,
    }


def browse_files(task_id: str, subdir: str = "") -> dict:
    """面向前端的文件列表(复用 list_files 逻辑)

    与 list_files 工具的区别:
    - 不需要传 repo_path(从 _sessions 取)
    - task_id 必填(前端按任务浏览)
    - 返回结构一致,前端可直接渲染树
    """
    ctx = _sessions.get(task_id)
    if ctx is None:
        raise RuntimeError("工作区不可用:任务未 clone 仓库或会话已过期清理")

    repo_path = ctx.get("repo_path", "")
    if not repo_path:
        raise RuntimeError("工作区不可用:尚未 clone 仓库")

    # 复用 list_files 的实现(mock / sandbox 分支)
    mode = ctx["mode"]
    if mode == "mock":
        return _list_files_mock(repo_path, subdir, 500)
    else:
        return _list_files_sandbox(ctx, repo_path, subdir, 500)


def browse_read_file(task_id: str, file_path: str, offset: int = 1, max_lines: int = 500) -> dict:
    """面向前端的文件读取(复用 read_file 逻辑,但不带行号)

    默认读 500 行(比 LLM 工具的 200 行多,前端查看用)。
    与 read_file 工具的区别:content 返回原始文本(不带行号前缀),
    因为前端 WorkspaceSidebar 会自己渲染行号列(start_line + i),
    若后端再带行号会造成两列行号重复。
    """
    ctx = _sessions.get(task_id)
    if ctx is None:
        raise RuntimeError("工作区不可用:任务未 clone 仓库或会话已过期清理")

    repo_path = ctx.get("repo_path", "")
    if not repo_path:
        raise RuntimeError("工作区不可用:尚未 clone 仓库")

    mode = ctx["mode"]
    if mode == "mock":
        return _read_file_mock(repo_path, file_path, max_lines, offset, with_line_numbers=False)
    else:
        return _read_file_sandbox(ctx, repo_path, file_path, max_lines, offset, with_line_numbers=False)


# ============================================================
# 工具 1:clone_repo
# ============================================================


def clone_repo(repo_url: str, branch: str | None = None, task_id: str = "", github_token: str = "") -> dict:
    """克隆 GitHub 仓库(LLM 工具入口)

    内部委托给 clone_repo_with_fallback,复用同一套协议回退逻辑:
    HTTPS+token → SSH → HTTPS 匿名。

    github_token 由 execute_tool 从 ContextVar 注入,LLM 不可见。
    """
    return clone_repo_with_fallback(repo_url, branch, task_id, github_token)


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
# 工具 3:read_file(参考 Claude Code / TRAE Read:带行号 + offset 分页)
# ============================================================


def read_file(
    repo_path: str,
    file_path: str,
    max_lines: int = 200,
    offset: int = 1,
    task_id: str = "",
) -> dict:
    """读取仓库内文件内容(带行号,支持分页)

    参考 Claude Code / TRAE Read 工具设计:
    - 返回内容带行号(cat -n 格式),便于 LLM 精确定位行号
    - 支持 offset 从第 N 行开始读,配合 max_lines 翻页,避免大文件一次性撑爆上下文
    - 默认读前 200 行;需要看后面时调 offset=N 再读

    参数:
        repo_path: clone_repo 返回的 path
        file_path: 仓库内相对路径
        max_lines: 本次最多返回行数,默认 200
        offset: 从第几行开始读(1-based),默认 1

    返回:{
        "path": str,           # 文件相对路径
        "content": str,        # 带行号的内容(cat -n 格式)
        "start_line": int,     # 本次返回的起始行号
        "end_line": int,       # 本次返回的结束行号
        "total_lines": int,    # 文件总行数
        "truncated": bool      # 是否还有更多未读(本次未读到文件尾)
    }
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "mock":
        return _read_file_mock(repo_path, file_path, max_lines, offset)
    else:
        return _read_file_sandbox(ctx, repo_path, file_path, max_lines, offset)


def _format_numbered_lines(lines: list[str], start_line: int) -> str:
    """把行列表格式化成 cat -n 风格的字符串(行号右对齐 + 冒号)"""
    width = len(str(start_line + len(lines) - 1))
    width = max(width, 4)  # 至少 4 位,视觉对齐
    return "\n".join(
        f"{str(i):>{width}}: {line}"
        for i, line in enumerate(lines, start=start_line)
    )


def _read_file_mock(
    repo_path: str, file_path: str, max_lines: int, offset: int,
    with_line_numbers: bool = True,
) -> dict:
    """mock 模式:直接用 Python 读

    with_line_numbers:
        True(LLM 工具 read_file):content 带 cat -n 风格行号前缀
        False(前端 browse_read_file):content 为原始文本,前端自行渲染行号列
    """
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
            "start_line": 0,
            "end_line": 0,
            "total_lines": 0,
            "truncated": False,
        }

    all_lines = content.splitlines()
    total_lines = len(all_lines)

    # offset 是 1-based,转 0-based 切片
    start_idx = max(0, min(offset - 1, total_lines))
    end_idx = min(start_idx + max_lines, total_lines)
    selected = all_lines[start_idx:end_idx]

    start_line = start_idx + 1
    end_line = start_idx + len(selected)

    if with_line_numbers:
        body = _format_numbered_lines(selected, start_line)
    else:
        body = "\n".join(selected)

    return {
        "path": file_path,
        "content": body,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "truncated": end_line < total_lines,
    }


def _read_file_sandbox(
    ctx: dict, repo_path: str, file_path: str, max_lines: int, offset: int,
    with_line_numbers: bool = True,
) -> dict:
    """sandbox 模式:在沙箱里用 awk 读(带行号 + 范围)

    with_line_numbers:
        True(LLM 工具 read_file):content 带 cat -n 风格行号前缀
        False(前端 browse_read_file):content 为原始文本,前端自行渲染行号列
    """
    session: SandboxSession = ctx["session"]
    full_path = f"{repo_path.rstrip('/')}/{file_path.lstrip('/')}"

    check = session.run_command(f"test -f {shlex.quote(full_path)} && echo OK || echo MISSING")
    if "MISSING" in check:
        raise FileNotFoundError(f"文件不存在: {file_path}")

    total_lines_str = session.run_command(f"wc -l < {shlex.quote(full_path)}").strip()
    total_lines = int(total_lines_str) if total_lines_str.isdigit() else 0

    # 用 awk 一次性完成:行号格式化 + 范围截取
    start = max(1, offset)
    end = start + max_lines - 1
    if with_line_numbers:
        awk_script = (
            f"NR>={start} && NR<={end} "
            f"{{printf \"%6d: %s\\n\", NR, $0}}"
        )
    else:
        awk_script = (
            f"NR>={start} && NR<={end} "
            f"{{printf \"%s\\n\", $0}}"
        )
    content = session.run_command(
        f"awk '{awk_script}' {shlex.quote(full_path)}"
    )

    start_line = min(start, total_lines) if total_lines > 0 else 0
    end_line = min(end, total_lines) if total_lines > 0 else 0

    return {
        "path": file_path,
        "content": content,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "truncated": end_line < total_lines,
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
    context_lines: int = 0,
    output_mode: str = "content",
    offset: int = 0,
    task_id: str = "",
) -> dict:
    """在仓库里搜索代码(支持上下文、多种输出模式、分页)

    参考 TRAE Grep 工具设计:
    - output_mode:
        - "content"(默认):返回匹配行 + 行号 + 上下文
        - "files_with_matches":只返回含匹配的文件路径(快速定位)
        - "count":返回每个文件的匹配数
    - context_lines:匹配行前后各显示 N 行(仅 content 模式有效),
        安全审计场景建议设 3-5,便于理解漏洞上下文
    - offset:分页偏移,跳过前 N 个匹配

    返回(content):{"matches": [{file,line,content,context_before,context_after}], "total_matches", "truncated", "offset"}
    返回(files_with_matches):{"files": [...], "total_files", "truncated", "offset"}
    返回(count):{"counts": {file: count}, "total_matches"}
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "mock":
        return _search_code_mock(
            repo_path, pattern, file_glob, case_sensitive,
            max_matches, context_lines, output_mode, offset,
        )
    else:
        return _search_code_sandbox(
            ctx, repo_path, pattern, file_glob, case_sensitive,
            max_matches, context_lines, output_mode, offset,
        )


def _search_code_mock(
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_matches: int,
    context_lines: int,
    output_mode: str,
    offset: int,
) -> dict:
    """mock 模式:用 Python 实现搜索"""
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

    need_context = output_mode == "content" and context_lines > 0
    all_matches: list[dict] = []

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
                    lines = f.readlines()
            except (PermissionError, OSError):
                continue

            rel = os.path.relpath(fpath, repo_path)
            for i, line in enumerate(lines):
                if regex.search(line):
                    m = {"file": rel, "line": i + 1, "content": line.rstrip()}
                    if need_context:
                        start = max(0, i - context_lines)
                        end = i + 1 + context_lines
                        m["context_before"] = [l.rstrip() for l in lines[start:i]]
                        m["context_after"] = [l.rstrip() for l in lines[i + 1:end]]
                    all_matches.append(m)

    if output_mode == "count":
        counts: dict[str, int] = {}
        for m in all_matches:
            counts[m["file"]] = counts.get(m["file"], 0) + 1
        return {"counts": counts, "total_matches": len(all_matches)}

    if output_mode == "files_with_matches":
        files = sorted(set(m["file"] for m in all_matches))
        total = len(files)
        page = files[offset:offset + max_matches]
        return {
            "files": page,
            "total_files": total,
            "truncated": offset + len(page) < total,
            "offset": offset,
        }

    # output_mode == "content"
    total = len(all_matches)
    page = all_matches[offset:offset + max_matches]
    for m in page:
        m.setdefault("context_before", [])
        m.setdefault("context_after", [])
    return {
        "matches": page,
        "total_matches": total,
        "truncated": offset + len(page) < total,
        "offset": offset,
    }


def _search_code_sandbox(
    ctx: dict,
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_matches: int,
    context_lines: int,
    output_mode: str,
    offset: int,
) -> dict:
    """sandbox 模式:用 ripgrep"""
    session: SandboxSession = ctx["session"]

    # ---- files_with_matches 模式:只返回文件路径 ----
    if output_mode == "files_with_matches":
        cmd_parts = ["rg", "--files-with-matches", "--color=never"]
        if not case_sensitive:
            cmd_parts.append("-i")
        if file_glob:
            cmd_parts.extend(["--glob", shlex.quote(file_glob)])
        cmd_parts.extend(["-e", shlex.quote(pattern), shlex.quote(repo_path)])
        output = session.run_command(f"{' '.join(cmd_parts)} || true")
        files = []
        for line in output.splitlines():
            f = line.strip()
            if not f:
                continue
            if f.startswith(repo_path):
                f = f[len(repo_path):].lstrip("/")
            files.append(f)
        files.sort()
        total = len(files)
        page = files[offset:offset + max_matches]
        return {
            "files": page,
            "total_files": total,
            "truncated": offset + len(page) < total,
            "offset": offset,
        }

    # ---- count 模式:返回每个文件的匹配数 ----
    if output_mode == "count":
        cmd_parts = ["rg", "--count", "--color=never"]
        if not case_sensitive:
            cmd_parts.append("-i")
        if file_glob:
            cmd_parts.extend(["--glob", shlex.quote(file_glob)])
        cmd_parts.extend(["-e", shlex.quote(pattern), shlex.quote(repo_path)])
        output = session.run_command(f"{' '.join(cmd_parts)} || true")
        counts = {}
        total = 0
        for line in output.splitlines():
            # 格式: path:count
            idx = line.rfind(":")
            if idx < 0:
                continue
            f = line[:idx]
            c_str = line[idx + 1:]
            c = int(c_str) if c_str.isdigit() else 0
            if f.startswith(repo_path):
                f = f[len(repo_path):].lstrip("/")
            counts[f] = c
            total += c
        return {"counts": counts, "total_matches": total}

    # ---- content 模式(默认):匹配行 + 可选上下文 ----
    # 用 rg -A/-B 一次性带上下文,避免对每个匹配单独跑 awk(N+1 沙箱往返)
    cmd_parts = ["rg", "--line-number", "--no-heading", "--color=never"]
    cmd_parts.extend(["--max-count", str(offset + max_matches)])
    if context_lines > 0:
        cmd_parts.extend([
            f"--before-context={context_lines}",
            f"--after-context={context_lines}",
        ])
    if not case_sensitive:
        cmd_parts.append("-i")
    if file_glob:
        cmd_parts.extend(["--glob", shlex.quote(file_glob)])
    cmd_parts.extend(["-e", shlex.quote(pattern), shlex.quote(repo_path)])
    cmd = " ".join(cmd_parts)
    logger.info(f"[sandbox] search: {cmd}")
    output = session.run_command(f"{cmd} || true")

    all_matches = _parse_search_output_with_context(output, repo_path)
    total = len(all_matches)
    page = all_matches[offset:offset + max_matches]

    return {
        "matches": page,
        "total_matches": total,
        "truncated": offset + len(page) < total,
        "offset": offset,
    }


# rg 输出解析正则:
# - 匹配行格式: path:line:content(分隔符为 :)
# - 上下文行格式: path-line-content(分隔符为 -)
# 贪婪 .* 从右往左定位 ":数字:" / "-数字-",可正确处理路径含 : 或 - 的情况
_MATCH_LINE_RE = re.compile(r"^(.*):(\d+):(.*)$")
_CONTEXT_LINE_RE = re.compile(r"^(.*)-(\d+)-(.*)$")


def _parse_search_output_with_context(output: str, repo_path: str) -> list[dict]:
    """解析 rg 输出(支持 -A/-B 上下文模式)

    rg --no-heading 输出格式:
    - 匹配行: path:line:content
    - 上下文行: path-line-content(用 - 区分匹配行的 :)
    - 多个匹配之间用 -- 分隔(仅当带 -A/-B 时)

    无上下文时全是匹配行(无 -- 分隔),本函数同样适用:
    每个 match 的 context_before/after 为空列表。

    优先按匹配行格式解析(:line:),失败再按上下文行格式(-line-),
    避免上下文行的 content 含 ":N:" 时被误判。
    """
    matches: list[dict] = []
    current: dict | None = None
    before: list[str] = []
    after: list[str] = []

    def _finalize() -> None:
        nonlocal current, before, after
        if current is not None:
            current["context_before"] = before
            current["context_after"] = after
            matches.append(current)
            current = None
            before = []
            after = []

    for line in output.splitlines():
        if not line:
            continue
        if line == "--":
            _finalize()
            continue
        # 先尝试匹配行格式 path:N:content
        m = _MATCH_LINE_RE.match(line)
        if m:
            # 遇到新匹配,先收尾上一个(无 -- 分隔时也兼容)
            _finalize()
            path, line_no, content = m.groups()
            if path.startswith(repo_path):
                path = path[len(repo_path):].lstrip("/")
            current = {
                "file": path,
                "line": int(line_no),
                "content": content,
            }
            continue
        # 再尝试上下文行格式 path-N-content
        m = _CONTEXT_LINE_RE.match(line)
        if m and current is not None:
            _path, line_no, content = m.groups()
            ln = int(line_no)
            if ln < current["line"]:
                before.append(content)
            else:
                after.append(content)
            continue
        # 无法解析的行,跳过

    _finalize()
    return matches


# ============================================================
# 工具:find_files(按文件名 glob 查找,参考 TRAE Glob 工具)
# ============================================================


def find_files(
    repo_path: str,
    pattern: str,
    max_results: int = 100,
    offset: int = 0,
    task_id: str = "",
) -> dict:
    """按 glob 模式递归查找仓库内文件路径(不看内容)

    参考 TRAE Glob 工具设计:
    - 按文件名 pattern 匹配,不读取文件内容
    - 递归查找(支持 ** 通配)
    - 跳过噪声目录(.git / node_modules / __pycache__ / venv 等)
    - 返回相对仓库根的路径列表,按路径排序
    - 支持分页(offset + max_results)

    与 list_files 的区别:
    - list_files:列单层目录,看结构
    - find_files:按 pattern 递归定位文件,知道文件名/扩展名时用

    与 search_code 的区别:
    - search_code:按文件内容搜索(正则)
    - find_files:按文件名 pattern 搜索

    pattern 示例:
    - "**/*.py":所有层级的 .py 文件(递归)
    - "src/**/*.ts":src 下所有 .ts 文件
    - "**/test_*.py":所有 test_ 开头的 .py 文件
    - "**/*.{js,ts}":所有 .js 和 .ts 文件(brace expansion)

    参数:
        repo_path: clone_repo 返回的 path
        pattern: glob 模式(支持 *、**、?、{a,b})
        max_results: 最多返回文件数,默认 100
        offset: 分页偏移,跳过前 N 个结果,默认 0

    返回:{
        "pattern": str,
        "files": ["src/main.py", "src/utils.py", ...],  # 相对路径
        "total": int,
        "truncated": bool,
        "offset": int,
    }
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "mock":
        return _find_files_mock(repo_path, pattern, max_results, offset)
    else:
        return _find_files_sandbox(ctx, repo_path, pattern, max_results, offset)


def _expand_braces(pattern: str) -> list[str]:
    """展开 {a,b} brace expansion 成多个 glob pattern

    Python pathlib.glob 不支持 {a,b} 语法(rg --glob 原生支持),
    mock 模式手动展开以保持与 sandbox 模式行为一致。
    支持嵌套(递归处理)。无 brace 时返回 [pattern]。
    """
    m = re.search(r"\{([^{}]+)\}", pattern)
    if not m:
        return [pattern]
    options = m.group(1).split(",")
    expanded: list[str] = []
    for opt in options:
        sub = pattern[:m.start()] + opt.strip() + pattern[m.end():]
        expanded.extend(_expand_braces(sub))
    return expanded


def _find_files_mock(
    repo_path: str, pattern: str, max_results: int, offset: int,
) -> dict:
    """mock 模式:用 pathlib.Path.glob 递归匹配

    Python pathlib.glob 语义:
    - "*.py" 只匹配根目录(不递归)
    - "**/*.py" 递归所有层级
    - "src/**/*.py" 递归 src 下所有层级
    与 rg --glob 的"*.py 递归"语义有差异,文档里提示 LLM 用 ** 明确递归。
    """
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"仓库目录不存在: {repo_path}")

    # Python pathlib 不支持 {a,b},手动展开成多个 pattern
    patterns = _expand_braces(pattern)
    seen: set[str] = set()
    matched: list[str] = []
    for pat in patterns:
        for p in root.glob(pat):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(root).parts
            # 跳过噪声目录下的文件(检查除文件名外的父目录)
            if any(part in _SKIP_DIRS_LIST for part in rel_parts[:-1]):
                continue
            rel = str(p.relative_to(root))
            if rel not in seen:
                seen.add(rel)
                matched.append(rel)

    matched.sort()
    total = len(matched)
    page = matched[offset:offset + max_results]
    return {
        "pattern": pattern,
        "files": page,
        "total": total,
        "truncated": offset + len(page) < total,
        "offset": offset,
    }


def _find_files_sandbox(
    ctx: dict, repo_path: str, pattern: str, max_results: int, offset: int,
) -> dict:
    """sandbox 模式:用 rg --files --glob 递归匹配

    rg --files 列出所有文件路径(每行一个),--glob 按 gitignore 风格 glob 过滤。
    rg 的 --glob 语义:
    - "*.py" 递归匹配任意层级(与 Python pathlib 不同)
    - "**/*.py" 同上
    - "src/**/*.py" 匹配 src 下任意层级
    - 支持 {a,b} brace expansion

    --no-ignore:不遵守 .gitignore(列出所有文件,含被 ignore 的配置文件)
    --hidden:包含隐藏文件(如 .env.example)
    然后手动排除噪声目录,保证与 mock 模式行为一致。
    """
    session: SandboxSession = ctx["session"]

    # 检查仓库目录存在
    check = session.run_command(
        f"test -d {shlex.quote(repo_path)} && echo OK || echo MISSING"
    )
    if "MISSING" in check:
        raise FileNotFoundError(f"仓库目录不存在: {repo_path}")

    # rg --files 列出所有文件路径,--glob 过滤
    cmd_parts = ["rg", "--files", "--color=never", "--no-ignore", "--hidden"]
    # 排除噪声目录(rg --glob 用 ! 前缀表示排除,匹配任意层级)
    for skip in _SKIP_DIRS_LIST:
        cmd_parts.extend(["--glob", f"!**/{skip}/**"])
    # 用户的 pattern
    cmd_parts.extend(["--glob", shlex.quote(pattern)])
    cmd_parts.append(shlex.quote(repo_path))

    cmd = " ".join(cmd_parts)
    logger.info(f"[sandbox] find_files: {cmd}")
    output = session.run_command(f"{cmd} || true")

    files: list[str] = []
    for line in output.splitlines():
        f = line.strip()
        if not f:
            continue
        # 去掉 repo_path 前缀,转成相对路径
        if f.startswith(repo_path):
            f = f[len(repo_path):].lstrip("/")
        files.append(f)
    files.sort()
    total = len(files)
    page = files[offset:offset + max_results]

    return {
        "pattern": pattern,
        "files": page,
        "total": total,
        "truncated": offset + len(page) < total,
        "offset": offset,
    }


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


def _to_https_url(repo_url: str) -> str:
    """把 GitHub SSH URL 转成 HTTPS URL(镜像 _to_ssh_url)"""
    m = re.match(r"^git@github\.com:(.+?)(?:\.git)?$", repo_url)
    if m:
        path = m.group(1)
        return f"https://github.com/{path}.git"

    # 已经是 https 形式,原样返回
    return repo_url


def _inject_token_in_https(https_url: str, token: str) -> str:
    """把 GitHub HTTPS URL 注入 access_token,形成带认证的 clone URL

    https://github.com/owner/repo.git
        → https://x-access-token:{token}@github.com/owner/repo.git

    若 URL 非 GitHub HTTPS 或已含认证信息,原样返回。
    """
    if not token or not https_url.startswith("https://github.com/"):
        return https_url
    # 已含认证信息,不重复注入
    if "@" in https_url.split("://", 1)[1].split("/", 1)[0]:
        return https_url
    return https_url.replace(
        "https://",
        f"https://x-access-token:{token}@",
        1,
    )


def clone_repo_with_fallback(
    repo_url: str, branch: str | None = None, task_id: str = "",
    github_token: str = "",
) -> dict:
    """克隆仓库(协议回退:HTTPS+token → SSH → HTTPS 匿名)

    供 orchestrator 在 user_agent 评估前主动调用,也供 clone_repo 工具委托。

    回退链(按顺序尝试,首个成功即返回):
    1. HTTPS + token(github_token 非空时,可访问私有仓库)
    2. SSH(依赖宿主机/沙箱的 SSH key 配置,适合公开仓库)
    3. HTTPS 匿名(无 token,仅公开仓库)
    三者都失败才抛 RuntimeError。

    复用同一套 session 管理(_get_or_create_session + _set_repo_path),
    所以 clone 完成后 react_agent / workspace 路由可直接通过 task_id 复用会话。
    """
    # 构造候选 URL:HTTPS+token、SSH、HTTPS 匿名(去重)
    https_anon = _to_https_url(repo_url)
    ssh_url = _to_ssh_url(repo_url)
    https_with_token = _inject_token_in_https(https_anon, github_token) if github_token else ""

    candidates: list[str] = []
    for u in [https_with_token, ssh_url, https_anon]:
        if u and u not in candidates:
            candidates.append(u)

    # 从 URL 提取仓库名(两种格式都支持)
    match = re.search(r"/([^/]+?)(?:\.git)?$", repo_url)
    if not match:
        raise ValueError(f"无法从 URL 解析仓库名: {repo_url}")
    repo_name = match.group(1)

    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    errors: list[str] = []
    for idx, url in enumerate(candidates):
        # 日志里不打印 token(脱敏)
        safe_url = url.split("@")[-1] if "@" in url else url
        try:
            logger.info(
                f"[clone_fallback] task={task_id} 尝试第 {idx + 1} 种协议: {safe_url}"
            )
            if mode == "mock":
                result = _clone_repo_mock(ctx, url, repo_name, branch)
            else:
                result = _clone_repo_sandbox(ctx, url, repo_name, branch)
            _set_repo_path(task_id, result["path"])
            logger.info(f"[clone_fallback] task={task_id} 克隆成功(协议 {safe_url})")
            return result
        except Exception as e:
            err_msg = str(e)[:300]
            errors.append(f"[{safe_url}] {err_msg}")
            logger.warning(
                f"[clone_fallback] task={task_id} 协议 {safe_url} 克隆失败: {err_msg}"
            )
            # 清理可能残留的半成品目录(mock 模式),避免下次重试撞目录
            if mode == "mock":
                mock_dir: Path = ctx["mock_dir"]
                leftover = mock_dir / repo_name
                if leftover.exists():
                    try:
                        shutil.rmtree(leftover, ignore_errors=True)
                    except Exception:
                        pass

    raise RuntimeError(
        f"仓库克隆失败(已尝试 {len(candidates)} 种协议):\n" + "\n".join(errors)
    )


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
                "mock 模式不支持 semgrep(需要 Linux 沙箱环境)。"
                "请通过其他工具(search_code + read_file)进行手动 SAST 检查,"
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
