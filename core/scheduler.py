import asyncio
import logging
import threading
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

class SchedulerManager:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.timezone = timezone
        self.scheduler: AsyncIOScheduler | None = None
        self._started = False
        self._init_lock = threading.Lock()
        # Delay initialization until event loop is available
        try:
            asyncio.get_running_loop()
            self._init_scheduler()
        except RuntimeError:
            # No event loop - will initialize later
            pass
    
    def _init_scheduler(self) -> None:
        """Initialize and start the scheduler (requires event loop)"""
        with self._init_lock:
            if self.scheduler is None:
                self.scheduler = AsyncIOScheduler(timezone=self.timezone)
            if not self._started:
                self.scheduler.start()
                self._started = True
    
    def ensure_started(self) -> None:
        """Ensure scheduler is initialized and started (requires event loop)"""
        if not self._started:
            self._init_scheduler()

    def add_job(self, job_id: str, func, cron: dict[str, Any]) -> None:
        self.ensure_started()
        self.remove_job(job_id)
        if self.scheduler:
            self.scheduler.add_job(func, trigger="cron", id=job_id, **cron)
            logger.info("Scheduled job %s", job_id)

    def remove_job(self, job_id: str) -> None:
        if not self.scheduler:
            return
        try:
            self.scheduler.remove_job(job_id)
        except JobLookupError:
            return

    def clear_prefix(self, prefix: str) -> None:
        if not self.scheduler:
            return
        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                self.scheduler.remove_job(job.id)
