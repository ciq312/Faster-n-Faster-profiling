"""Run the local profiling Compose stack and render its artifacts."""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_APP_REPO = HERE.parent / "Faster-n-Faster"


def run(command, *, env=None, check=True):
    print("$", " ".join(map(str, command)), flush=True)
    return subprocess.run(command, cwd=HERE, env=env, check=check)


def main():
    parser = argparse.ArgumentParser(description="Run local Docker profiling and graphs")
    parser.add_argument("--users", type=int, required=True)
    parser.add_argument("--label", help="output folder name (default: timestamp and user count)")
    parser.add_argument("--wpm", type=int, default=120)
    parser.add_argument("--app-repo", type=Path, default=DEFAULT_APP_REPO)
    parser.add_argument("--project", default="fnf-profile")
    parser.add_argument("--skip-build", action="store_true", help="reuse existing Compose images")
    parser.add_argument("--no-graph", action="store_true", help="collect data without rendering graphs")
    args = parser.parse_args()
    if args.users < 1 or args.wpm < 1:
        parser.error("--users and --wpm must be positive")

    app_repo = args.app_repo.expanduser().resolve()
    compose_file = app_repo / "docker-compose.profiling.yml"
    runs_dir = app_repo / "profiling-runs"
    if not compose_file.is_file():
        parser.error(f"profiling Compose file not found: {compose_file}")

    label = args.label or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{args.users}users"
    output = HERE / "profiling" / label
    output.mkdir(parents=True, exist_ok=False)

    env = os.environ.copy()
    env.update({
        "PROFILE_UID": str(os.getuid()),
        "PROFILE_GID": str(os.getgid()),
        "PROFILE_USERS": str(args.users),
        "PROFILE_WPM": str(args.wpm),
    })
    compose = ["docker", "compose", "--env-file", "/dev/null", "-p", args.project,
               "-f", str(compose_file)]

    def dc(*parts, check=True):
        return run([*compose, *parts], env=env, check=check)

    result = None
    try:
        if not args.skip_build:
            dc("up", "--build", "-d", "api")
        else:
            dc("up", "-d", "api")

        # Reset the runtime after the previous collector stopped; .NET permits
        # only one counters session per process. Then attach one fresh collector.
        dc("stop", "diagnostics", check=False)
        dc("restart", "api")
        dc("up", "-d", "--force-recreate", "--wait", "diagnostics")
        previous_runs = {p.name for p in runs_dir.iterdir() if p.is_dir()} if runs_dir.exists() else set()
        loadtest_args = ["run", "--rm", "loadtest"]
        if not args.skip_build:
            loadtest_args.insert(1, "--build")
        result = dc(*loadtest_args, check=False)
    finally:
        # Stop dotnet-counters so its CSV is flushed before copying/rendering.
        dc("stop", "diagnostics", check=False)

    output_files = {
        "counters.csv": runs_dir / "counters.csv",
        "phases.json": runs_dir / "phases.json",
    }
    for name, source in output_files.items():
        if source.is_file():
            shutil.copy2(source, output / name)

    run_dirs = sorted((p for p in runs_dir.iterdir()
                       if p.is_dir() and p.name not in previous_runs),
                      key=lambda p: p.stat().st_mtime, reverse=True) if runs_dir.exists() else []
    if run_dirs:
        shutil.copytree(run_dirs[0], output / "bot-run", dirs_exist_ok=True)

    missing = [name for name in output_files if not (output / name).is_file()]
    if missing or not (output / "counters.csv").stat().st_size:
        print(f"[!] Missing or empty artifacts: {', '.join(missing) or 'counters.csv'}", file=sys.stderr)
        return 1
    elif not args.no_graph:
        profiler_dir = HERE / "profiling"
        run(["docker", "build", "-t", "fnf-profiler", str(profiler_dir)])
        run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
             "-w", "/data", "-v", f"{output}:/data:z",
             "fnf-profiler", "counters.csv", "phases.json"])

    print(f"Artifacts: {output}")
    return result.returncode if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
