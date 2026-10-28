#!/usr/bin/env python3
"""Run this directory's scripts on the calendar in schedule.conf, via launchd.

Each line of schedule.conf becomes a macOS LaunchAgent
(~/Library/LaunchAgents/com.fetlifetools.<script>.plist) that runs the
script with /bin/bash in this directory, with .venv/bin on its PATH, writing
its output to logs/<script>.log.

Runs aren't lost while the Mac is off: launchd starts a job when it wakes
from sleep after a missed time, and every job also starts at login. Either
way, the job runs its script only if a scheduled time has passed since it
last ran (logs/.<label>.last), so a run missed while the Mac was shut down
happens at the next login, once. Several missed times make one run.

    python3 schedule.py check            show what schedule.conf schedules (changes nothing)
    python3 schedule.py install          schedule exactly what schedule.conf lists
    python3 schedule.py status           each scheduled job: loaded? last exit code?
    python3 schedule.py run SCRIPT       run a scheduled job now, the way launchd will
    python3 schedule.py uninstall        remove every job this file scheduled

`install` also removes the jobs of lines since deleted from schedule.conf,
so rerun it after every edit.

A line with both a day and a weekday (1-7 * mon, the first Monday) needs
both to match. launchd would start it when either does, so it only gets the
weekday, and the job's own check of the schedule skips the other days.
"""

from __future__ import annotations

