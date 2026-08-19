#!/usr/bin/env python3
"""Run the support matrix on this machine.

support.json says which databases Sustained claims to run against. This
script proves the claim: it starts each server, runs the integration suite
against it, and prints one line per server. No arguments runs every server
that this machine can serve.

    python3 matrix.py                      # every server
    python3 matrix.py postgres mysql       # only these
    python3 matrix.py python               # the unit suite on each interpreter
    python3 matrix.py --check              # what would run, and what is missing

Containers come from docker/compose.yaml and are removed afterwards, unless
--keep. Set a server's connection variable, for example
SUSTAINED_TEST_POSTGRES_DSN, and that server is used as given and no
container is started.

Exit codes: 0 every selected target ran clean, 1 a failure, 2 nothing
failed but something was skipped.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUPPORT = json.loads((ROOT / "support.json").read_text())
ROWS = {row["name"]: row for row in SUPPORT["databases"]}
SERVERS = [name for name, row in ROWS.items() if row["server"] != "none"]
PYTHON = "python"
TARGETS = SERVERS + [PYTHON]

COMPOSE_FILE = ROOT / "docker" / "compose.yaml"
# Wide enough for the longest name a line can carry, so the columns hold
# while the lines are printed one at a time.
LABELS = SERVERS + [f"python{version}" for version in SUPPORT["python"]["versions"]]
NAME_WIDTH = max(len(label) for label in LABELS) + 2
HEALTHY_TIMEOUT = 300
RAN = re.compile(r"^Ran (\d+) test", re.MULTILINE)
BROKEN = re.compile(r"(failures|errors)=(\d+)")

# One result per target. `state` is what the line says it did.
RAN_OK = "ran"
FAILED = "failed"
WAITING = "waiting"
READY = "ready"


class Result:
    def __init__(self, target, state, detail):
        self.target = target
        self.state = state
        self.detail = detail
        self.output = ""


def docker_ready():
    """True when a docker daemon is up and can take a compose command."""
    if not shutil.which("docker"):
        return False
    done = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return done.returncode == 0


def installed(module):
    if not module:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def missing_reason(name, have_docker):
    """
    Why a server cannot run here, or None when it can. The order matters:
    a driver that is not installed is the developer's next step whether or
    not the server itself is reachable.
    """
    row = ROWS[name]
    if not installed(row["module"]):
        return f"the {row['module']} driver is missing. Install {row['driver']}"
    given = os.environ.get(row["dsn_env"] or "")
    if row["server"] == "account" and not given:
        return (
            f"{row['dsn_env']} is not set. Point it at a staging S3 directory, "
            "and name a profile with --athena-profile"
        )
    if row["server"] == "container" and not given and not have_docker:
        return "docker is not running, and no connection variable is set"
    return None


def services_for(names):
    """The compose services needed by these servers, in support.json order."""
    wanted = []
    for name in names:
        row = ROWS[name]
        if row["server"] != "container" or os.environ.get(row["dsn_env"] or ""):
            continue
        if row["service"] not in wanted:
            wanted.append(row["service"])
    return wanted


def compose(*args):
    """Runs a compose command quietly; the caller reports what went wrong."""
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def health_of(services):
    """Maps each service to 'healthy', 'starting', or 'gone'."""
    done = compose("ps", "--format", "json", *services)
    states = {service: "gone" for service in services}
    for line in done.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        service = entry.get("Service")
        if service not in states:
            continue
        health = (entry.get("Health") or "").lower()
        running = entry.get("State") == "running"
        if health == "healthy" or (running and not health):
            states[service] = "healthy"
        elif health == "unhealthy":
            states[service] = "unhealthy"
        else:
            states[service] = "starting"
    return states


def start(services):
    """Starts the services and waits for every one to report healthy."""
    print(f"starting {', '.join(services)}", flush=True)
    done = compose("up", "-d", *services)
    if done.returncode != 0:
        print(done.stderr.rstrip(), file=sys.stderr)
        return False
    deadline = time.monotonic() + HEALTHY_TIMEOUT
    while time.monotonic() < deadline:
        states = health_of(services)
        if all(state == "healthy" for state in states.values()):
            return True
        sick = [name for name, state in states.items() if state == "unhealthy"]
        if sick:
            print(f"error: {', '.join(sick)} reported unhealthy.", file=sys.stderr)
            return False
        time.sleep(2)
    late = [name for name, state in health_of(services).items() if state != "healthy"]
    print(
        f"error: {', '.join(late)} did not become healthy in "
        f"{HEALTHY_TIMEOUT} seconds.",
        file=sys.stderr,
    )
    return False


def stop(services):
    print(f"removing {', '.join(services)}", flush=True)
    compose("rm", "--force", "--stop", "--volumes", *services)


def suite_env(extra=None):
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    path = os.pathsep.join(["src", "."] + ([existing] if existing else []))
    env["PYTHONPATH"] = path
    env.update(extra or {})
    return env


def read_counts(output):
    """The test count and the number that did not pass."""
    ran = RAN.search(output)
    total = int(ran.group(1)) if ran else 0
    broken = sum(int(count) for _, count in BROKEN.findall(output))
    return total, broken


def run_tests(target, command, env, suffix=""):
    """Runs one test command and turns its output into a result line."""
    done = subprocess.run(
        command, cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    output = done.stdout + done.stderr
    total, broken = read_counts(output)
    if done.returncode == 0:
        result = Result(target, RAN_OK, f"{total} tests{suffix}")
    elif total:
        result = Result(target, FAILED, f"{broken} of {total} tests")
    else:
        # setUpClass raised, so no test got as far as running.
        result = Result(target, FAILED, "the suite did not start")
    result.output = output
    return result


def run_server(name):
    """The integration suite for one server, with skips turned into failures."""
    row = ROWS[name]
    extra = {"SUSTAINED_TEST_STRICT": "1"}
    if row["dsn_env"] and row["dsn"] and not os.environ.get(row["dsn_env"]):
        extra[row["dsn_env"]] = row["dsn"]
    return run_tests(
        name,
        [sys.executable, "-m", "unittest", f"tests.integration.test_{name}"],
        suite_env(extra),
        suffix=f", {', '.join(row['covers'])}",
    )


def interpreters():
    """Every python3.X on PATH that support.json names, newest first."""
    found = []
    for version in reversed(SUPPORT["python"]["versions"]):
        path = shutil.which(f"python{version}")
        found.append((version, path))
    return found


def run_python():
    """The unit suite on every interpreter this machine has, newest first."""
    for version, path in interpreters():
        target = f"python{version}"
        if not path:
            yield Result(target, WAITING, "not on PATH")
            continue
        command = [path, "-m", "unittest", "discover", "-s", "tests"]
        yield run_tests(target, command, suite_env())


def emit(result):
    """One line per target, printed as soon as that target is settled."""
    print(
        f"{result.state:<8}{result.target:<{NAME_WIDTH}}{result.detail}",
        flush=True,
    )
    return result


def report(results):
    """The failures in full, then the count. Every line is already printed."""
    failed = [result for result in results if result.state == FAILED]
    waiting = [result for result in results if result.state == WAITING]
    for result in failed:
        print(f"\n--- {result.target} ---\n{result.output.rstrip()}")
    print()
    if failed:
        print(f"{len(failed)} of {len(results)} failed")
        return 1
    if waiting:
        print(f"{len(waiting)} of {len(results)} still waiting")
        return 2
    print(f"{len(results)} clean")
    return 0


def check(names, have_docker):
    """Says what would run and what is missing, without running anything."""
    results = []
    for name in names:
        if name == PYTHON:
            for version, path in interpreters():
                results.append(
                    Result(
                        f"python{version}",
                        READY if path else WAITING,
                        path or "not on PATH",
                    )
                )
            continue
        reason = missing_reason(name, have_docker)
        row = ROWS[name]
        results.append(
            Result(
                name,
                WAITING if reason else READY,
                reason or f"{row['title']}, {', '.join(row['covers'])}",
            )
        )
    for result in results:
        emit(result)
    waiting = [result for result in results if result.state == WAITING]
    print()
    if waiting:
        print(f"{len(waiting)} of {len(results)} still waiting")
        return 2
    print(f"{len(results)} ready")
    return 0


def parse(argv):
    parser = argparse.ArgumentParser(
        prog="matrix.py",
        description="Run the support matrix against real servers.",
        epilog=f"targets: {', '.join(TARGETS)}",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="target",
        help="servers to run, or 'python' for the interpreter matrix",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="say what would run and what is missing, then stop",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the containers running afterwards",
    )
    parser.add_argument(
        "--athena-profile",
        metavar="NAME",
        help="the AWS profile Athena connects with",
    )
    return parser.parse_args(argv)


def main(argv):
    args = parse(argv)
    unknown = [target for target in args.targets if target not in TARGETS]
    if unknown:
        print(
            f"error: no such target: {', '.join(unknown)}. "
            f"Targets are: {', '.join(TARGETS)}",
            file=sys.stderr,
        )
        return 1
    if args.athena_profile:
        os.environ["AWS_PROFILE"] = args.athena_profile

    names = args.targets or SERVERS
    have_docker = docker_ready()
    if args.check:
        return check(names, have_docker)

    results = []
    runnable = []
    for name in names:
        if name == PYTHON:
            continue
        reason = missing_reason(name, have_docker)
        if reason:
            results.append(emit(Result(name, WAITING, reason)))
        else:
            runnable.append(name)

    services = services_for(runnable)
    started = False
    try:
        if services:
            started = start(services)
            if not started:
                waited = "the server did not start"
                results.extend(emit(Result(n, WAITING, waited)) for n in runnable)
                runnable = []
        for name in runnable:
            results.append(emit(run_server(name)))
    finally:
        if started and not args.keep:
            stop(services)

    if PYTHON in names:
        results.extend(emit(result) for result in run_python())

    return report(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
