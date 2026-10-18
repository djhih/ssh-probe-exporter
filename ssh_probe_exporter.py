#!/usr/bin/env python3
"""SSH latency probe exporter — long-running HTTP server, stdlib only
(plus the `sshpass` binary when password auth is configured).

Runs on the MONITORING host (next to Prometheus), NOT on the targets: it
measures each target from the outside, the way a user at a login prompt
would experience it. Per target, two background loops:

 - session probe (every SSH_PROBE_CONNECT_INTERVAL, default 10s):
   opens a REAL login shell with multiplexing force-disabled and times
   two phases separately: login (process start until the remote shell
   answered our first command — TCP + key exchange + auth + shell
   startup) and logout (sending `exit` until the ssh session fully
   terminated — channel close + session teardown). This is "how long
   until a new login works" and "how long until exit gives my prompt
   back".
 - command probe (every SSH_PROBE_COMMAND_INTERVAL, default 5s):
   times `cd`, `ls` and a poetry invocation on the target, run over a
   shared ControlMaster connection. The master is primed OUTSIDE the
   timed section, so these samples measure remote command latency only —
   connection setup is the connect probe's job, not double-counted here.

GET /metrics renders the most recent measurements in Prometheus text
format; every series carries a `target` label, and the scrape config
copies it into `instance` (one exporter, many probed hosts). Durations
are emitted only for successful runs — absent means "failed or not
measured yet", never zero (same honesty rule as exporter.py). Failures
are visible via *_success gauges and *_failures_total counters instead.

Auth: with SSH_PASSWORD empty, key-based auth (BatchMode — keys must
already be set up). With SSH_PASSWORD set, password auth via sshpass;
the password is passed through the environment (SSHPASS), never argv,
so it does not show up in `ps`.

Env knobs (deployment details only — WHO and WHERE to probe are the
fixed constants below):
  SSH_PROBE_LISTEN            default 127.0.0.1:9754; set 0.0.0.0:9754 so
                              the dockerized Prometheus can reach it
  SSH_PROBE_CONNECT_INTERVAL  seconds, default 10
  SSH_PROBE_COMMAND_INTERVAL  seconds, default 5
  SSH_PROBE_TIMEOUT           per-ssh timeout seconds, default 10
  SSH_PROBE_WORKDIR           remote dir for cd/ls/poetry; absolute path,
                              empty (default) = the account's home
  SSH_PROBE_POETRY_CMD        default `poetry --version`; note that a
                              non-interactive ssh shell may not have
                              ~/.local/bin on PATH — use a full path then
  SSH_PROBE_MULTIPLEX         0 disables ControlMaster (each command then
                              pays full connection setup)
  SSH_PROBE_CONTROL_PATH      default ~/.ssh/ssh-probe-%C (%C hashes
                              user+host+port, so targets never share a
                              master)
"""
import os
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Fixed probe configuration — edit in place ────────────────────────────────
# TARGETS: the hosts to probe. Use EXACTLY the strings the other scrape jobs
# use as `instance` (hostname vs IP matters): the scrape config copies
# `target` into `instance`, and cross-job joins match instance byte-for-byte.
#
# SSH_PASSWORD: empty string = key-based auth (BatchMode). Non-empty =
# password auth via sshpass (must be installed: `apt install sshpass`).
# The values below are PLACEHOLDERS. This repo is public on GitHub — put the
# real password in your local checkout only and NEVER commit or push it.
TARGETS = [
    "gpu-node-1",
    # "192.0.2.11",
]
SSH_USER = "probeuser"
SSH_PASSWORD = ""
# ─────────────────────────────────────────────────────────────────────────────


def env_float(name, default):
    v = os.environ.get(name, default)
    try:
        f = float(v)
    except ValueError:
        raise SystemExit(f"invalid {name}={v!r}, expected a number")
    if f <= 0:
        raise SystemExit(f"invalid {name}={v!r}, expected > 0")
    return f


