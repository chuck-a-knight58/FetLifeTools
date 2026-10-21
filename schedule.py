#!/usr/bin/env python3
"""Run this directory's scripts on the calendar in schedule.conf, via launchd.

Each line of schedule.conf becomes a macOS LaunchAgent
(~/Library/LaunchAgents/com.fetlifetools.<script>.plist) that runs the
script with /bin/bash in this directory, with .venv/bin on its PATH, writing
its output to logs/<script>.log.

    python3 schedule.py check            show what schedule.conf schedules (changes nothing)
    python3 schedule.py install          schedule exactly what schedule.conf lists
    python3 schedule.py status           each scheduled job: loaded? last exit code?
    python3 schedule.py run SCRIPT       run a scheduled job now, the way launchd will
    python3 schedule.py uninstall        remove every job this file scheduled

`install` also removes the jobs of lines since deleted from schedule.conf,
so rerun it after every edit.

launchd runs a job when *either* its day or its weekday matches. For a line
that sets both (1-7 * mon, the first Monday), launchd only gets the weekday
and the job checks the day of the month itself when it starts.
"""

from __future__ import annotations

import itertools
import os
import plistlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
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
    days: list[int] | None = None  # days of the month the job checks for itself

    @property
    def plist_path(self) -> Path:
        return AGENTS / f"{self.label}.plist"

    @property
    def log_path(self) -> Path:
        return LOGS / f"{self.label[len(PREFIX):]}.log"

    @property
    def run_now_flag(self) -> Path:
        """Left by `schedule.py run` so the next start skips the day check."""
        return LOGS / f".{self.label}.run-now"

    def program(self) -> list[str]:
        if not self.days:
            return ["/bin/bash", str(self.script), *self.args]
        flag = shlex.quote(str(self.run_now_flag))
        guard = (f'if [ -e {flag} ]; then rm -f {flag}; '
                 f'else case " {" ".join(map(str, self.days))} " in *" $(date +%-d) "*) ;; *) exit 0;; esac; fi; '
                 'exec /bin/bash "$0" "$@"')
        return ["/bin/bash", "-c", guard, str(self.script), *self.args]

    def plist(self) -> dict:
        return {
            "Label": self.label,
            "ProgramArguments": self.program(),
            "WorkingDirectory": str(HERE),
            "EnvironmentVariables": {"PATH": PATH},
            "StartCalendarInterval": self.times if len(self.times) > 1 else self.times[0],
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
            days = None
            fields = dict(choices)
            if fields["Day"] is not None and fields["Weekday"] is not None:
                # launchd would start on either; give it the weekday, check the day.
                days = fields["Day"]
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
        jobs.append(Job(n, label, script, words[6:], times, " ".join(words[:5]), days))
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
                 f" {','.join(map(str, job.days))} of the month)" if job.days else ""))


def cmd_install(jobs: list[Job]) -> None:
    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    wanted = {job.plist_path for job in jobs}
    for old in _installed():
        if old not in wanted:
            _unload(old)
            old.unlink()
            print(f"removed   {old.stem}")
    for job in jobs:
        _unload(job.plist_path)  # a reload picks up the new schedule
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
        print(f"{plist.stem:<45} {'loaded' if loaded else 'NOT loaded':<11}"
              f" {('state ' + state.group(1)) if state else '':<16}"
              f" {('last exit ' + exit_code.group(1)) if exit_code else ''}"
              + ("" if plist.stem in listed else "   (no longer in schedule.conf: run install)"))
    for label in sorted(listed - {p.stem for p in _installed()}):
        print(f"{label:<45} not installed (run install)")


def cmd_run(jobs: list[Job], name: str) -> None:
    matches = [j for j in jobs if name in (j.label, j.label[len(PREFIX):], j.script.name, j.script.stem)]
    if not matches:
        raise ScheduleError(f"{name!r} isn't scheduled in {CONF.name}")
    for job in matches:
        if job.days:
            LOGS.mkdir(exist_ok=True)
            job.run_now_flag.touch()
        result = _launchctl("kickstart", f"{_domain()}/{job.label}")
        if result.returncode != 0:
            raise ScheduleError(f"couldn't start {job.label} (is it installed?): {result.stderr.strip()}")
        print(f"started {job.label}; output in {job.log_path.relative_to(HERE)}")


def main(argv: list[str]) -> int:
    commands = ("check", "install", "status", "run", "uninstall")
    if not argv or argv[0] not in commands or (argv[0] == "run") != (len(argv) == 2) or len(argv) > 2:
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
        else:
            cmd_run(jobs, argv[1])
    except ScheduleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
