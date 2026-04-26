"""
Recording file format.

One JSONL line per ZMQ message. Each record is:

    {"channel": "rgb"|"depth"|"imu",
     "ts_ns":   <int, monotonic reference clock>,
     "header":  <decoded JSON header>,
     "payload": <base64-encoded bytes>}

`ts_ns` is our *recording-side* timestamp (time.monotonic_ns at recv), not the
sensor's hardware timestamp. We use it to schedule playback; the header keeps
whatever ts_ns the sensor stamped originally.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import IO, Iterator


@dataclass
class Record:
    channel: str
    ts_ns: int
    header: dict
    payload: bytes

    def to_line(self) -> str:
        return json.dumps(
            {
                "channel": self.channel,
                "ts_ns": self.ts_ns,
                "header": self.header,
                "payload": base64.b64encode(self.payload).decode("ascii"),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_line(cls, line: str) -> "Record":
        obj = json.loads(line)
        return cls(
            channel=obj["channel"],
            ts_ns=int(obj["ts_ns"]),
            header=obj["header"],
            payload=base64.b64decode(obj["payload"]),
        )


def write(fp: IO[str], rec: Record) -> None:
    fp.write(rec.to_line())
    fp.write("\n")


def read_all(path: str) -> Iterator[Record]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            yield Record.from_line(line)