LISTEN = os.environ.get("SSH_PROBE_LISTEN", "127.0.0.1:9754")
CONNECT_INTERVAL = env_float("SSH_PROBE_CONNECT_INTERVAL", "10")
COMMAND_INTERVAL = env_float("SSH_PROBE_COMMAND_INTERVAL", "5")
TIMEOUT = env_float("SSH_PROBE_TIMEOUT", "10")
WORKDIR = os.environ.get("SSH_PROBE_WORKDIR", "")
POETRY_CMD = os.environ.get("SSH_PROBE_POETRY_CMD", "poetry --version")
MULTIPLEX = os.environ.get("SSH_PROBE_MULTIPLEX", "1") != "0"
CONTROL_PATH = os.environ.get("SSH_PROBE_CONTROL_PATH", "~/.ssh/ssh-probe-%C")

if SSH_PASSWORD:
    # BatchMode would refuse password prompts, so it only guards key mode.
    # accept-new because password-auth hosts rarely have pre-seeded
    # known_hosts entries; changed (possibly spoofed) keys still fail hard.
    AUTH_OPTS = [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "NumberOfPasswordPrompts=1",
    ]
else:
    AUTH_OPTS = ["-o", "BatchMode=yes"]

BASE_OPTS = [*AUTH_OPTS, "-o", f"ConnectTimeout={int(TIMEOUT)}"]
# -S none overrides any user ssh_config ControlPath, forcing a fresh
# connection — without it the connect probe could silently measure a
# multiplexed no-op and report microseconds.
FRESH_OPTS = ["-S", "none", "-o", "ControlMaster=no"]
MUX_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={CONTROL_PATH}",
    "-o", "ControlPersist=600",
]


def ssh_argv(extra_opts, target, *tail):
    argv = ["sshpass", "-e"] if SSH_PASSWORD else []
    return [*argv, "ssh", *BASE_OPTS, *extra_opts, f"{SSH_USER}@{target}", *tail]


def ssh_env():
    if not SSH_PASSWORD:
        return None
    return {**os.environ, "SSHPASS": SSH_PASSWORD}


def remote_commands():
    """The three command probes. WORKDIR is shell-quoted verbatim, so it
    must be an absolute path; empty means the login home directory."""
    if WORKDIR:
        wd = shlex.quote(WORKDIR)
        return [
            ("cd", f"cd -- {wd}"),
            ("ls", f"cd -- {wd} && ls"),
            ("poetry", f"cd -- {wd} && {POETRY_CMD}"),
        ]
    return [("cd", "cd"), ("ls", "ls"), ("poetry", POETRY_CMD)]


COMMANDS = remote_commands()


def empty_slot():
    return {"duration": None, "success": None, "last_run": None, "failures": 0}


LOCK = threading.Lock()
STATE = {
    target: {
        "connect": empty_slot(),
        "logout": empty_slot(),
        "commands": {name: empty_slot() for name, _ in COMMANDS},
    }
    for target in TARGETS
}


def log(msg):
    print(f"[ssh-probe] {msg}", file=sys.stderr, flush=True)


def timed_ssh(extra_opts, target, command):
    """Run one ssh and return (success, duration_seconds, error_summary)."""
    t0 = time.monotonic()
    try:
        # Slack over TIMEOUT: ConnectTimeout only covers TCP setup, and a
        # hung remote command must not wedge the probe loop forever.
        r = subprocess.run(
            ssh_argv(extra_opts, target, command),
            capture_output=True, text=True, env=ssh_env(), timeout=TIMEOUT + 5,
        )
    except subprocess.TimeoutExpired:
        return False, None, f"timeout after {TIMEOUT + 5:.0f}s"
    duration = time.monotonic() - t0
    if r.returncode != 0:
        detail = r.stderr.strip().splitlines()
        return False, None, detail[-1] if detail else f"exit {r.returncode}"
    return True, duration, ""


def ensure_master(target):
    """Prime the shared connection outside the timed section, so command
    samples never include connection setup. Best-effort: on failure the
    commands still run (ControlMaster=auto retries) and fail visibly."""
    check = subprocess.run(
        ["ssh", "-o", f"ControlPath={CONTROL_PATH}", "-O", "check",
         f"{SSH_USER}@{target}"],
        capture_output=True,
    )
    if check.returncode == 0:
        return
    try:
        subprocess.run(
            ssh_argv(MUX_OPTS, target, "-fN"),
            capture_output=True, env=ssh_env(), timeout=TIMEOUT + 5,
        )
    except subprocess.TimeoutExpired:
        pass


