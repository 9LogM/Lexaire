"""
STT service — phase 1 stub.

Reads text lines from stdin (push-to-talk simulation) and pushes a
`VoiceCommand` to the orchestrator over ZMQ PUSH. Whisper-backed STT is a
phase-2 task; for now the config option `stt.backend = whisper` raises.

Modes:
    --once TEXT         Send a single command and exit. Good for scripted tests.
    --from-file PATH    Send one command per non-blank line, with `--interval`
                        seconds between them. Lines starting with `#` are
                        treated as comments.
    (default)           Interactive: read a line at a time from stdin and push
                        each one. Blank lines are ignored. Ctrl-D / Ctrl-C
                        ends the session.

The abort keyword from `stt.abort_keyword` (default `"abort"`) flips
`is_abort=True` on the outgoing command so the orchestrator can short-circuit
straight to the abort tool without waiting on the VLM.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from lexaire import logs, transport
from lexaire.config import load_config
from lexaire.messages import VoiceCommand, encode_header, now_ns


class SttService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logs.configure("stt", cfg.get("logging.level", "INFO"))
        self.stop_event = threading.Event()

        backend = cfg.get("stt.backend", "dummy")
        if backend == "whisper":
            raise NotImplementedError(
                "stt.backend=whisper is a phase-2 task — set it back to 'dummy' for now"
            )
        if backend != "dummy":
            raise ValueError(f"unknown stt.backend: {backend!r}")

        self.abort_kw = cfg.get("stt.abort_keyword", "abort").lower().strip()
        self.push = transport.push(cfg.get(
            "services.orchestrator_command_pull", "tcp://127.0.0.1:6200"
        ))

    # -- Lifecycle ------------------------------------------------------------

    def stop(self):
        self.stop_event.set()
        try:
            self.push.close()
        except Exception:
            pass

    # -- Send -----------------------------------------------------------------

    def send(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        is_abort = self.abort_kw in text.lower()
        cmd = VoiceCommand(ts_ns=now_ns(), text=text, is_abort=is_abort)
        self.push.send(encode_header(cmd))
        self.log.info("-> %s%s", text, "  [ABORT]" if is_abort else "")
        return True

    # -- Modes ----------------------------------------------------------------

    def run_once(self, text: str) -> int:
        self.send(text)
        return 0

    def run_file(self, path: str, interval_s: float) -> int:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            if self.stop_event.is_set():
                break
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.send(stripped)
            if interval_s > 0 and not self.stop_event.is_set():
                time.sleep(interval_s)
        return 0

    def run_interactive(self) -> int:
        self.log.info(
            "interactive STT stub ready — type a command and hit Enter (Ctrl-D to quit)"
        )
        # Read stdin on a worker thread so SIGINT can still interrupt us.
        def _reader():
            try:
                for line in sys.stdin:
                    if self.stop_event.is_set():
                        return
                    self.send(line)
            except Exception as e:
                self.log.warning("stdin reader error: %s", e)
            finally:
                self.stop_event.set()

        t = threading.Thread(target=_reader, name="stt-stdin", daemon=True)
        t.start()
        self.stop_event.wait()
        return 0


def cli() -> int:
    p = argparse.ArgumentParser(description="Lexaire STT service (phase-1 stub)")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("--once", metavar="TEXT", help="send a single command and exit")
    p.add_argument("--from-file", metavar="PATH", help="read commands from a file, one per line")
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between commands in --from-file mode (default 1.0)")
    args = p.parse_args()

    cfg = load_config(args.config)
    svc = SttService(cfg)

    def _stop(*_):
        svc.stop_event.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        if args.once is not None:
            return svc.run_once(args.once)
        if args.from_file is not None:
            return svc.run_file(args.from_file, args.interval)
        return svc.run_interactive()
    finally:
        svc.stop()


if __name__ == "__main__":
    sys.exit(cli())
