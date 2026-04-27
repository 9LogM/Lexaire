FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build-time toolchain only — runtime is pure-Python (zstandard et al may
# fall back to sdist on some platforms).
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

RUN pip install -e ".[gemini,detector-yolo,stt-whisper]" \
    && pip uninstall -y opencv-python opencv-python-headless \
    && pip install --no-cache-dir opencv-python-headless

# `ultralytics` declares `opencv-python` (the GUI variant) as a hard dep,
# which needs libxcb/libGL and crashes on import in slim-bookworm.
# `opencv-python-headless` provides the same `cv2` module headlessly. Both
# packages own the same `cv2/` directory, so we uninstall both then reinstall
# only the headless variant — the directory ends up owned by headless alone.

# Config is mounted read-only from the host at runtime; copy is a fallback.
COPY common /workspace/common

WORKDIR /workspace

# Default command is a no-op — compose/override picks the actual service.
CMD ["python", "-c", "print('lexaire-python image ready; specify a command')"]
