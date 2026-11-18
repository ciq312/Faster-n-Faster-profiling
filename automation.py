"""Run the local profiling Compose stack and save metrics or a diagnostic trace."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from trace_report import convert_trace, write_trace_report


HERE = Path(__file__).resolve().parent
DEFAULT_APP_REPO = HERE.parent / "Faster-n-Faster"
TRACE_TOOL_VERSION = "10.0.750501"
TRACE_PROFILE = "dotnet-common,dotnet-sampled-thread-time"


def run(command, *, env=None, check=True):
    print("$", " ".join(map(str, command)), flush=True)
    return subprocess.run(command, cwd=HERE, env=env, check=check)


def convert_in_diagnostics(trace_path, compose, env):
    image = subprocess.run(
        [*compose, "config", "--images", "diagnostics"],
        cwd=HERE, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not image:
        raise ValueError("could not resolve the diagnostics image")

    def run_converter(output):
        run(["docker", "run", "--rm", "--pull", "never", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
             "--volume", f"{trace_path.parent}:/traces:z", "--entrypoint", "/tools/dotnet-trace", image,
             "convert", f"/traces/{trace_path.name}", "--format", "Speedscope",
             "--output", f"/traces/{output.relative_to(trace_path.parent)}"], env=env)

    convert_trace(trace_path, run_converter)


def main():
    parser = argparse.ArgumentParser(description="Run local Docker profiling and graphs")
    parser.add_argument("--users", type=int, required=True)
    parser.add_argument("--label", help="output folder name (default: timestamp and user count)")
    parser.add_argument("--wpm", type=int, default=120)
    parser.add_argument("--app-repo", type=Path, default=DEFAULT_APP_REPO)
    parser.add_argument("--project", default="fnf-profile")
    parser.add_argument("--skip-build", action="store_true", help="reuse existing Compose images")
    parser.add_argument("--no-graph", action="store_true", help="skip PNG graphs and Speedscope reports")
    parser.add_argument("--trace", action="store_true", help="collect a diagnostic .nettrace instead of counters")
    parser.add_argument("--trace-duration", default="00:00:00:45",
                        help="trace duration in DD:HH:MM:SS format (default: 45 seconds)")
    args = parser.parse_args()
    if args.users < 1 or args.wpm < 1:
        parser.error("--users and --wpm must be positive")
    if args.trace and not re.fullmatch(r"\d{2}:\d{2}:\d{2}:\d{2}", args.trace_duration):
        parser.error("--trace-duration must use DD:HH:MM:SS format")

    app_repo = args.app_repo.expanduser().resolve()
    compose_file = app_repo / "docker-compose.profiling.yml"
    runs_dir = app_repo / "profiling-runs"
    if not compose_file.is_file():
        parser.error(f"profiling Compose file not found: {compose_file}")

    label = args.label or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{args.users}users"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", label):
        parser.error("--label may contain only letters, numbers, dots, underscores and hyphens")
    output = HERE / "profiling" / label
    output.mkdir(parents=True, exist_ok=False)
    trace_filename = f"trace-{label}.nettrace"

    env = os.environ.copy()
    env.update({
        "PROFILE_UID": str(os.getuid()),
        "PROFILE_GID": str(os.getgid()),
        "PROFILE_USERS": str(args.users),
        "PROFILE_WPM": str(args.wpm),
        # Start dependencies in counters mode; the API is restarted before the
        # selected collector is attached, clearing any prior EventPipe session.
        "PROFILE_COLLECTOR": "counters",
        "PROFILE_TRACE_DURATION": args.trace_duration,
        "PROFILE_TRACE_FILE": trace_filename,
    })
    compose = ["docker", "compose", "--env-file", "/dev/null", "-p", args.project,
               "-f", str(compose_file)]

    def dc(*parts, check=True):
        return run([*compose, *parts], env=env, check=check)

    result = None
    collector_result = None
    setup_error = None
    trace_report_failed = False
    previous_runs = set()
    try:
        if not args.skip_build:
            dc("up", "--build", "-d", "api")
        else:
            dc("up", "-d", "api")

        dc("stop", "diagnostics", check=False)
        dc("restart", "api")
        previous_runs = {p.name for p in runs_dir.iterdir() if p.is_dir()} if runs_dir.exists() else set()
        for stale in (runs_dir / "phases.json", runs_dir / "counters.csv", runs_dir / trace_filename):
            stale.unlink(missing_ok=True)
        env["PROFILE_COLLECTOR"] = "trace" if args.trace else "counters"
        dc("up", "-d", "--force-recreate", "--wait", "diagnostics")

        loadtest_args = ["run", "--rm", "loadtest"]
        if not args.skip_build:
            loadtest_args.insert(1, "--build")
        result = dc(*loadtest_args, check=False)
        if args.trace:
            # A duration bound lets dotnet-trace flush a complete nettrace file.
            collector_result = dc("wait", "diagnostics", check=False)
    except subprocess.CalledProcessError as error:
        setup_error = error
        print(f"[!] Profiling setup or collector failed: {error}", file=sys.stderr)
    finally:
        dc("stop", "diagnostics", check=False)

    phases_source = runs_dir / "phases.json"
    if phases_source.is_file():
        shutil.copy2(phases_source, output / "phases.json")

    run_dirs = sorted((p for p in runs_dir.iterdir()
                       if p.is_dir() and p.name not in previous_runs),
                      key=lambda p: p.stat().st_mtime, reverse=True) if runs_dir.exists() else []
    if run_dirs:
        shutil.copytree(run_dirs[0], output / "bot-run", dirs_exist_ok=True)

    if args.trace:
        trace_source = runs_dir / trace_filename
        if trace_source.is_file() and trace_source.stat().st_size > 0:
            traces_dir = output / "traces"
            traces_dir.mkdir()
            trace_path = traces_dir / trace_filename
            shutil.copy2(trace_source, trace_path)
            (output / "trace-metadata.json").write_text(json.dumps({
                "schemaVersion": 1,
                "tool": f"dotnet-trace {TRACE_TOOL_VERSION}",
                "profile": TRACE_PROFILE,
                "duration": args.trace_duration,
                "trace": f"traces/{trace_filename}",
                "loadtestExitCode": result.returncode if result else None,
            }, indent=2) + "\n")
            if not args.no_graph and collector_result is not None and collector_result.returncode == 0:
                try:
                    convert_in_diagnostics(trace_path, compose, env)
                    write_trace_report(trace_path, output / "phases.json")
                except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
                    trace_report_failed = True
                    print(f"[!] Could not build the Speedscope report: {error}", file=sys.stderr)
        else:
            print("[!] Trace file is missing or empty.", file=sys.stderr)
    else:
        counters_source = runs_dir / "counters.csv"
        if counters_source.is_file():
            shutil.copy2(counters_source, output / "counters.csv")

        if not (output / "counters.csv").is_file() or not (output / "counters.csv").stat().st_size:
            print("[!] counters.csv is missing or empty.", file=sys.stderr)
        elif not args.no_graph:
            profiler_dir = HERE / "profiling"
            run(["docker", "build", "-t", "fnf-profiler", str(profiler_dir)])
            run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
                 "-w", "/data", "-v", f"{output}:/data:z",
                 "fnf-profiler", "counters.csv", "phases.json"])

    print(f"Artifacts: {output}")
    if setup_error:
        return setup_error.returncode or 1
    if result is None or result.returncode != 0:
        return result.returncode if result else 1
    if collector_result and collector_result.returncode != 0:
        return collector_result.returncode
    if args.trace and not (output / "traces" / trace_filename).is_file():
        return 1
    if trace_report_failed:
        return 1
    if not args.trace and (not (output / "counters.csv").is_file()
                           or not (output / "counters.csv").stat().st_size):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
