"""
STT service.

Reads commands from stdin, a scripted file, or a WAV clip (Whisper), and
pushes a `VoiceCommand` to the orchestrator over ZMQ PUSH.

Modes:
    --once TEXT          Send a single command and exit.
    --from-file PATH     Send one command per non-blank line, with --interval
                         seconds between.
    --audio-file PATH    Transcribe a WAV clip via faster-whisper and push
                         the result. Requires `pip install 'lexaire[stt-whisper]'`.
                         Microphone capture is a follow-up (Docker audio
                         passthrough is host-specific; file mode is portable).
    (default)            Interactive: read a line at a time from stdin and
                         push each one. Ctrl-D / Ctrl-C ends the session.

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

import zmq

from lexaire import logs, transport
from lexaire.config import load_config
from lexaire.messages import VoiceCommand, encode_header, now_ns


class SttService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logs.configure("stt", cfg.get("logging.level", "INFO"))
        self.stop_event = threading.Event()

        # Whisper backend is built lazily — only --audio-file mode loads it,
        # so text-mode runs don't waste seconds + GPU memory.
        self._whisper = None

        self.abort_kw = cfg.get("stt.abort_keyword", "abort").lower().strip()
        self._zmq = zmq.Context()
        self.push = transport.push(self._zmq, cfg.require("services.orchestrator_command_pull"))

    def _get_whisper(self):
        if self._whisper is not None:
            return self._whisper
        from .whisper_backend import FasterWhisperBackend  # lazy import
        self._whisper = FasterWhisperBackend(
            model_size=self.cfg.get("stt.whisper_model", "small"),
            device=self.cfg.get("stt.whisper_device", "auto"),
            compute_type=self.cfg.get("stt.whisper_compute_type", None),
            language=self.cfg.get("stt.whisper_language", None),
        )
        return self._whisper

    # -- Lifecycle ------------------------------------------------------------

    def stop(self):
        self.stop_event.set()
        self.push.close()
        self._zmq.term()

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
        # Give PUSH a beat to finish connecting before we send + exit, otherwise
        # a fresh one-shot process can drop the message during teardown.
        time.sleep(0.15)
        self.send(text)
        # And a short hold so the LINGER drain window has time to run.
        time.sleep(0.2)
        return 0

    def run_audio_file(self, path: str) -> int:
        from .whisper_backend import load_wav_as_f32_mono_16k

        whisper = self._get_whisper()
        audio = load_wav_as_f32_mono_16k(path)
        text = whisper.transcribe(audio)
        if not text:
            self.log.warning("whisper: no speech detected in %s", path)
            return 0
        self.log.info("whisper: %s", text)
        time.sleep(0.15)
        self.send(text)
        time.sleep(0.2)
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
            "interactive STT ready — type a command and hit Enter (Ctrl-D to quit)"
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
    p = argparse.ArgumentParser(description="Lexaire STT service")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("--once", metavar="TEXT", help="send a single command and exit")
    p.add_argument("--from-file", metavar="PATH", help="read commands from a file, one per line")
    p.add_argument("--audio-file", metavar="PATH",
                   help="transcribe a WAV clip via the whisper backend and push the result")
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
        if args.audio_file is not None:
            return svc.run_audio_file(args.audio_file)
        if args.once is not None:
            return svc.run_once(args.once)
        if args.from_file is not None:
            return svc.run_file(args.from_file, args.interval)
        return svc.run_interactive()
    finally:
        svc.stop()


if __name__ == "__main__":
    sys.exit(cli())