import itertools
import os
import plistlib
import re
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = HERE / "schedule.conf"
LOGS = HERE / "logs"
AGENTS = Path.home() / "Library" / "LaunchAgents"
PREFIX = "com.fetlifetools."
PATH = os.pathsep.join([str(HERE / ".venv" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
                        "/usr/bin", "/bin", "/usr/sbin", "/sbin"])

MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
WEEKDAYS = "sun mon tue wed thu fri sat".split()
# (launchd key, lowest, highest, names for lowest..)
FIELDS = [
    ("Minute", 0, 59, None),
    ("Hour", 0, 23, None),
    ("Day", 1, 31, None),
    ("Month", 1, 12, MONTHS),
    ("Weekday", 0, 7, WEEKDAYS),
]
MAX_TIMES = 1000  # launchd keeps one entry per matching combination
NETWORK_HOST = ("fetlife.com", 443)
NETWORK_WAIT = 180  # seconds a job waits for the network after login or wake


class ScheduleError(Exception):
    pass


@dataclass
class Job:
    line: int
    label: str
    script: Path
    args: list[str]
    times: list[dict]  # StartCalendarInterval entries
    spec: str
    fields: dict[str, list[int] | None]  # the full schedule, None for *

    @property
    def plist_path(self) -> Path:
        return AGENTS / f"{self.label}.plist"

    @property
    def log_path(self) -> Path:
        return LOGS / f"{self.label[len(PREFIX):]}.log"

    @property
    def run_now_flag(self) -> Path:
        """Left by `schedule.py run` so the next start runs whether or not a time is due."""
        return LOGS / f".{self.label}.run-now"

    @property
    def stamp_path(self) -> Path:
        """When the script last started."""
        return LOGS / f".{self.label}.last"

    def last_run(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.stamp_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def mark_run(self, when: datetime) -> None:
        self.stamp_path.write_text(when.isoformat(timespec="seconds") + "\n")

    def latest_time(self, now: datetime, since: datetime) -> datetime | None:
        """The latest scheduled time after *since* and no later than *now*."""
        f = self.fields
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        while day + timedelta(days=1) > since:
            weekday = (day.weekday() + 1) % 7  # cron counts from Sunday
            if ((f["Month"] is None or day.month in f["Month"])
                    and (f["Day"] is None or day.day in f["Day"])
                    and (f["Weekday"] is None or weekday in {w % 7 for w in f["Weekday"]})):
                for hour in reversed(f["Hour"] or range(24)):
                    for minute in reversed(f["Minute"] or range(60)):
                        t = day.replace(hour=hour, minute=minute)
                        if t <= since:
                            return None
                        if t <= now:
                            return t
            day -= timedelta(days=1)
        return None

    def program(self) -> list[str]:
        return [sys.executable, str(Path(__file__).resolve()), "fire", self.label]

    def plist(self) -> dict:
        return {
            "Label": self.label,
            "ProgramArguments": self.program(),
            "WorkingDirectory": str(HERE),
            "EnvironmentVariables": {"PATH": PATH},
            "StartCalendarInterval": self.times if len(self.times) > 1 else self.times[0],
            "RunAtLoad": True,  # catch up at login on a time missed while shut down
            "StandardOutPath": str(self.log_path),
            "StandardErrorPath": str(self.log_path),
        }


def _number(text: str, low: int, names: list[str] | None) -> int:
    if names and text.lower() in names:
        return low + names.index(text.lower())
    if not text.isdigit():
        raise ScheduleError(f"{text!r} isn't a number" + (f" or one of {', '.join(names)}" if names else ""))
    return int(text)


def parse_field(text: str, low: int, high: int, names: list[str] | None) -> list[int] | None:
    """The values *text* allows, or None for any (``*``)."""
    if text == "*":
        return None
    values: set[int] = set()
    for part in text.split(","):
        part, _, step_text = part.partition("/")
        step = _number(step_text, 0, None) if step_text else 1
        if step < 1:
            raise ScheduleError(f"step {step_text!r} must be at least 1")
        if part == "*":
            first, last = low, high
        elif "-" in part:
            a, _, b = part.partition("-")
            first, last = _number(a, low, names), _number(b, low, names)
        else:
            first = _number(part, low, names)
            last = high if step_text else first
        if not (low <= first <= high and low <= last <= high) or first > last:
            raise ScheduleError(f"{part!r} must be within {low}-{high}")
        values.update(range(first, last + 1, step))
    return sorted(values)


def parse_conf(path: Path = CONF) -> list[Job]:
    jobs: list[Job] = []
    labels: dict[str, int] = {}
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            words = shlex.split(line)
            if len(words) < 6:
                raise ScheduleError("expected: minute hour day month weekday script [arguments]")
            choices = []
            for text, (key, low, high, names) in zip(words[:5], FIELDS):
                try:
                    choices.append((key, parse_field(text, low, high, names)))
                except ScheduleError as e:
                    raise ScheduleError(f"{key.lower()}: {e}") from None
            script = (HERE / words[5]).resolve()
            if not script.is_file():
                raise ScheduleError(f"no such script: {words[5]}")
            fields = dict(choices)
            if fields["Day"] is not None and fields["Weekday"] is not None:
                # launchd would start on either; give it the weekday, check the day.
                choices = [(key, None if key == "Day" else vals) for key, vals in choices]
            limited = [(key, vals) for key, vals in choices if vals is not None]
            times = [dict(zip([k for k, _ in limited], combo))
                     for combo in itertools.product(*[vals for _, vals in limited])] or [{}]
            if len(times) > MAX_TIMES:
                raise ScheduleError(f"matches {len(times)} times per cycle; keep it under {MAX_TIMES}")
            if times == [{}]:
                raise ScheduleError("* * * * * would run it every minute; give at least a minute")
        except (ScheduleError, ValueError) as e:
            raise ScheduleError(f"{path.name} line {n}: {e}\n    {raw}") from None
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", script.stem)
        labels[stem] = labels.get(stem, 0) + 1
        label = PREFIX + stem + (f"-{labels[stem]}" if labels[stem] > 1 else "")
        jobs.append(Job(n, label, script, words[6:], times, " ".join(words[:5]), fields))
    return jobs


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _installed() -> list[Path]:
    return sorted(AGENTS.glob(PREFIX + "*.plist"))


def _unload(plist: Path) -> None:
    _launchctl("bootout", _domain(), str(plist))


def cmd_check(jobs: list[Job]) -> None:
    if not jobs:
        print(f"{CONF.name} schedules nothing.")
    for job in jobs:
        print(f"{job.spec:<20} {job.script.relative_to(HERE)} {' '.join(job.args)}".rstrip()
              + f"\n{'':<20} → {job.label}, {len(job.times)} time{'s' if len(job.times) != 1 else ''}"
                f" per cycle, log {job.log_path.relative_to(HERE)}"
              + (f"\n{'':<20}   (launchd starts it on the weekday; it runs only on day"
                 f" {','.join(map(str, job.fields['Day']))} of the month)"
                 if job.fields["Day"] is not None and job.fields["Weekday"] is not None else ""))


def cmd_install(jobs: list[Job]) -> None:
    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    wanted = {job.plist_path for job in jobs}
    for old in _installed():
        if old not in wanted:
            _unload(old)
            old.unlink()
            print(f"removed   {old.stem}")
    now = datetime.now()
    for job in jobs:
        _unload(job.plist_path)  # a reload picks up the new schedule
        job.run_now_flag.unlink(missing_ok=True)
        if job.last_run() is None:
            job.mark_run(now)  # a new job starts with the next time, not a past one
        with open(job.plist_path, "wb") as fh:
            plistlib.dump(job.plist(), fh)
        result = _launchctl("bootstrap", _domain(), str(job.plist_path))
        if result.returncode != 0:
            raise ScheduleError(f"launchctl couldn't load {job.label}: {result.stderr.strip()}")
        print(f"scheduled {job.label}  ({job.spec}  {job.script.name})")
    if not jobs:
        print(f"{CONF.name} schedules nothing; no jobs are installed.")


def cmd_uninstall() -> None:
    for plist in _installed():
        _unload(plist)
        plist.unlink()
        print(f"removed   {plist.stem}")


def cmd_status(jobs: list[Job]) -> None:
    listed = {job.label for job in jobs}
    for plist in _installed():
        out = _launchctl("print", f"{_domain()}/{plist.stem}").stdout
        loaded = bool(out)
        exit_code = re.search(r"last exit code = (.+)", out)
        state = re.search(r"\n\s*state = (.+)", out)
        last = (LOGS / f".{plist.stem}.last")
        last = last.read_text().strip().replace("T", " ")[:16] if last.exists() else "never"
        print(f"{plist.stem:<45} {'loaded' if loaded else 'NOT loaded':<11}"
              f" {('state ' + state.group(1)) if state else '':<16}"
              f" last run {last:<17}"
              f" {('last exit ' + exit_code.group(1)) if exit_code else ''}"
              + ("" if plist.stem in listed else "   (no longer in schedule.conf: run install)"))
    for label in sorted(listed - {p.stem for p in _installed()}):
        print(f"{label:<45} not installed (run install)")


def cmd_run(jobs: list[Job], name: str) -> None:
    matches = [j for j in jobs if name in (j.label, j.label[len(PREFIX):], j.script.name, j.script.stem)]
    if not matches:
        raise ScheduleError(f"{name!r} isn't scheduled in {CONF.name}")
    for job in matches:
        LOGS.mkdir(exist_ok=True)
        job.run_now_flag.touch()
        result = _launchctl("kickstart", f"{_domain()}/{job.label}")
        if result.returncode != 0:
            raise ScheduleError(f"couldn't start {job.label} (is it installed?): {result.stderr.strip()}")
        print(f"started {job.label}; output in {job.log_path.relative_to(HERE)}")


def _wait_for_network() -> None:
    """Right after login or wake the Wi-Fi may not be up yet; give it a few minutes."""
    deadline = time.monotonic() + NETWORK_WAIT
    while True:
        try:
            socket.create_connection(NETWORK_HOST, timeout=10).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                print(f"still no network after {NETWORK_WAIT}s; running anyway", flush=True)
                return
            time.sleep(10)


def cmd_fire(jobs: list[Job], label: str) -> None:
    """launchd's entry point: run the script if a scheduled time has passed since it last ran."""
    job = next((j for j in jobs if j.label == label), None)
    if job is None:
        raise ScheduleError(f"{label} isn't in {CONF.name} any more; run install")
    now = datetime.now()
    if job.run_now_flag.exists():
        job.run_now_flag.unlink()
        why = "run by hand"
    else:
        last = job.last_run()
        if last is None:  # never ran here: count from now
            job.mark_run(now)
            return
        due = job.latest_time(now, last)
        if due is None:
            return
        why = (f"scheduled {due:%a %b %-d %H:%M}"
               + (", missed while the Mac was off or asleep" if now - due > timedelta(minutes=5) else ""))
    job.mark_run(now)  # before running, so a failing script doesn't rerun at every login
    print(f"--- {now:%Y-%m-%d %H:%M} {job.script.name} ({why})", flush=True)
    _wait_for_network()
    os.execv("/bin/bash", ["/bin/bash", str(job.script), *job.args])


def main(argv: list[str]) -> int:
    commands = ("check", "install", "status", "run", "uninstall", "fire")
    if (not argv or argv[0] not in commands or (argv[0] in ("run", "fire")) != (len(argv) == 2)
            or len(argv) > 2):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        if argv[0] == "uninstall":
            cmd_uninstall()
            return 0
        jobs = parse_conf()
        if argv[0] == "check":
            cmd_check(jobs)
        elif argv[0] == "install":
            cmd_install(jobs)
        elif argv[0] == "status":
            cmd_status(jobs)
        elif argv[0] == "fire":
            cmd_fire(jobs, argv[1])
        else:
            cmd_run(jobs, argv[1])
    except ScheduleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
