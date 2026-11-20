import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from typing import Annotated

import typer
import yaml

from nowcast import config, paths
from nowcast.log import get_logger

LOG_LIMIT = 2 * 1024 * 1024
_log_lock = Lock()


def append_run(path, record):
    with _log_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        if path.stat().st_size > LOG_LIMIT:
            with path.open("rb") as stream:
                stream.seek(-LOG_LIMIT // 2, os.SEEK_END)
                stream.readline()
                newest = stream.read()
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(newest)
            temporary.replace(path)


def daily_due(job, now):
    hour, minute = map(int, job["at"].split(":"))
    stamp = datetime.fromtimestamp(now, UTC).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if stamp.timestamp() < now:
        stamp += timedelta(days=1)
    return stamp.timestamp()


class Runner:
    def __init__(self, lanes, root, command=None):
        self.lanes = lanes
        self.log_path = root / "state" / "runs.jsonl"
        self.command = command or [sys.executable, "-m", "nowcast.cli"]
        self.stop = Event()
        self.shutdown_at = None
        self.logger = get_logger(__name__)

    def shutdown(self, signum=None, frame=None):
        if self.shutdown_at is None:
            self.shutdown_at = time.monotonic()
        self.stop.set()

    def execute(self, lane, job):
        if self.stop.is_set():
            return
        started = datetime.now(UTC)
        begin = time.monotonic()
        self.logger.info("Starting %s/%s", lane, job["name"])
        child = subprocess.Popen(self.command + job["command"], start_new_session=True)
        terminated = False
        failed = False
        while child.poll() is None:
            elapsed = time.monotonic() - begin
            if elapsed >= job.get("timeout", 1800):
                failed = True
                self.kill_group(child, signal.SIGKILL)
                break
            if self.stop.is_set():
                failed = True
                if not terminated:
                    self.kill_group(child, signal.SIGTERM)
                    terminated = True
                if time.monotonic() - self.shutdown_at >= 20:
                    self.kill_group(child, signal.SIGKILL)
                    break
            time.sleep(0.1)
        code = child.wait()
        # A child can exit before its descendants; stop the whole process group.
        self.kill_group(child, signal.SIGKILL)
        record = {
            "job": job["name"], "lane": lane, "started": started.isoformat(),
            "seconds": round(time.monotonic() - begin, 3),
            "status": "success" if code == 0 and not failed else "failure",
            "exit_code": code,
        }
        append_run(self.log_path, record)
        self.logger.info("Finished %s/%s: %s (%s, %.1fs)",
                         lane, job["name"], record["status"], code, record["seconds"])

    @staticmethod
    def kill_group(child, signum):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    def lane(self, lane, once=False):
        jobs = lane["jobs"]
        now = time.time()
        due = [now if "every" in job else daily_due(job, now) for job in jobs]
        while not self.stop.is_set():
            now = time.time()
            selected = [i for i, stamp in enumerate(due) if once or stamp <= now]
            for i in selected:
                self.execute(lane["name"], jobs[i])
            if once or self.stop.is_set():
                return
            now = time.time()
            for i in selected:
                job = jobs[i]
                if "every" in job:
                    # Skip missed intervals and retain the shared cadence of paired jobs.
                    due[i] += (int((now - due[i]) // job["every"]) + 1) * job["every"]
                else:
                    due[i] = daily_due(job, now + 0.001)
            self.stop.wait(max(0, min(due) - time.time()))

    def run(self, once=False):
        with ThreadPoolExecutor(max_workers=len(self.lanes)) as pool:
            futures = [pool.submit(self.run_lane, lane, once) for lane in self.lanes]
            for future in futures:
                future.result()

    def run_lane(self, lane, once):
        try:
            self.lane(lane, once)
        except Exception:
            self.shutdown()
            raise


def runner(
    once: Annotated[bool, typer.Option("--once", help="Run every job once and exit.")] = False,
) -> None:
    """Run the live job schedule in serial lanes."""
    lanes = yaml.safe_load((config.CONFIG_DIR / "schedule.yaml").read_text())
    service = Runner(lanes, paths.data_root())
    previous = {sig: signal.signal(sig, service.shutdown) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        service.run(once)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
