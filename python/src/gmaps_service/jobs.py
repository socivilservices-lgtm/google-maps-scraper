"""In-memory job records, with JSON and CSV copies under the output directory."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from gmaps_service.models import ScrapeResult, utc_now

_JOB_ID = re.compile(r"^[a-f0-9]{32}$")


class JobNotFound(KeyError):
    """No job matches the id."""


class JobStore:
    """Track scrape jobs for the HTTP service."""

    def __init__(self, output_dir: str) -> None:
        self.directory = Path(output_dir) / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict[str, object]] = {}

    def create(self, *, query: str, location: str, max_results: int, backend: str) -> dict[str, object]:
        job_id = uuid.uuid4().hex
        job: dict[str, object] = {
            "job_id": job_id,
            "status": "queued",
            "query": query,
            "location": location,
            "max_results": max_results,
            "backend": backend,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "error": None,
            "error_type": None,
            "result": None,
        }
        self._jobs[job_id] = job
        return job

    def mark_running(self, job_id: str) -> None:
        job = self._require(job_id)
        job["status"] = "running"
        job["updated_at"] = utc_now()

    def complete(self, job_id: str, result: ScrapeResult) -> None:
        job = self._require(job_id)
        job["status"] = "completed"
        job["result"] = result.to_dict()
        job["error"] = None
        job["error_type"] = None
        job["updated_at"] = utc_now()

    def fail(self, job_id: str, message: str, error_type: str) -> None:
        job = self._require(job_id)
        job["status"] = "failed"
        job["error"] = message
        job["error_type"] = error_type
        job["updated_at"] = utc_now()

    def get(self, job_id: str) -> dict[str, object]:
        self._validate(job_id)
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        loaded = self._from_disk(job_id)
        if loaded is None:
            raise JobNotFound(job_id)
        self._jobs[job_id] = loaded
        return loaded

    def public(self, job_id: str) -> dict[str, object]:
        job = self.get(job_id)
        body = dict(job)
        if job.get("status") == "completed":
            body["downloads"] = {
                "json": f"/jobs/{job_id}/download?format=json",
                "csv": f"/jobs/{job_id}/download?format=csv",
            }
        return body

    def file_for(self, job_id: str, file_format: str) -> Path:
        self._validate(job_id)
        if file_format not in {"json", "csv"}:
            raise ValueError(file_format)
        return self.directory / f"{job_id}.{file_format}"

    def _require(self, job_id: str) -> dict[str, object]:
        self._validate(job_id)
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    def _from_disk(self, job_id: str) -> dict[str, object] | None:
        path = self.file_for(job_id, "json")
        if not path.is_file():
            return None
        result = json.loads(path.read_text(encoding="utf-8"))
        return {
            "job_id": job_id,
            "status": "completed",
            "query": result.get("query", ""),
            "location": result.get("location", ""),
            "max_results": result.get("total_found"),
            "backend": result.get("backend", ""),
            "created_at": result.get("timestamp"),
            "updated_at": result.get("timestamp"),
            "error": None,
            "error_type": None,
            "result": result,
        }

    @staticmethod
    def _validate(job_id: str) -> None:
        if not _JOB_ID.fullmatch(job_id or ""):
            raise JobNotFound(job_id)
