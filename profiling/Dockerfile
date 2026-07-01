FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir pandas matplotlib

COPY profiler.py ./

# Headless rendering — no display needed for savefig
ENV MPLBACKEND=Agg

# Run with a mounted data dir and -w /data so output "graphs<csv>" lands there, e.g.:
#   docker run --rm -w /data -v /path/to/run:/data profiler counters.csv phases.json
ENTRYPOINT ["python", "/app/profiler.py"]
