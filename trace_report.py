"""Build a Speedscope report that aggregates the API's managed samples."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from math import fsum, inf
from pathlib import Path
import shutil
import struct
import subprocess
from tempfile import NamedTemporaryFile, TemporaryDirectory


TRACE_HEADER = (
    b"Nettrace"
    + struct.pack("<I20s", 20, b"!FastSerialization.1")
    + b"\x05\x05\x01"
    + struct.pack("<III", 4, 4, 5)
    + b"Trace\x06"
)
APP_PREFIX = "FasterNFaster."


def trace_start(trace_path):
    # The NetTrace v5 Trace object starts with a UTC SYSTEMTIME.
    with trace_path.open("rb") as stream:
        if stream.read(len(TRACE_HEADER)) != TRACE_HEADER:
            raise ValueError("unsupported .nettrace header for phase alignment")
        timestamp = stream.read(16)
    if len(timestamp) != 16:
        raise ValueError("incomplete .nettrace timestamp")
    year, month, _, day, hour, minute, second, millis = struct.unpack("<8h", timestamp)
    return datetime(year, month, day, hour, minute, second, millis * 1000, timezone.utc)


def report_windows(trace_path, phases_path, trace_end):
    windows = []
    if phases_path.is_file():
        origin = trace_start(trace_path)
        phases = json.loads(phases_path.read_text(encoding="utf-8"))["phases"]
        offsets = {
            name: (datetime.fromisoformat(time) - origin).total_seconds() * 1000
            for name, time in phases.items()
        }
        if "typing_start" in offsets and "race_ended" in offsets:
            start, end = offsets["typing_start"], offsets["race_ended"]
            if end > trace_end or start < 0:
                print("[!] Trace covers only part of the typing phase, if any.")
            windows.extend([
                ("Typing | FasterNFaster call stacks", start, end, True),
                ("Typing | managed CPU samples | all threads", start, end, False),
            ])
        if "auth_start" in offsets and "auth_end" in offsets:
            windows.append(("Auth and SignalR connect | managed CPU samples | all threads",
                            offsets["auth_start"], offsets["auth_end"], False))
    else:
        print("[!] No phases.json. Using the full capture.")
    windows.append(("Full capture | managed CPU samples | all threads", -inf, inf, False))
    return windows


def managed_intervals(profile, cpu_frames):
    if profile["type"] != "evented" or profile["unit"] != "milliseconds":
        raise ValueError("expected the per-thread Speedscope export from dotnet-trace")
    stack = []
    previous = profile["startValue"]
    for event in profile["events"]:
        current, frame = event["at"], event["frame"]
        if current < previous:
            raise ValueError("trace events are out of order")
        if current > previous and stack and stack[-1] in cpu_frames:
            yield previous, current, tuple(stack[:-1])
        if event["type"] == "O":
            stack.append(frame)
        elif event["type"] != "C" or not stack or stack.pop() != frame:
            raise ValueError("trace contains an unbalanced call stack")
        previous = current
    if stack:
        raise ValueError("trace contains an unfinished call stack")


def aggregate_stacks(trace, windows):
    names = [frame["name"] for frame in trace["shared"]["frames"]]
    cpu_frames = {i for i, name in enumerate(names) if name == "CPU_TIME"}
    if not cpu_frames:
        raise ValueError("trace contains no CPU_TIME samples")
    app_frames = {i for i, name in enumerate(names) if name.startswith(APP_PREFIX)}
    labels = {i for i, name in enumerate(names)
              if name in {"(Non-Activities)", "Threads"} or name.startswith(("Thread (", "Process64 ", "Process32 "))}
    totals = [Counter() for _ in windows]

    for profile in trace["profiles"]:
        for start, end, stack in managed_intervals(profile, cpu_frames):
            stack = tuple(frame for frame in stack if frame not in labels)
            app_start = next((i for i, frame in enumerate(stack) if frame in app_frames), None)
            for (_, window_start, window_end, app_only), counts in zip(windows, totals, strict=True):
                overlap = min(end, window_end) - max(start, window_start)
                if overlap > 0 and stack and (not app_only or app_start is not None):
                    counts[stack[app_start:] if app_only else stack] += overlap
    return totals


def write_trace_report(trace_path, phases_path):
    original = trace_path.with_suffix(".speedscope.json")
    trace = json.loads(original.read_bytes())
    trace_end = max(profile["endValue"] for profile in trace["profiles"])
    windows = report_windows(trace_path, phases_path, trace_end)
    totals = aggregate_stacks(trace, windows)

    used = sorted({frame for counts in totals for stack in counts for frame in stack})
    indices = {frame: index for index, frame in enumerate(used)}
    profiles = []
    for (name, *_), counts in zip(windows, totals, strict=True):
        if not counts:
            continue
        stacks, weights = zip(*counts.most_common())
        profiles.append({
            "type": "sampled",
            "name": name,
            "unit": "milliseconds",
            "startValue": 0,
            "endValue": fsum(weights),
            "samples": [[indices[frame] for frame in stack] for stack in stacks],
            "weights": weights,
        })
    if not profiles:
        raise ValueError("trace contains no usable managed stacks")
    if profiles[0]["name"] != "Typing | FasterNFaster call stacks":
        print("[!] No application samples for the typing phase. Open another view in the report.")

    report = {
        "$schema": "https://www.speedscope.app/file-format-schema.json",
        "name": f"{trace_path.stem} | aggregated managed CPU samples",
        "activeProfileIndex": 0,
        "shared": {"frames": [trace["shared"]["frames"][frame] for frame in used]},
        "profiles": profiles,
    }
    output = trace_path.with_suffix(".managed.speedscope.json")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete_on_close=False) as temp:
        json.dump(report, temp, separators=(",", ":"), allow_nan=False)
        temp.close()
        Path(temp.name).replace(output)
    print(f"Speedscope report: {output}")
    return output


def convert_trace(trace_path, run_converter):
    with TemporaryDirectory(dir=trace_path.parent) as directory:
        output = Path(directory) / "trace"
        run_converter(output)
        converted = output.with_suffix(".speedscope.json")
        if not converted.is_file() or not converted.stat().st_size:
            raise ValueError("dotnet-trace did not produce a Speedscope file")
        converted.replace(trace_path.with_suffix(".speedscope.json"))


def convert_on_host(trace_path):
    tool = shutil.which("dotnet-trace") or Path.home() / ".dotnet" / "tools" / "dotnet-trace"
    if not Path(tool).is_file():
        raise ValueError("install the converter with: dotnet tool install --global dotnet-trace")

    def run_converter(output):
        subprocess.run([tool, "convert", trace_path, "--format", "Speedscope", "--output", output], check=True)

    convert_trace(trace_path, run_converter)


def main():
    parser = argparse.ArgumentParser(description="Aggregate managed stacks from an existing .nettrace")
    parser.add_argument("trace", type=Path, help="path to the original .nettrace")
    parser.add_argument("--phases", type=Path, help="phase file (default: phases.json in the run folder)")
    args = parser.parse_args()
    trace_path = args.trace.resolve()
    if trace_path.suffix != ".nettrace" or not trace_path.is_file():
        parser.error("expected an existing .nettrace file")
    phases_path = args.phases or trace_path.parent.parent / "phases.json"
    try:
        per_thread = trace_path.with_suffix(".speedscope.json")
        if not per_thread.is_file() or not per_thread.stat().st_size:
            convert_on_host(trace_path)
        write_trace_report(trace_path, phases_path)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Could not build the Speedscope report: {error}\n")


if __name__ == "__main__":
    main()
