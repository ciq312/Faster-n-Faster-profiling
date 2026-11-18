# Faster'n'Faster — Local Load Test & Profiling

Runs the SignalR bot load test against the isolated Docker Compose stack in the sibling `Faster-n-Faster` repository, collects .NET runtime counters, and renders graphs locally. It does not connect to a VPS or production server.

## Requirements

- Docker Compose v2 and a running Docker daemon.
- Python 3.12+ and `pip install -r requirements.txt`.
- The application repository alongside this repository, or pass `--app-repo PATH`.

## Run the complete pipeline

```bash
python automation.py --users 5 --label local-5-users --wpm 120
```

The command builds and starts the isolated API, databases and diagnostics sidecar, starts a fresh counter collection, runs the bots, stops the collector so the CSV is flushed, copies raw data and bot results, then builds the graph image. Artifacts land in `profiling/<label>/`.

To capture a short trace for investigating slow code, run a diagnostic scenario instead of collecting counters:

```bash
python automation.py --users 5 --label inspect-hot-path --wpm 120 --trace
```

The default trace lasts 45 seconds and uses the `dotnet-common` and `dotnet-sampled-thread-time` profiles. Set `--trace-duration 00:00:02:00` (DD:HH:MM:SS) if bots take longer to connect and complete the race. The trace mode runs one collector per capture. Use a regular run for counter graphs and comparisons.

After collection, the diagnostics image converts the trace locally. The run folder contains three trace files:

| File in `traces/` | Use |
| --- | --- |
| `trace-<label>.nettrace` | Original recording for further analysis. |
| `trace-<label>.speedscope.json` | Per-thread profiles for the **Time Order** view. |
| `trace-<label>.managed.speedscope.json` | Combined managed samples for **Sandwich** or **Left Heavy**. |

Import the managed file into [Speedscope](https://www.speedscope.app) and press **3** for **Sandwich**. When typing samples are available, the default view starts at the application frames during typing. Other views include managed samples across all threads for typing, authentication and connection, and the full recording. The weights sum the recorded `CPU_TIME` intervals and discard their original order. Keep the per-thread export for timeline analysis.

To build the combined report from a saved run:

```bash
python trace_report.py profiling/1000-users-trace/traces/trace-1000-users-trace.nettrace
```

The script reuses the per-thread JSON if present. If you only have the `.nettrace`, install the host converter with `dotnet tool install --global dotnet-trace`. It reads `phases.json` from the run folder or from the path passed to `--phases`.

Options:

- `--users N` (required), `--wpm N` and `--label NAME` configure the run.
- `--skip-build` reuses existing API, tools and bot images.
- `--no-graph` saves raw data and bot results without PNG or Speedscope reports.
- `--app-repo PATH` selects the application repository; default is `../Faster-n-Faster`.
- `--project NAME` selects the Compose project; default is `fnf-profile`.

The profiling stack stays running after a run so another scenario can reuse it. Shut it down explicitly:

```bash
docker compose --env-file /dev/null -p fnf-profile \
  -f ../Faster-n-Faster/docker-compose.profiling.yml down
```

Counter runs contain `counters.csv` and `graphscounters.csv/` PNGs. Trace runs contain `traces/` and `trace-metadata.json`. Both include `phases.json` and `bot-run/` when available. A failed load test preserves available artifacts and returns its non-zero exit status. The isolated local profiling API runs with HTTP rate limiting disabled.
