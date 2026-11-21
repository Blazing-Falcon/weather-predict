import json
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime

from typer.testing import CliRunner

from nowcast import config
from nowcast import runner as module
from nowcast.cli import app
from nowcast.runner import Runner, append_run, daily_due


def job(name, command=None, **schedule):
    return {"name": name, "command": command or ["-c", "pass"], **(schedule or {"every": 60})}


def records(root):
    return [json.loads(line) for line in (root / "state/runs.jsonl").read_text().splitlines()]


def test_once_serial_children_and_log(tmp_path):
    output = tmp_path / "order"
    command = (
        "import pathlib,time,sys; p=pathlib.Path(sys.argv[1]); "
        "f=p.open('a'); f.write(sys.argv[2]+' start\\n'); f.flush(); "
        "time.sleep(.05); f.write(sys.argv[2]+' end\\n'); f.close()"
    )
    jobs = [job(name, ["-c", command, str(output), name]) for name in ("first", "second")]
    Runner([{"name": "fast", "jobs": jobs}], tmp_path, [sys.executable]).run(once=True)
    assert output.read_text().splitlines() == ["first start", "first end", "second start", "second end"]
    rows = records(tmp_path)
    assert [row["job"] for row in rows] == ["first", "second"]
    assert set(rows[0]) == {"job", "lane", "started", "seconds", "status", "exit_code"}
    assert all(row["status"] == "success" and row["exit_code"] == 0 for row in rows)
    assert datetime.fromisoformat(rows[0]["started"]).tzinfo == UTC


def test_due_order_and_late_runs_once(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    service = Runner([], tmp_path)
    seen = []

    def execute(lane, item):
        seen.append(item["name"])
        clock[0] += 150

    def wait(seconds):
        assert seconds == 60
        service.shutdown()

    monkeypatch.setattr(service, "execute", execute)
    monkeypatch.setattr(service.stop, "wait", wait)
    service.lane({"name": "fast", "jobs": [job("tick"), job("score"), job("daily", at="01:30")]})
    assert seen == ["tick", "score"]


def test_daily_utc():
    now = datetime(2026, 1, 1, 2, tzinfo=UTC).timestamp()
    assert daily_due({"at": "01:30"}, now) == datetime(2026, 1, 2, 1, 30, tzinfo=UTC).timestamp()


def test_log_trim_and_threads(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "LOG_LIMIT", 2048)
    path = tmp_path / "state/runs.jsonl"
    threads = [threading.Thread(target=lambda n=n: [
        append_run(path, {"job": str(n), "sequence": i, "padding": "x" * 100})
        for i in range(30)
    ]) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    append_run(path, {"job": "last"})
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-1] == {"job": "last"}
    assert path.stat().st_size <= 2048
    assert len(rows) < 61


def test_timeout_kills_and_logs(tmp_path):
    service = Runner([], tmp_path, [sys.executable])
    service.execute("fast", job("slow", ["-c", "import time; time.sleep(60)"], timeout=.2))
    row, = records(tmp_path)
    assert row["status"] == "failure"
    assert row["exit_code"] == -signal.SIGKILL
    assert row["seconds"] < 3


def test_shutdown_stops_child_and_new_jobs(tmp_path):
    service = Runner([], tmp_path, [sys.executable])
    ready = tmp_path / "ready"
    command = f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    lane = {"name": "fast", "jobs": [job("slow", ["-c", command]), job("never")]}
    thread = threading.Thread(target=service.lane, args=(lane, True))
    thread.start()
    import time

    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert ready.exists()
    service.shutdown(signal.SIGTERM)
    thread.join(timeout=3)
    assert not thread.is_alive()
    row, = records(tmp_path)
    assert row["job"] == "slow"
    assert row["status"] == "failure"
    assert row["exit_code"] == -signal.SIGTERM


def test_cli_once_fake_schedule(monkeypatch, tmp_path):
    (tmp_path / "schedule.yaml").write_text(
        '- name: fake\n  jobs:\n    - name: version\n      command: [--version]\n      every: 60\n'
    )
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    result = CliRunner().invoke(app, ["runner", "--once"])
    assert result.exit_code == 0, result.output
    assert records(tmp_path)[0]["status"] == "success"


def test_lanes_run_independently(tmp_path, monkeypatch):
    lanes = [{"name": name, "jobs": [job("one"), job("two")]} for name in ("fast", "bmkg")]
    service = Runner(lanes, tmp_path)
    barrier = threading.Barrier(2, timeout=3)
    seen = {"fast": [], "bmkg": []}

    def execute(lane, item):
        seen[lane].append(item["name"])
        barrier.wait()

    monkeypatch.setattr(service, "execute", execute)
    service.run(once=True)
    assert seen == {"fast": ["one", "two"], "bmkg": ["one", "two"]}


def test_shutdown_kills_uncooperative_child(tmp_path):
    import time

    service = Runner([], tmp_path, [sys.executable])
    ready = tmp_path / "ready"
    command = (
        "import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    )
    thread = threading.Thread(target=service.execute, args=("fast", job("slow", ["-c", command])))
    thread.start()
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert ready.exists()
    service.shutdown_at = time.monotonic() - 20
    service.shutdown()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert records(tmp_path)[0]["exit_code"] == -signal.SIGKILL


def test_default_command_runs_the_cli(tmp_path):
    """The runner starts jobs as `python -m nowcast.cli`, so the module must run the app."""
    command = Runner([], tmp_path).command
    result = subprocess.run([*command, "--version"], capture_output=True, text=True, check=True)
    assert result.stdout.startswith("nowcast ")
