#!/usr/bin/env python3
"""Health check for ssh_probe_exporter.py — stdlib only.

Answers "is the SSH probe actually working right now", which the dashboard
can hide: a dead probe thread freezes its gauges at the last good value, so
the latency panels keep looking fine. This script reads
ssh_probe_last_run_timestamp_seconds to catch that, then per target reports
the last result of every probe (login / logout / cd / ls / poetry).

Checks:
  1. exporter    — /metrics reachable; if it is local, the process is running
  2. per target  — each probe ran recently (stale = loop dead or wedged),
                   last run succeeded, latest duration, failures since start
  3. Prometheus  — (only with --prometheus) the ssh-probe scrape job is up,
                   and every probed target string byte-matches an `instance`
                   used by some other job; otherwise cross-job joins and the
                   dashboard's $host variable silently miss that host

Exit status: 0 all OK, 1 warnings only, 2 at least one critical problem.

Usage:
  python3 probe/check_probe.py
  python3 probe/check_probe.py --exporter http://127.0.0.1:9754/metrics \\
      --prometheus http://127.0.0.1:9090
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

OK, WARN, CRIT = 0, 1, 2
TAG = {OK: "OK  ", WARN: "WARN", CRIT: "CRIT"}
SESSION_PROBES = ("connect", "logout")
LINE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

worst = OK


def report(level, msg):
    global worst
    worst = max(worst, level)
    print(f"  [{TAG[level]}] {msg}")


def fetch(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_metrics(text):
    """Returns {metric_name: [(labels_dict, value), ...]}."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        name, labels, value = m.groups()
        try:
            v = float(value)
        except ValueError:
            continue
        out.setdefault(name, []).append((dict(LABEL_RE.findall(labels or "")), v))
    return out


def probe_key(labels, metric):
    """Normalizes the per-probe identity: connect/logout or the command name."""
    if metric == "ssh_probe_last_run_timestamp_seconds":
        return labels.get("probe")
    if metric.startswith("ssh_probe_command_"):
        return labels.get("command")
    if metric.startswith("ssh_probe_connect_"):
        return "connect"
    if metric.startswith("ssh_probe_logout_"):
        return "logout"
    return None


def index_by_target(metrics):
    """{target: {probe: {"last_run", "success", "duration", "failures", "user"}}}"""
    field = {
        "ssh_probe_last_run_timestamp_seconds": "last_run",
        "ssh_probe_connect_success": "success",
        "ssh_probe_logout_success": "success",
        "ssh_probe_command_success": "success",
        "ssh_probe_connect_duration_seconds": "duration",
        "ssh_probe_logout_duration_seconds": "duration",
        "ssh_probe_command_duration_seconds": "duration",
        "ssh_probe_connect_failures_total": "failures",
        "ssh_probe_logout_failures_total": "failures",
        "ssh_probe_command_failures_total": "failures",
    }
    targets = {}
    for metric, key in field.items():
        for labels, value in metrics.get(metric, []):
            target, probe = labels.get("target"), probe_key(labels, metric)
            if target is None or probe is None:
                continue
            slot = targets.setdefault(target, {}).setdefault(probe, {})
            slot[key] = value
            slot["user"] = labels.get("user", "?")
    return targets


