# HOSPILOT allocation — HTTP API
#
# Two stages. The builder compiles wheels; the runtime gets the wheels and the source, and
# never gets a compiler. That is not image-size vanity: a container that can build C is a
# container that can build whatever an attacker uploads to it.
#
#   docker build -t hospilot-allocation .
#   docker run --rm -p 8000:8000 hospilot-allocation
#   curl -s localhost:8000/health
#
# What this image serves is three invented Appendix C patients. See README "Deploying it".

# ---------------------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Copy only what the dependency resolution needs, so editing a .py does not invalidate the
# wheel cache. pyproject alone is not enough for `pip wheel .` — it needs the package to
# exist — so the source comes too, but after the layer that matters.
COPY pyproject.toml ./
COPY allocation ./allocation

RUN pip install --no-cache-dir --upgrade pip build \
    && pip wheel --no-cache-dir --wheel-dir /wheels ".[api]"

# ---------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Unbuffered so uvicorn's startup lines reach `docker logs` before the first request rather
# than sitting in a pipe buffer until the process exits — which is exactly when you need them.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ALLOCATION_HOST=0.0.0.0 \
    ALLOCATION_PORT=8000 \
    ALLOCATION_SCENARIO_DIR=/app/scenarios \
    ALLOCATION_POLICY=/app/artifacts/er_policy.D_672ev_pop48.json \
    ALLOCATION_LIVE_POLICY=1

# Non-root. The API reads YAML from two fixed directories and writes nothing; there is no
# reason for it to own any of it.
RUN useradd --create-home --uid 10001 allocation

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Scenarios are data, not code: mounting a directory over this one is the supported way to
# add them, and `resolve_scenario` will still only accept names from inside it.
COPY --chown=allocation:allocation scenarios ./scenarios

# The served weights. Baked in rather than mounted: ALLOCATION_POLICY names a file the
# process refuses to start without, and a bind mount that is absent on one host is a
# deployment that silently falls back to the heuristic. One file, read-only, 4.9 KB.
COPY --chown=allocation:allocation artifacts/er_policy.D_672ev_pop48.json ./artifacts/

USER allocation
EXPOSE 8000

# /health is deliberately outside the API-key gate so this keeps working when one is set.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# Exec form, no shell: PID 1 is Python, so SIGTERM reaches uvicorn and `docker stop` takes a
# second instead of the ten it waits before SIGKILL.
CMD ["python", "-m", "allocation.api"]
