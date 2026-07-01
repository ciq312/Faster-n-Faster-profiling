"""
Load-test + profiling orchestrator.

The server (fnf) does only what must happen there; graphs are built locally from
the raw data, because they're derived artifacts you iterate on (metrics, overlays)
and the server is the box under test. Pipeline for one invocation:

  ON SERVER
  1. build the loadtest image
  2. start dotnet-counters inside the backend container
  3. run the loadtest container to completion  -> writes phases.json
     (its runtime is exactly the counter collection window)
  4. stop counters, copy counters.csv out of the backend container

  LOCAL
  5. pull counters.csv + phases.json into profiling/<label>/
  6. delete the temp run dir on the server
  7. build + run the profiler image locally -> graphs next to the raw data

Config comes from .env; run parameters from CLI. Example:

  python automation.py --users 200 --label 200UsersProdFix --wpm 120
"""

import argparse
import os
import subprocess
import time

from dotenv import load_dotenv
from fabric import Connection

# ---------------------------------------------------------------------------
# Config (env with sensible defaults)
# ---------------------------------------------------------------------------
load_dotenv()

SERVER = os.getenv("SERVER_NAME", "fnf")
REMOTE_REPO = os.getenv("REMOTE_REPO", "/opt/Faster-n-Faster")
BACKEND_CONTAINER = os.getenv("BACKEND_CONTAINER", "backend")
# URL the loadtest container uses to reach the backend, from on the server.
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8080")
# Docker network for the loadtest container. "host" reaches published ports;
# "container:backend" shares the backend's net namespace (localhost = backend).
LOADTEST_NETWORK = os.getenv("LOADTEST_NETWORK", "host")
COUNTERS = os.getenv("COUNTERS", "System.Runtime,Microsoft.AspNetCore.Hosting")

LOADTEST_IMAGE = "loadtest"
PROFILER_IMAGE = "profiler"

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# dotnet-counters is baked into the backend image at /opt/tools; collect on PID 1.
COLLECT_METRICS_CMD = (
    f"docker exec -d {BACKEND_CONTAINER} sh -c "
    f"'/opt/tools/dotnet-counters collect -p 1 "
    f"--format csv -o /tmp/counters.csv "
    f"--counters {COUNTERS} "
    f"& echo $! > /tmp/counters.pid'"
)
STOP_COLLECTING_METRICS_CMD = (
    f"docker exec {BACKEND_CONTAINER} sh -c 'kill -INT $(cat /tmp/counters.pid)'"
)


def collect_on_server(conn, args, remote_dir):
    """Steps 1-4: everything that must run on the box under test."""
    conn.run(f"mkdir -p {remote_dir}")

    print("[collect] starting dotnet-counters")
    conn.run(COLLECT_METRICS_CMD)
    time.sleep(args.preroll)  # baseline before load

    # Loadtest runs in the foreground; this call blocks for the whole run, so the
    # counter window brackets it. phases.json lands in the mounted temp dir.
    insecure = " --insecure" if args.insecure else ""
    print(f"[loadtest] {args.users} users @ {args.wpm} wpm -> {BACKEND_URL}")
    conn.run(
        f"docker run --rm --network {LOADTEST_NETWORK} "
        f"-e LOADTEST_OUT=/out -v {remote_dir}:/out {LOADTEST_IMAGE} "
        f"--users {args.users} --server {BACKEND_URL} --wpm {args.wpm}{insecure} --out /out"
    )

    time.sleep(args.postroll)  # let counters settle after load
    print("[collect] stopping dotnet-counters")
    conn.run(STOP_COLLECTING_METRICS_CMD)

    print("[copy] counters.csv out of backend")
    conn.run(f"docker cp {BACKEND_CONTAINER}:/tmp/counters.csv {remote_dir}/counters.csv")


def graph_locally(local_dir):
    """Steps 7: build + run the profiler image locally over the raw data."""
    print("[graph] building profiler image")
    subprocess.run(
        ["docker", "build", "-t", PROFILER_IMAGE, os.path.join(REPO_ROOT, "profiling")],
        check=True,
    )
    print("[graph] rendering graphs")
    # List form -> no shell quoting; the apostrophe in the repo path is safe.
    subprocess.run(
        ["docker", "run", "--rm", "-w", "/data", "-v", f"{local_dir}:/data",
         PROFILER_IMAGE, "counters.csv", "phases.json"],
        check=True,
    )


def main():
    p = argparse.ArgumentParser(description="Load-test + profiling orchestrator")
    p.add_argument("--users", type=int, required=True)
    p.add_argument("--label", help="run name / output subdir (default: <N>users)")
    p.add_argument("--wpm", type=int, default=120)
    p.add_argument("--insecure", action="store_true", help="skip TLS validation")
    p.add_argument("--skip-build", action="store_true", help="reuse loadtest image on server")
    p.add_argument("--keep-remote", action="store_true", help="don't delete the server temp dir")
    p.add_argument("--no-graph", action="store_true", help="pull raw data only, skip graphing")
    p.add_argument("--preroll", type=int, default=3, help="baseline seconds before load")
    p.add_argument("--postroll", type=int, default=3, help="settle seconds after load")
    args = p.parse_args()
    if not args.label:
        args.label = f"{args.users}users"

    remote_dir = f"/tmp/loadtest-{args.label}"
    local_dir = os.path.join(REPO_ROOT, "profiling", args.label)
    os.makedirs(local_dir, exist_ok=True)

    with Connection(SERVER) as conn:
        print(f"Connected to {SERVER}")
        if not args.skip_build:
            print("[build] loadtest image")
            conn.run(f"docker build -t {LOADTEST_IMAGE} {REMOTE_REPO}/loadtest")

        collect_on_server(conn, args, remote_dir)

        print(f"[pull] -> {local_dir}")
        conn.get(f"{remote_dir}/counters.csv", local=os.path.join(local_dir, "counters.csv"))
        conn.get(f"{remote_dir}/phases.json", local=os.path.join(local_dir, "phases.json"))

        if not args.keep_remote:
            print("[cleanup] removing server temp dir")
            conn.run(f"rm -rf {remote_dir}")

    if not args.no_graph:
        graph_locally(local_dir)

    print(f"Done. Raw data + graphs in {local_dir}")


if __name__ == "__main__":
    main()