def record(slot, success, duration, error, what):
    with LOCK:
        slot["last_run"] = time.time()
        prev = slot["success"]
        slot["success"] = success
        slot["duration"] = duration if success else None
        if not success:
            slot["failures"] += 1
    if success and prev is False:
        log(f"{what}: recovered")
    elif not success and prev is not False:
        log(f"{what}: failed ({error})")


READY_MARKER = "__SSH_PROBE_READY__"


def session_probe(target):
    """One real login+logout round trip, both phases timed separately.
    Returns ((login_ok, login_dur, err), (logout_ok, logout_dur, err)).

    ssh gets no remote command, so sshd starts the account's login shell;
    with stdin a pipe the shell runs non-interactively and executes the
    lines we feed it. Login is measured to our marker echoing back (the
    shell is provably ready for commands, banners can't fool it); logout
    is measured from writing `exit` to the ssh process exiting.
    """
    deadline = time.monotonic() + TIMEOUT + 5
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            ssh_argv(FRESH_OPTS, target),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=ssh_env(),
        )
        proc.stdin.write(f"echo {READY_MARKER}\n".encode())
        proc.stdin.flush()
    except (OSError, BrokenPipeError) as e:
        fail = (False, None, str(e))
        return fail, (False, None, "login failed")

    login = None
    fd = proc.stdout.fileno()
    buf = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], deadline - time.monotonic())
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk
        if READY_MARKER.encode() in buf:
            login = (True, time.monotonic() - t0, "")
            break
    if login is None:
        proc.kill()
        proc.wait()
        return (False, None, "no shell within timeout"), (False, None, "login failed")

    t1 = time.monotonic()
    try:
        proc.stdin.write(b"exit\n")
        proc.stdin.flush()
        proc.stdin.close()
    except (OSError, BrokenPipeError):
        pass
    try:
        rc = proc.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return login, (False, None, "session did not close within timeout")
    if rc != 0:
        return login, (False, None, f"exit status {rc}")
    return login, (True, time.monotonic() - t1, "")


def connect_loop(target):
    while True:
        t0 = time.monotonic()
        login, logout = session_probe(target)
        record(STATE[target]["connect"], *login, f"login {SSH_USER}@{target}")
        record(STATE[target]["logout"], *logout, f"logout {SSH_USER}@{target}")
        time.sleep(max(0.0, CONNECT_INTERVAL - (time.monotonic() - t0)))


def command_loop(target):
    opts = MUX_OPTS if MULTIPLEX else FRESH_OPTS
    while True:
        t0 = time.monotonic()
        if MULTIPLEX:
            ensure_master(target)
        for name, cmd in COMMANDS:
            success, duration, error = timed_ssh(opts, target, cmd)
            record(STATE[target]["commands"][name], success, duration, error,
                   f"command {name!r} on {target}")
        time.sleep(max(0.0, COMMAND_INTERVAL - (time.monotonic() - t0)))


