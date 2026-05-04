"""
YAML config loader with dot-notation access.

Usage:
    cfg = load_config()           # finds common/config.yaml from cwd or repo root
    cfg.sensor.channels.rgb       # -> "tcp://drone.local:5555"
    cfg.perception.vlm.provider   # -> "gemini"
    cfg.get("safety.max_altitude_m", default=5.0)

The loader also walks up to find a `.env` file and populates `os.environ`
without overwriting existing values. Fields whose name ends in `_env`
(e.g. `perception.vlm.api_key_env`) hold the env-var name; the consumer
reads `os.environ[<value>]` itself — there is no automatic resolution
at load time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


class _AttrDict(dict):
    """dict that also allows attribute access. Nested dicts are wrapped
    eagerly by `_wrap` at load time, but direct `_AttrDict({...})`
    construction skips that — the lazy-wrap below covers that path so
    `_AttrDict({"a": {"b": 1}}).a.b` works."""

    def __getattr__(self, name: str) -> Any:
        try:
            v = self[name]
        except KeyError as e:
            raise AttributeError(name) from e
        if isinstance(v, dict) and not isinstance(v, _AttrDict):
            v = _AttrDict(v)
            self[name] = v
        return v

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted-path lookup with default. YAML-null is treated as missing
        so commenting out a value line falls back to the default rather
        than crashing downstream casts (float(None) etc.)."""
        cur: Any = self
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur if cur is not None else default

    def require(self, path: str) -> Any:
        """Dotted-path lookup that raises if the key is missing or null."""
        sentinel = object()
        v = self.get(path, sentinel)
        if v is sentinel or v is None:
            raise KeyError(f"required config key missing: {path!r}")
        return v


def _wrap(obj: Any) -> Any:
    if isinstance(obj, dict):
        return _AttrDict({k: _wrap(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap(v) for v in obj]
    return obj


def _find_config_path(override: str | None) -> Path:
    if override:
        p = Path(override)
        if p.is_file():
            return p
        raise FileNotFoundError(f"config not found: {override}")

    # Walk up from cwd looking for common/config.yaml (up to 6 levels).
    cur = Path.cwd()
    for _ in range(6):
        candidate = cur / "common" / "config.yaml"
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent

    raise FileNotFoundError(
        "common/config.yaml not found — specify path with LEXAIRE_CONFIG env var "
        "or pass `path=` to load_config()."
    )


def _load_dotenv(start: Path) -> None:
    """Walk up from `start` to find .env; populate os.environ (no overwrite)."""
    cur = start
    for _ in range(6):
        candidate = cur / ".env"
        if candidate.is_file():
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                # Only strip quotes when both ends match — bare `.strip('"')`
                # would corrupt API keys like `foo'bar` or `"fooo`.
                if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                    v = v[1:-1]
                if k and k not in os.environ:
                    os.environ[k] = v
            return
        if cur.parent == cur:
            return
        cur = cur.parent


def load_config(path: str | None = None) -> _AttrDict:
    env_override = os.environ.get("LEXAIRE_CONFIG")
    config_path = _find_config_path(path or env_override)

    _load_dotenv(config_path.parent.parent)

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return _wrap(raw)