def check_process(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
        return  # remote exporter: its process table is not ours to read
    try:
        res = subprocess.run(["pgrep", "-af", "ssh_probe_exporter.py"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return
    procs = [l for l in res.stdout.splitlines() if "check_probe" not in l]
    if procs:
        for p in procs:
            report(OK, f"process: {p}")
    else:
        report(WARN, "no local ssh_probe_exporter.py process found "
                     "(fine if it runs in a container or under another name)")


def check_exporter(args):
    print(f"== exporter ({args.exporter}) ==")
    check_process(args.exporter)
    try:
        text = fetch(args.exporter, args.http_timeout)
    except Exception as e:
        report(CRIT, f"cannot read metrics: {e}")
        return None
    metrics = parse_metrics(text)
    targets = index_by_target(metrics)
    if not targets:
        report(CRIT, "reachable, but no ssh_probe_* series with a target label")
        return None
    report(OK, f"reachable, {len(targets)} target(s): {', '.join(sorted(targets))}")
    return targets


def stale_after(probe, args):
    # One full cycle can legitimately take interval + a timed-out ssh (the
    # session probe runs login and logout back to back, hence 2 timeouts).
    if probe in SESSION_PROBES:
        return args.stale_factor * args.connect_interval + 2 * args.timeout
    return args.stale_factor * args.command_interval + 2 * args.timeout


def check_targets(targets, args):
    now = time.time()
    for target in sorted(targets):
        probes = targets[target]
        user = next(iter(probes.values())).get("user", "?")
        print(f"== target {target} (as {user}) ==")
        order = [p for p in SESSION_PROBES if p in probes] + \
                sorted(p for p in probes if p not in SESSION_PROBES)
        for probe in order:
            s = probes[probe]
            name = "login" if probe == "connect" else probe
            fails = int(s.get("failures", 0))
            if "last_run" not in s:
                report(WARN, f"{name:<8} never ran yet (just started, or the loop "
                             f"is stuck in its first ssh) — failures={fails}")
                continue
            age = now - s["last_run"]
            limit = stale_after(probe, args)
            if age > limit:
                report(CRIT, f"{name:<8} STALE: last run {age:.0f}s ago (limit {limit:.0f}s) "
                             f"— probe loop dead or wedged; gauges are frozen")
                continue
            if s.get("success") == 1:
                dur = s.get("duration")
                dur_s = f"{dur * 1000:.0f} ms" if dur is not None else "n/a"
                level = WARN if dur is not None and dur > args.slow else OK
                slow = f" (> {args.slow:g}s)" if level == WARN else ""
                report(level, f"{name:<8} ok  {dur_s}{slow}, ran {age:.0f}s ago, "
                              f"failures since start={fails}")
            else:
                report(CRIT, f"{name:<8} FAILING (last run {age:.0f}s ago), "
                             f"failures since start={fails}")


def prom_query(base, expr, timeout):
    url = base.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    data = json.loads(fetch(url, timeout))
    if data.get("status") != "success":
        raise RuntimeError(data.get("error", "query failed"))
    return data["data"]["result"]


def check_prometheus(targets, args):
    print(f"== prometheus ({args.prometheus}) ==")
    try:
        up = prom_query(args.prometheus, f'up{{job="{args.job}"}}', args.http_timeout)
    except Exception as e:
        report(CRIT, f"cannot query Prometheus: {e}")
        return
    if not up:
        report(CRIT, f'no up{{job="{args.job}"}} series — job missing from prometheus.yml?')
    for r in up:
        inst = r["metric"].get("instance", "?")
        if r["value"][1] == "1":
            report(OK, f"scrape job {args.job} up (scrape address {inst})")
        else:
            report(CRIT, f"scrape job {args.job} DOWN for {inst} — Prometheus cannot "
                         f"reach the exporter (LISTEN must be 0.0.0.0/172.17.0.1 for docker)")

    # The scrape job relabels target -> instance; joins only work if that
    # string is exactly an instance some other job already uses.
    try:
        others = prom_query(args.prometheus, f'up{{job!="{args.job}"}}', args.http_timeout)
    except Exception as e:
        report(WARN, f"cannot list other jobs' instances: {e}")
        return
    by_instance = {}
    for r in others:
        by_instance.setdefault(r["metric"].get("instance", ""), set()).add(
            r["metric"].get("job", "?"))
    for target in sorted(targets):
        if target in by_instance:
            report(OK, f"{target} matches instance used by: "
                       f"{', '.join(sorted(by_instance[target]))}")
            continue
        near = sorted(i for i in by_instance if i.split(":")[0] == target.split(":")[0])
        hint = (f" — same host, different form: {', '.join(near)}"
                if near else " — no job scrapes this host under any form (typo?)")
        report(WARN, f"{target} matches no other job's instance; "
                     f"cross-job joins and $host will miss it{hint}")


def main():
    ap = argparse.ArgumentParser(description="Health check for ssh_probe_exporter.py")
    ap.add_argument("--exporter", default="http://127.0.0.1:9754/metrics",
                    help="probe exporter metrics URL (default %(default)s)")
    ap.add_argument("--prometheus", default="",
                    help="Prometheus base URL, e.g. http://127.0.0.1:9090 (optional)")
    ap.add_argument("--job", default="ssh-probe", help="scrape job name (default %(default)s)")
    ap.add_argument("--connect-interval", type=float, default=10,
                    help="must match SSH_PROBE_CONNECT_INTERVAL (default %(default)s)")
    ap.add_argument("--command-interval", type=float, default=5,
                    help="must match SSH_PROBE_COMMAND_INTERVAL (default %(default)s)")
    ap.add_argument("--timeout", type=float, default=10,
                    help="must match SSH_PROBE_TIMEOUT (default %(default)s)")
    ap.add_argument("--stale-factor", type=float, default=3,
                    help="intervals without a run before a probe counts as stale "
                         "(default %(default)s)")
    ap.add_argument("--slow", type=float, default=2.0,
                    help="warn when a successful probe took longer than this many "
                         "seconds (default %(default)s)")
    ap.add_argument("--http-timeout", type=float, default=5,
                    help="HTTP timeout for exporter/Prometheus requests (default %(default)s)")
    args = ap.parse_args()

    targets = check_exporter(args)
    if targets:
        check_targets(targets, args)
        if args.prometheus:
            check_prometheus(targets, args)
    print(f"== overall: {TAG[worst].strip()} ==")
    sys.exit(worst)


if __name__ == "__main__":
    main()