def render():
    with LOCK:
        snap = {
            target: {
                "connect": dict(s["connect"]),
                "logout": dict(s["logout"]),
                "commands": {n: dict(c) for n, c in s["commands"].items()},
            }
            for target, s in STATE.items()
        }
    out = []

    out.append("# HELP ssh_probe_connect_duration_seconds Fresh login: ssh start until the remote shell answered our first command (TCP + key exchange + auth + shell startup), multiplexing disabled. Absent while failing.")
    out.append("# TYPE ssh_probe_connect_duration_seconds gauge")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        if s["connect"]["duration"] is not None:
            out.append(f"ssh_probe_connect_duration_seconds{{{base}}} {s['connect']['duration']:.6f}")
    out.append("# HELP ssh_probe_connect_success Whether the last connect probe succeeded (absent until the first run).")
    out.append("# TYPE ssh_probe_connect_success gauge")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        if s["connect"]["success"] is not None:
            out.append(f"ssh_probe_connect_success{{{base}}} {int(s['connect']['success'])}")
    out.append("# HELP ssh_probe_connect_failures_total Failed connect probes since exporter start.")
    out.append("# TYPE ssh_probe_connect_failures_total counter")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        out.append(f"ssh_probe_connect_failures_total{{{base}}} {s['connect']['failures']}")

    out.append("# HELP ssh_probe_logout_duration_seconds Logout: sending `exit` until the ssh session fully terminated (channel close + teardown). Absent while failing.")
    out.append("# TYPE ssh_probe_logout_duration_seconds gauge")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        if s["logout"]["duration"] is not None:
            out.append(f"ssh_probe_logout_duration_seconds{{{base}}} {s['logout']['duration']:.6f}")
    out.append("# HELP ssh_probe_logout_success Whether the last logout probe succeeded (0 also while login itself is failing; absent until the first run).")
    out.append("# TYPE ssh_probe_logout_success gauge")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        if s["logout"]["success"] is not None:
            out.append(f"ssh_probe_logout_success{{{base}}} {int(s['logout']['success'])}")
    out.append("# HELP ssh_probe_logout_failures_total Failed logout probes since exporter start (includes cycles where login already failed).")
    out.append("# TYPE ssh_probe_logout_failures_total counter")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        out.append(f"ssh_probe_logout_failures_total{{{base}}} {s['logout']['failures']}")

    out.append("# HELP ssh_probe_command_duration_seconds Wall time of one remote command over the multiplexed connection (setup cost excluded). Absent while failing.")
    out.append("# TYPE ssh_probe_command_duration_seconds gauge")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        for name, c in s["commands"].items():
            if c["duration"] is not None:
                out.append(f'ssh_probe_command_duration_seconds{{{base},command="{name}"}} {c["duration"]:.6f}')
    out.append("# HELP ssh_probe_command_success Whether the last run of this command succeeded (absent until the first run).")
    out.append("# TYPE ssh_probe_command_success gauge")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        for name, c in s["commands"].items():
            if c["success"] is not None:
                out.append(f'ssh_probe_command_success{{{base},command="{name}"}} {int(c["success"])}')
    out.append("# HELP ssh_probe_command_failures_total Failed runs of this command since exporter start.")
    out.append("# TYPE ssh_probe_command_failures_total counter")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        for name, c in s["commands"].items():
            out.append(f'ssh_probe_command_failures_total{{{base},command="{name}"}} {c["failures"]}')

    out.append("# HELP ssh_probe_last_run_timestamp_seconds Unix time the probe last ran — detects a dead probe loop, which frozen gauges alone would hide.")
    out.append("# TYPE ssh_probe_last_run_timestamp_seconds gauge")
    for target, s in snap.items():
        base = f'target="{target}",user="{SSH_USER}"'
        if s["connect"]["last_run"] is not None:
            out.append(f'ssh_probe_last_run_timestamp_seconds{{{base},probe="connect"}} {s["connect"]["last_run"]:.3f}')
        if s["logout"]["last_run"] is not None:
            out.append(f'ssh_probe_last_run_timestamp_seconds{{{base},probe="logout"}} {s["logout"]["last_run"]:.3f}')
        for name, c in s["commands"].items():
            if c["last_run"] is not None:
                out.append(f'ssh_probe_last_run_timestamp_seconds{{{base},probe="{name}"}} {c["last_run"]:.3f}')

    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/metrics"):
            self.send_error(404)
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # per-scrape access logs are noise


def main():
    if not TARGETS:
        raise SystemExit("TARGETS is empty — edit the fixed configuration block")
    if SSH_PASSWORD and shutil.which("sshpass") is None:
        raise SystemExit("SSH_PASSWORD is set but sshpass is not installed "
                         "(apt install sshpass)")
    host, _, port = LISTEN.rpartition(":")
    try:
        port = int(port)
    except ValueError:
        raise SystemExit(f"invalid SSH_PROBE_LISTEN={LISTEN!r}, expected host:port")
    for target in TARGETS:
        threading.Thread(target=connect_loop, args=(target,), daemon=True,
                         name=f"connect-{target}").start()
        threading.Thread(target=command_loop, args=(target,), daemon=True,
                         name=f"command-{target}").start()
    server = ThreadingHTTPServer((host, port), Handler)
    log(
        f"listening on {LISTEN}, probing {len(TARGETS)} target(s) as {SSH_USER} "
        f"({'password' if SSH_PASSWORD else 'key'} auth, "
        f"connect every {CONNECT_INTERVAL:g}s, commands every {COMMAND_INTERVAL:g}s, "
        f"multiplex={'on' if MULTIPLEX else 'off'}): " + ", ".join(TARGETS)
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
