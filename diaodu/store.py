"""任务目录。每个任务一份状态和一份工作区。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        path = (self.root / job_id).resolve()
        if path.parent != self.root.resolve():
            raise ValueError("非法任务编号")
        return path

    def work_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "work"

    def create(self, brief: str) -> dict:
        brief = brief.strip()
        if not brief:
            raise ValueError("目标不能为空")
        job_id = uuid.uuid4().hex[:8]
        work = self.work_dir(job_id)
        work.mkdir(parents=True)
        (work / "douyin" / "publish-pack").mkdir(parents=True)
        (work / "wechat").mkdir()
        (work / "brief.md").write_text(brief + "\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=work, check=False)
        job = {
            "id": job_id,
            "brief": brief,
            "phase": "collect",
            "running": None,
            "stop_requested": False,
            "messages": [],
            "created_at": now(),
        }
        self.save(job)
        return job

    def load(self, job_id: str) -> dict:
        path = self.job_dir(job_id) / "job.json"
        if not path.is_file():
            raise KeyError(job_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, job: dict) -> None:
        """Publish job.json by replacing the whole file.

        write_text truncates in place, so a reader on another thread (stop
        reloads after releasing the service lock) can observe an empty file
        and raise JSONDecodeError. A temp file in the same directory plus
        os.replace is atomic on this filesystem.
        """
        directory = self.job_dir(job["id"])
        path = directory / "job.json"
        fd, name = tempfile.mkstemp(prefix=".job-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(job, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise

    def list_jobs(self) -> list[dict]:
        jobs = []
        if not self.root.exists():
            return jobs
        for child in self.root.iterdir():
            meta = child / "job.json"
            if meta.is_file():
                jobs.append(json.loads(meta.read_text(encoding="utf-8")))
        jobs.sort(key=lambda item: item["created_at"], reverse=True)
        return jobs

    def list_files(self, job_id: str) -> list[str]:
        work = self.work_dir(job_id)
        names = []
        for path in work.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(work).as_posix()
            if any(part.startswith(".") for part in path.relative_to(work).parts):
                continue
            names.append(relative)
        names.sort()
        return names

    def read_file(self, job_id: str, relative: str) -> str:
        work = self.work_dir(job_id).resolve()
        path = (work / relative).resolve()
        if path != work and work not in path.parents:
            raise ValueError("不能读任务目录以外的文件")
        if not path.is_file():
            raise FileNotFoundError(relative)
        return path.read_text(encoding="utf-8")

    def write_file(self, job_id: str, relative: str, text: str) -> None:
        work = self.work_dir(job_id).resolve()
        path = (work / relative).resolve()
        if work not in path.parents:
            raise ValueError("不能写任务目录以外的文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
