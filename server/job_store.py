# -*- coding: utf-8 -*-
"""打印任务登记与状态持久化（仅服务端）。

每个任务对应两个文件：
  spool/jobs/<job_id>.json   任务元数据（状态机持久化，重启安全）
  spool/<job_id>.pdf         待打印源文件（打印成功后删除）
"""

import json
import logging
import os
import time
from pathlib import Path

from common.protocol import JobState, TERMINAL_STATES

log = logging.getLogger("lanprint.server")


class JobStore:
    """基于文件的任务存储（JSON 原子写入，服务重启后可恢复）。"""

    def __init__(self, spool_dir):
        self.spool_dir = Path(spool_dir)
        self.jobs_dir = self.spool_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    # -- 内部工具 ----------------------------------------------------------

    @staticmethod
    def _atomic_write_json(path: Path, obj: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)

    def _meta_path(self, job_id: str) -> Path:
        return self.jobs_dir / ("%s.json" % job_id)

    # -- 公开接口 ----------------------------------------------------------

    def add_job(self, job_id: str, filename: str, copies: int, path) -> dict:
        """登记新任务（初始状态 queued），返回任务元数据。"""
        meta = {
            "job_id": job_id,
            "filename": filename,
            "copies": int(copies),
            "file": str(path),
            "state": JobState.QUEUED,
            "win_job_id": None,
            "pages": None,
            "error": None,
            "created_at": time.time(),
            "finished_at": None,
        }
        self._atomic_write_json(self._meta_path(job_id), meta)
        return meta

    def get_job(self, job_id: str):
        path = self._meta_path(job_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def set_state(self, job_id: str, state: str, **extra) -> None:
        """更新任务状态及附加字段（pages/error/win_job_id/finished_at）。"""
        meta = self.get_job(job_id)
        if meta is None:
            return
        old = meta.get("state")
        meta["state"] = state
        if state in TERMINAL_STATES:
            meta["finished_at"] = time.time()
        meta.update(extra)
        self._atomic_write_json(self._meta_path(job_id), meta)
        log.info("任务 %s 状态: %s -> %s%s", job_id, old, state,
                 ("，原因: %s" % extra["error"]) if extra.get("error") else "")

    def all_jobs(self):
        """遍历全部任务元数据（重启恢复用）。"""
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    yield meta
            except (OSError, ValueError):
                continue

    def pending_jobs(self):
        """按创建时间排序返回 queued 状态任务（FIFO 依据）。"""
        jobs = [m for m in self.all_jobs() if m.get("state") == JobState.QUEUED]
        jobs.sort(key=lambda m: m.get("created_at", 0))
        return jobs

    def cleanup_expired(self, keep_failed_days: int) -> None:
        """删除终态超期任务的源文件与元数据。"""
        horizon = time.time() - max(1, int(keep_failed_days)) * 86400
        for meta in list(self.all_jobs()):
            state = meta.get("state")
            finished = meta.get("finished_at") or 0
            if state in TERMINAL_STATES and finished and finished < horizon:
                self.remove_job(meta["job_id"])

    def remove_job(self, job_id: str) -> None:
        """删除任务的源文件与元数据。"""
        meta = self.get_job(job_id)
        if meta and meta.get("file"):
            try:
                os.remove(meta["file"])
            except OSError:
                pass
        try:
            self._meta_path(job_id).unlink()
        except OSError:
            pass
