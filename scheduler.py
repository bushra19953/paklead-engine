"""
Daily scheduler (TRD §1.7). APScheduler runs `pipeline.run_daily` on the
RUN_TIME cron schedule in the Asia/Karachi timezone.

    python scheduler.py

If APScheduler is not installed, it logs a clear error instead of
silently doing nothing. Missed runs (host down) are detected on restart:
the gap is logged and surfaced, never silently backfilled beyond the
watermark (TRD FR-PK-64).
"""

from __future__ import annotations

import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from config import Settings  # noqa: E402
from engine.pipeline import Pipeline  # noqa: E402
from models import audit, connect, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("paklead.scheduler")


def _timezone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Karachi")
    except Exception:
        log.warning("zoneinfo unavailable — scheduler falls back to UTC.")
        import datetime
        return datetime.timezone.utc


def main() -> None:
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore
        from apscheduler.triggers.cron import CronTrigger  # type: ignore
    except Exception:
        log.error("APScheduler is not installed — run: pip install -r requirements.txt")
        raise SystemExit(1)

    settings = Settings()
    conn = connect(settings.DB_PATH)
    init_db(conn)
    pipeline = Pipeline(settings, conn)

    def job():
        if settings.KILL_SWITCH:
            audit(conn, "scheduler", "run_skipped_kill_switch", detail={})
            log.warning("KILL_SWITCH=true — scheduled run skipped.")
            return
        summary = pipeline.run_daily()
        log.info("Daily run complete: %s sources, %s leads, %s alerts.",
                 len(summary.get("runs", [])), summary.get("leads"),
                 len(summary.get("alerts", [])))

    cron = settings.RUN_TIME.strip()
    try:
        trigger = CronTrigger.from_crontab(cron, timezone=_timezone())
    except Exception:
        parts = cron.split(":")
        trigger = CronTrigger(hour=int(parts[0]), minute=int(parts[1]),
                              timezone=_timezone())
    scheduler = BlockingScheduler(timezone=_timezone())
    scheduler.add_job(job, trigger, id="paklead-daily", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True)
    log.info("Scheduler started — daily run at '%s' (Asia/Karachi).", cron)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
