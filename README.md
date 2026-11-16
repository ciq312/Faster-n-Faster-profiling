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

Options:

- `--users N` (required), `--wpm N` and `--label NAME` configure the run.
- `--skip-build` reuses existing API, tools and bot images.
- `--no-graph` saves raw data and bot results only.
- `--app-repo PATH` selects the application repository; default is `../Faster-n-Faster`.
- `--project NAME` selects the Compose project; default is `fnf-profile`.

The profiling stack stays running after a run so another scenario can reuse it. Shut it down explicitly:

```bash
docker compose --env-file /dev/null -p fnf-profile \
  -f ../Faster-n-Faster/docker-compose.profiling.yml down
```

Each run folder contains `counters.csv`, `phases.json`, `bot-run/` (the versioned run summary and bot artifacts), and generated `graphscounters.csv/` PNGs. A failed load test still preserves available artifacts and returns its non-zero exit status. This isolated local stack has rate limiting disabled only for the profiling API; never point the bot command at a live service for capacity testing.
