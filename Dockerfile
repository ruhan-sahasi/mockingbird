# The server image: FastAPI under uvicorn, for Fargate on ARM64.
#
#   docker buildx build --platform linux/arm64 -t mockingbird:test .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /bin/uv

# Compile the dependencies' bytecode once, at build time (the unprivileged user below could not
# write it at run time), copy packages out of uv's cache (a build-only mount, so the cache never
# lands in the image), and use the image's own Python, never a downloaded one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The dependencies get their own layer, so a code change does not reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# The package is installed in editable mode, so it runs from /app/server/mockingbird. That keeps
# server/data (questions.yaml, rubric.yaml) and server/migrations where the code looks for them:
# next to the package.
COPY server/mockingbird server/mockingbird
COPY server/data server/data
COPY server/migrations server/migrations
COPY scripts/entrypoint.sh scripts/entrypoint.sh
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# At run time, `uv run` must not re-sync (by default it would, installing the dev group on every
# start), and it gets a throwaway cache, since the unprivileged user below has no home for one.
ENV UV_NO_SYNC=1 \
    UV_NO_CACHE=1

RUN useradd --system --no-create-home --uid 10001 app
USER app

EXPOSE 8000

# The entrypoint applies migrations when DATABASE_URL is set, then runs CMD.
# --no-access-log: the access log would record the Vapi paths' secrets and the socket's token.
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "mockingbird.app:app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
