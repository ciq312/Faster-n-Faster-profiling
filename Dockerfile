# syntax=docker/dockerfile:1

# Grab just the Docker CLI binary. automation.py shells out to `docker build/run`
# for the local graphing step; it talks to the host daemon over a mounted socket,
# so only the client is needed here — not the engine.
FROM docker:27-cli AS docker-cli

FROM python:3.12-slim
WORKDIR /app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY automation.py ./

# Args mirror the script's CLI, e.g.:
#   docker run --rm orchestrator --users 200 --label run1 --wpm 120
ENTRYPOINT ["python", "automation.py"]
CMD ["--help"]
