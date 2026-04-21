FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build-time toolchain needed only to wheel-build a few deps (zstandard et al
# may fall back to sdist on some platforms).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/python

# Copy dependency manifest first so dep install caches across code changes.
COPY python/pyproject.toml ./

# Installing an editable package from only pyproject requires the package
# tree to exist. Copy the package source, then install with the optional
# feature sets we need at runtime.
COPY python/lexaire ./lexaire
COPY python/services ./services

RUN pip install -e ".[gemini,perception]"

# Config is mounted read-only from the host at runtime; copy is a fallback.
COPY common /workspace/common

WORKDIR /workspace

# Default command is a no-op — compose/override picks the actual service.
CMD ["python", "-c", "print('lexaire-python image ready; specify a command')"]
