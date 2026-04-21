"""
YAML config loader with dot-notation access and env-var resolution.

Usage:
    cfg = load_config()           # finds common/config.yaml from cwd or repo root
    cfg.sensor.channels.rgb       # -> "tcp://drone.local:5555"
    cfg.perception.vlm.provider   # -> "gemini"
    cfg.get("safety.max_altitude_m", default=5.0)

API keys and other secrets live in a gitignored .env file in the repo root.
Fields whose name ends in `_env` are resolved to os.environ[value] before use.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import yaml


class _AttrDict(dict):
    """dict that also allows attribute access. Nested dicts are wrapped lazily."""

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
        """Dotted-path lookup with default."""
        cur: Any = self
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def resolve_env(self, key: str) -> str | None:
        """Read a sibling `<key>_env` field and look up the env var."""
        env_name = self.get(f"{key}_env")
        if env_name is None:
            return None
        return os.environ.get(env_name)


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

    # Walk up from cwd looking for common/config.yaml (up to 5 levels).
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
                v = v.strip().strip('"').strip("'")
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

    cfg = _wrap(raw)
    cfg["_path"] = str(config_path)
    return cfg


def walk_leaves(d: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield (dotted_key, value) for every non-dict leaf."""
    if isinstance(d, dict):
        for k, v in d.items():
            if k.startswith("_"):
                continue
            yield from walk_leaves(v, f"{prefix}.{k}" if prefix else k)
    else:
        yield prefix, d
