#!/usr/bin/env python3
"""ACP Bridge:HTTP <-> stdio 桥接服务(运行在沙箱内)

将后端的 HTTP 请求桥接到任意支持 ACP(Agent Client Protocol)over stdio 的
智能体 CLI(如 Qoder CLI),CLI 通过命令行参数启动,凭证经环境变量注入。

工作原理:
1. 启动时 spawn `<bin> <args...>` 子进程(如 `qodercli --acp --yolo`),
   持有 stdin/stdout pipe。凭证(PAT 等)由父进程(bridge)环境变量继承,
   无需命令行明文传递。
2. 监听 HTTP 端口(默认 8088)
3. POST /rpc:接收 JSON-RPC 请求,写入 CLI stdin,读取 stdout 响应
   - 响应通过 SSE(stream)返回:每行 stdout 作为一个 SSE event
   - 通知(notification,无 id)作为中间事件,最终响应(有 id)作为终止事件
4. GET /health:健康检查(CLI 进程存活返回 200)

ACP 协议(JSON-RPC 2.0 over stdio):
- 请求:{"jsonrpc":"2.0","method":"initialize","params":{...},"id":1}
- 响应:{"jsonrpc":"2.0","result":{...},"id":1}
- 通知:{"jsonrpc":"2.0","method":"progress","params":{...}}  (无 id)

使用方式(沙箱内):
    python acp_bridge.py --port 8088 --bin qodercli --args '["--acp","--yolo"]'

依赖:仅 Python 标准库(http.server, subprocess, json, threading)
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ============================================================
# ACP CLI 进程管理(通用,不绑定具体 CLI)
# ============================================================


class ACPCLIProcess:
    """管理 ACP CLI 子进程的 stdin/stdout 通信

    通过 bin + args 启动子进程,子进程继承父进程环境变量
    (凭证经 envs 注入到 bridge,再继承给 CLI,避免命令行明文)。
    """

    def __init__(self, bin_name: str, args: list[str] | None = None):
        self.bin_name = bin_name
        self.args = args or []
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()  # 保护 stdin 写入(并发请求互斥)

    def start(self) -> None:
        """启动 ACP CLI 子进程"""
        cmd = [self.bin_name, *self.args]
        print(f"[bridge] 启动 ACP CLI: {' '.join(cmd)}", flush=True)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # 行缓冲
        )
        # 启动 stderr 监控线程(把 CLI 的 stderr 转发到 bridge 的 stderr)
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stderr(self) -> None:
        """把 CLI 的 stderr 输出到 bridge 的 stderr(调试用)"""
        if not self.proc or not self.proc.stderr:
            return
        for line in self.proc.stderr:
            print(f"[cli stderr] {line.rstrip()}", file=sys.stderr, flush=True)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send_and_collect(self, request: dict) -> list[str]:
        """发送 JSON-RPC 请求,收集所有响应行(通知 + 最终响应)

        返回行列表,每行是一个 JSON 字符串。最后一行是匹配 id 的最终响应。
        通知(无 id)在最终响应之前返回。

        线程安全:用锁保护 stdin 写入,避免并发请求交叉。
        """
        if not self.alive:
            raise RuntimeError("ACP CLI 进程未运行或已退出")

        request_id = request.get("id")
        request_line = json.dumps(request, ensure_ascii=False)
        collected: list[str] = []

        with self._lock:
            # 写入请求(加换行符,ACP 用 newline-delimited JSON)
            assert self.proc is not None
            assert self.proc.stdin is not None
            self.proc.stdin.write(request_line + "\n")
            self.proc.stdin.flush()

            # 逐行读取响应,直到收到匹配 id 的最终响应
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                collected.append(line)
                # 检查是否是最终响应(有 id 且匹配)
                try:
                    msg = json.loads(line)
                    if request_id is not None and msg.get("id") == request_id:
                        break
                except json.JSONDecodeError:
                    continue  # 非 JSON 行(如日志),跳过

        return collected

    def stop(self) -> None:
        """停止 CLI 子进程"""
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            self.proc = None


# ============================================================
# HTTP 请求处理
# ============================================================

# 全局 CLI 进程实例(所有请求共享)
_cli: ACPCLIProcess | None = None


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, format, *args):
        # 覆盖默认日志,安全格式化(避免参数数量不匹配导致异常)
        try:
            msg = format % args if args else format
        except Exception:
            msg = f"{format} {args}"
        print(f"[bridge] {msg}", file=sys.stderr, flush=True)

    def do_GET(self):
        """GET /health:健康检查"""
        if self.path == "/health":
            alive = _cli is not None and _cli.alive
            status_code = 200 if alive else 503
            body = json.dumps({"status": "ok" if alive else "cli_not_running"})
            self._send_json(status_code, body)
        else:
            self._send_json(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        """POST /rpc:发送 JSON-RPC 请求,流式返回 SSE(每行 stdout 即时推送)

        与批量收集模式不同,这里逐行读取 CLI stdout 并立即推送 SSE,
        让后端能实时收到 thinking/text/tool_call 增量,实现真正流式体验。

        线程安全:整个读取过程持有 _cli._lock,避免并发请求交叉。
        (ACP 是串行协议,同一时刻只处理一个请求,锁不影响吞吐)
        """
        if self.path != "/rpc":
            self._send_json(404, json.dumps({"error": "not found"}))
            return

        if _cli is None or not _cli.alive:
            self._send_json(503, json.dumps({"error": "CLI process not running"}))
            return

        # 读取请求体
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            request = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, json.dumps({"error": f"invalid JSON: {e}"}))
            return

        request_id = request.get("id")
        request_method = request.get("method", "")
        request_line = json.dumps(request, ensure_ascii=False)

        print(f"[bridge] >>> 发送到 CLI: method={request_method}, id={request_id}", file=sys.stderr, flush=True)

        # 发送请求 + 流式读取响应(持锁,串行)
        try:
            with _cli._lock:
                if not _cli.alive:
                    raise RuntimeError("ACP CLI 进程已退出")

                assert _cli.proc is not None
                assert _cli.proc.stdin is not None
                _cli.proc.stdin.write(request_line + "\n")
                _cli.proc.stdin.flush()

                # 先发 SSE 响应头
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                # 逐行读取 stdout,立即推送 SSE
                # 使用 readline + 超时检测,避免 CLI 无输出时永久挂起
                assert _cli.proc.stdout is not None
                import select as _select
                stdout_fd = _cli.proc.stdout.fileno()
                deadline = time.time() + 120  # 单次请求最多等 120s
                line_count = 0

                while time.time() < deadline:
                    # 用 select 检查 stdout 是否有数据(1s 超时,便于周期性检查 deadline)
                    ready, _, _ = _select.select([stdout_fd], [], [], 1.0)
                    if not ready:
                        # 暂无数据,检查 CLI 进程是否还活着
                        if not _cli.alive:
                            print(f"[bridge] CLI 进程在等待响应时退出(method={request_method})", file=sys.stderr, flush=True)
                            break
                        continue

                    raw_line = _cli.proc.stdout.readline()
                    if not raw_line:
                        # EOF,CLI 进程关闭了 stdout
                        print(f"[bridge] CLI stdout EOF(method={request_method})", file=sys.stderr, flush=True)
                        break

                    line = raw_line.strip()
                    if not line:
                        continue

                    line_count += 1
                    print(f"[bridge] <<< CLI stdout [{line_count}]: {line[:500]}", file=sys.stderr, flush=True)

                    sse_data = f"data: {line}\n\n"
                    try:
                        self.wfile.write(sse_data.encode("utf-8"))
                        self.wfile.flush()
                    except BrokenPipeError:
                        break  # 客户端断开连接

                    # 收到匹配 id 的最终响应 → 结束流
                    try:
                        msg = json.loads(line)
                        if request_id is not None and msg.get("id") == request_id:
                            print(f"[bridge] 收到匹配 id={request_id} 的最终响应,结束流", file=sys.stderr, flush=True)
                            break
                    except json.JSONDecodeError:
                        continue

                if line_count == 0:
                    print(f"[bridge] 警告:CLI 在 120s 内未输出任何响应行(method={request_method})", file=sys.stderr, flush=True)
        except Exception as e:
            # 响应头未发时返回 JSON 错误;已发则只能日志记录
            try:
                self._send_json(500, json.dumps({"error": str(e)}))
            except Exception:
                print(f"[bridge] 流式响应异常: {e}", file=sys.stderr, flush=True)

    def _send_json(self, status_code: int, body: str) -> None:
        """发送 JSON 响应"""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


# ============================================================
# 主入口
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="ACP Bridge: HTTP <-> stdio")
    parser.add_argument(
        "--port", type=int, default=8088,
        help="HTTP 监听端口(默认 8088)",
    )
    parser.add_argument(
        "--bin", type=str, default="qodercli",
        help="ACP CLI 可执行文件名/路径(默认 qodercli,从 PATH 查找或绝对路径)",
    )
    parser.add_argument(
        "--args", type=str, default='["--acp", "--permission-mode", "bypass_permissions"]',
        help='CLI 启动参数(JSON 数组,默认 \'["--acp", "--permission-mode", "bypass_permissions"]\')',
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="监听地址(默认 0.0.0.0,允许外部访问)",
    )
    args = parser.parse_args()

    # 解析 args JSON
    try:
        cli_args = json.loads(args.args)
        if not isinstance(cli_args, list):
            raise ValueError("args 必须是 JSON 数组")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[bridge] --args 解析失败: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    global _cli

    # 启动 CLI 子进程(继承当前环境变量,凭证经 envs 注入到 bridge 进程)
    _cli = ACPCLIProcess(bin_name=args.bin, args=cli_args)
    _cli.start()

    # 等待 CLI 就绪(短暂等待进程稳定)
    time.sleep(1)
    if not _cli.alive:
        print("[bridge] ACP CLI 启动失败,退出", file=sys.stderr, flush=True)
        sys.exit(1)

    # 启动 HTTP 服务器
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    print(f"[bridge] HTTP 服务监听 {args.host}:{args.port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[bridge] 正在关闭...", file=sys.stderr, flush=True)
        if _cli:
            _cli.stop()
        server.server_close()


if __name__ == "__main__":
    main()
