# Faster'n'Faster — Load Test & Profiling Tools

Tooling to **load-test and profile the [Faster'n'Faster](https://faster-n-faster.com) web app** by
simulating hundreds to thousands of concurrent typing-race players and correlating the resulting
server load against the phases of a run.

Each simulated player ("bot") authenticates, opens a **SignalR** hub connection, joins a lobby,
receives a passage, and types it out at a configurable words-per-minute rate while its round-trip
latency is sampled. In parallel, `dotnet-counters` records runtime metrics (CPU, memory, GC,
thread pool, HTTP) inside the backend container. The two data sets are then rendered into graphs
so you can see exactly how the server behaves as load ramps up.

---

## How it works

One `automation.py` invocation runs this pipeline:

```
ON THE SERVER (under test)                          LOCALLY
  1. build the loadtest image                         5. pull counters.csv + phases.json
  2. start dotnet-counters in the backend                into profiling/<label>/
  3. run the loadtest container to completion         6. delete the server temp dir
     -> writes phases.json                            7. build + run the profiler image
  4. stop counters, copy counters.csv out                -> graphs next to the raw data
```

The loadtest container's runtime **is** the counter-collection window, so the metrics line up with
the phase markers (`auth`, `join`, `passage_ready`, `race_started`, `race_ended`) the load test emits.

---

## Repository layout

| Path | What it is |
|------|-----------|
| `automation.py`        | Orchestrator. Drives the server over SSH ([Fabric](https://www.fabfile.org/)) and graphs locally. |
| `loadtest/`            | .NET 10 console app that spawns the bot swarm against the backend. |
| `loadtest/Program.cs`  | Run flow: auth → connect → lobby → race → type → stats → `phases.json`. |
| `loadtest/LoadTestBot.cs` | A single simulated player (HTTP auth + SignalR hub client). |
| `profiling/`           | `profiler.py` — turns `counters.csv` + `phases.json` into graphs (pandas + matplotlib). |
| `profiling/<label>/`   | Raw data + generated graphs for a given run. |
| `Dockerfile`           | Packages the orchestrator (`automation.py`). |
| `requirements.txt`     | Python deps for the orchestrator (`fabric`, `python-dotenv`). |

Each component ships its own `Dockerfile`, so they can run standalone or be driven by the orchestrator.

---

## Prerequisites

- **Docker** on your local machine.
- **SSH access to the server under test**, configured as a host alias (default `fnf`) in
  `~/.ssh/config`. Docker must be installed on that server, and the backend must run in a container
  with `dotnet-counters` available at `/opt/tools/dotnet-counters`.
- To run the orchestrator **without** Docker: Python 3.12+ and `pip install -r requirements.txt`.

---

## Configuration (`.env`)

Config lives in `.env` (git-ignored); run parameters come from the CLI. Keys and their defaults:

| Key | Default | Meaning |
|-----|---------|---------|
| `SERVER_NAME`       | `fnf`                        | SSH host alias of the box under test. |
| `REMOTE_REPO`       | `/opt/Faster-n-Faster`       | Path to the backend repo on the server (holds `loadtest/`). |
| `BACKEND_CONTAINER` | `backend`                    | Name of the backend container (counters run inside it). |
| `BACKEND_URL`       | `http://localhost:8080`      | URL the loadtest container uses to reach the backend. |
| `LOADTEST_NETWORK`  | `host`                       | Docker network for the loadtest container. |
| `COUNTERS`          | `System.Runtime,Microsoft.AspNetCore.Hosting` | Counter providers to collect. |

> **`BACKEND_URL` must match how the backend is exposed.** Hitting the public domain requires
> `https://` — over plain `http://` the site 301-redirects and .NET downgrades the auth `POST` to a
> bodyless `GET`, so auth fails. For the local container use `http://localhost:8080` with
> `LOADTEST_NETWORK=host`, or `LOADTEST_NETWORK=container:backend`.

Minimal `.env`:

```dotenv
SERVER_NAME=fnf
BACKEND_URL=https://faster-n-faster.com
```

---

## Quick start

Run the full pipeline against the server, graphs included:

```bash
python automation.py --users 200 --label 200UsersProdFix --wpm 120
```

Raw data and graphs land in `profiling/200UsersProdFix/`.

### `automation.py` options

| Flag | Default | Description |
|------|---------|-------------|
| `--users N`     | *(required)* | Number of simulated players. |
| `--label NAME`  | `<N>users`   | Run name / output subdir under `profiling/`. |
| `--wpm N`       | `120`        | Target typing speed per bot. |
| `--insecure`    | off          | Skip TLS validation (self-signed / local certs). |
| `--skip-build`  | off          | Reuse the existing `loadtest` image on the server. |
| `--keep-remote` | off          | Don't delete the server temp dir after pulling data. |
| `--no-graph`    | off          | Pull raw data only; skip the graphing step. |
| `--preroll N`   | `3`          | Baseline seconds recorded before load starts. |
| `--postroll N`  | `3`          | Settle seconds recorded after load ends. |

---

## Running the components on their own

### Load test only

The load test runs **anywhere** — you don't need the SSH orchestrator. Point `--server` at whatever
backend you want to hit: a local instance (`http://localhost:8080`) or production
(`https://faster-n-faster.com`). The only requirement for collecting metrics is having
`dotnet-counters` available on whichever backend you're profiling.

```bash
docker build -t loadtest ./loadtest
docker run --rm --network host \
  -e LOADTEST_OUT=/out -v "$PWD/out:/out" \
  loadtest --users 100 --server http://localhost:8080 --wpm 120 --out /out
```

`loadtest` args: `--users N`, `--server URL`, `--wpm N`, `--insecure`, `--out DIR`
(output dir for `phases.json`; also honors the `LOADTEST_OUT` env var).

### Profiler only

Render graphs from raw data already pulled into a run directory:

```bash
docker build -t profiler ./profiling
docker run --rm -w /data -v "$PWD/profiling/200UsersProdFix:/data" \
  profiler counters.csv phases.json
```

The profiler auto-detects headered vs. headerless `dotnet-counters` CSV. It also supports an
**overlay mode** to compare runs — pass `label=csv,phases` arguments:

```bash
profiler 200=200/counters.csv,200/phases.json 500=500/counters.csv,500/phases.json
```

---

## Running the orchestrator in Docker

Build it:

```bash
docker build -t orchestrator .
```

The orchestrator needs three things at runtime: your **SSH keys/config** (to reach the server), the
**`.env`**, and — only for the local graphing step — the **Docker socket**.

```bash
docker run --rm \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --env-file .env \
  -v "$PWD/profiling:/app/profiling" \
  orchestrator --users 200 --label run1 --wpm 120 --no-graph
```

> **Docker-outside-of-Docker caveat.** The graphing step runs `docker run -v <local_dir>:/data` via
> the host daemon, so `<local_dir>` must exist **on the host** at the same path used inside the
> container. Unless you mount the repo at a matching path, run the orchestrator with `--no-graph`
> and render graphs separately with the **profiler** image (see above). The SSH orchestration and
> data pull work regardless.

---

## Output artifacts

For each run, `profiling/<label>/` contains:

- **`counters.csv`** — raw `dotnet-counters` samples from the backend.
- **`phases.json`** — phase timestamps + per-message RTT samples from the load test.
- **`graphs.../*.png`** — CPU (user/system), working-set memory, thread pool, GC allocation,
  JIT activity, and RTT percentiles (p50/p95/p99), each annotated with the run's phase markers.

> **Graphs are not committed to the repo** (`*.png` is git-ignored). They're derived artifacts —
> generate them locally from the raw data with the **profiler** image (see
> [Profiler only](#profiler-only)), or let `automation.py` render them at the end of a run.

---

## Notes on interpreting results

- **502s during connect at high user counts** are usually a real backend finding, not a tooling bug.
  Connections are cumulative (bots stay connected), so the failure point is a **total concurrent
  connection ceiling** — commonly the DB connection pool saturating on a per-connect `OnConnectedAsync`
  query, or reverse-proxy `worker_connections` limits. Check the backend/proxy logs before assuming
  the load test is at fault.
