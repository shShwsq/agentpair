"""异步出题任务的内存状态管理

POST /practice/generate 立即返回 job_id,后台线程执行生成,
前端轮询 GET /practice/generate/{job_id} 获取进度与结果。

设计:
- 进程内 dict + 锁(单实例部署够用;多实例部署时各实例轮询自己的 job,
  前端轮询命中非执行实例会 404,属已知边界,后续可换 Redis)
- job 完成/失败后保留 TTL 秒供轮询取结果,过期或超量时清理
- job 记录 user_id,读取时校验,防跨用户窥探
"""
import threading
import time
import uuid

# job 完成后保留时长(秒),供前端取结果
_JOB_TTL_SECONDS = 3600
# 最多保留的 job 数(超量时先清理最旧的已完成 job)
_MAX_JOBS = 200

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def create_job(user_id) -> str:
    job_id = uuid.uuid4().hex
    with _LOCK:
        _prune_locked()
        _JOBS[job_id] = {
            "user_id": user_id,
            "status": "pending",
            "done": 0,
            "total": 0,
            "error": "",
            "questions": [],
            "skipped_findings": 0,
            "created_at": time.monotonic(),
            "finished_at": None,
        }
    return job_id


def get_job(job_id: str, user_id) -> dict | None:
    """按 id + user 读取 job(不存在或不属于该用户返回 None)"""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        # 拷贝一份,避免读时被 worker 线程修改
        return dict(job)


def update_job(job_id: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        if fields.get("status") in ("done", "error"):
            job["finished_at"] = time.monotonic()


def set_total(job_id: str, total: int) -> None:
    update_job(job_id, total=total, status="running")


def _prune_locked() -> None:
    """清理过期/超量 job(调用方持锁)"""
    now = time.monotonic()
    expired = [
        jid for jid, j in _JOBS.items()
        if j["finished_at"] is not None and now - j["finished_at"] > _JOB_TTL_SECONDS
    ]
    for jid in expired:
        del _JOBS[jid]
    # 超量:按创建时间淘汰最旧的已完成 job
    if len(_JOBS) > _MAX_JOBS:
        finished = sorted(
            ((jid, j) for jid, j in _JOBS.items() if j["finished_at"] is not None),
            key=lambda kv: kv[1]["created_at"],
        )
        for jid, _ in finished[: len(_JOBS) - _MAX_JOBS]:
            del _JOBS[jid]
