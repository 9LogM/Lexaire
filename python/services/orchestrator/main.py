"""
Orchestrator service (VLA mode).

Runs a continuous control loop at `orchestrator.control_hz`. On every
tick the VLA backend gets the latest RGB frame, the operator's current
spoken instruction (held in context across ticks until the operator
updates it), and telemetry, and emits at most one tool call.

Threads:
    * command_thread   — PULLs voice commands from the STT service queue.
    * telemetry_thread — SUBs the flight bridge telemetry.
    * frame_thread     — pulls RGB frames from the sensor subscriber.
    * main thread      — control loop: tick at control_hz, call VLA,
                          dispatch one tool, publish status.

Voice commands are held as the current `instruction` in the context.
A new voice command replaces the previous instruction; an abort or
the abort-keyword short-circuits straight to the abort tool.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import signal
import sys
import threading
import time
from typing import Optional

import zmq

from lexaire import logs, transport
from lexaire.config import load_config
from lexaire.messages import (
    OrchestratorStatus, ToolCall, ToolResult,
    VoiceCommand, decode_header, encode_header, now_ns,
)
from lexaire.subscriber import SensorSubscriber

from .vla_base import VLA, VlaContext


def _build_vla(cfg) -> VLA:
    from .vla import build_vla
    return build_vla(cfg)


class Orchestrator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logs.configure("orchestrator", cfg.get("logging.level", "INFO"))
        self.vla = _build_vla(cfg)

        self.stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

        # Continuous control: tick rate the main loop drives the VLA at.
        self._control_hz = float(cfg.get("orchestrator.control_hz", 10.0))
        if self._control_hz <= 0:
            raise ValueError("orchestrator.control_hz must be > 0")
        self._control_period_s = 1.0 / self._control_hz

        # Operator's current instruction. Updated by the command thread,
        # read by the main loop. None means "no instruction yet — idle."
        self._instruction: Optional[str] = None
        self._instruction_lock = threading.Lock()

        self.latest_telem: dict = {}
        self.latest_telem_lock = threading.Lock()

        self._zmq = zmq.Context()

        self.sub = SensorSubscriber(
            self._zmq,
            rgb_endpoint=cfg.require("sensor.channels.rgb"),
            depth_endpoint=cfg.require("sensor.channels.depth"),
        )
        self.latest_fs = None
        self.latest_fs_ts: float = 0.0
        self.latest_fs_lock = threading.Lock()
        # Tight default for VLA closed-loop control — a 2 s gate would
        # let the model command setpoints against pixels that are 60
        # ticks old at 30 Hz.
        self._frame_max_age_s = float(
            cfg.get("orchestrator.frame_max_age_s", 0.2))

        self.command_pull = transport.pull(self._zmq, cfg.require("services.orchestrator_command_pull"))
        self.status_pub   = transport.pub(self._zmq, cfg.require("services.orchestrator_status_pub"))
        self.telem_sub    = transport.sub(self._zmq, cfg.require("services.telemetry_pub"))
        self.flight_req_endpoint = cfg.require("services.flight_bridge_rep")

        self.safety_snapshot = {
            "max_altitude_m":    float(cfg.get("safety.max_altitude_m", 5.0)),
            "geofence_radius_m": float(cfg.get("safety.geofence_radius_m", 10.0)),
            "max_velocity_mps":  float(cfg.get("safety.max_velocity_mps", 1.5)),
        }

        self._bridge_offline_flag = False
        self._bridge_lock = threading.Lock()

        self._req_socket: Optional[zmq.Socket] = None

        # Word-boundary so "laboratory"/"abortive" don't trip. Empty/
        # whitespace keyword would compile to `\b\b` and match every
        # word boundary.
        abort_kw = cfg.get("stt.abort_keyword", "abort").strip()
        if not abort_kw:
            raise ValueError(
                "stt.abort_keyword is empty/whitespace — would flag every "
                "command as abort. Set a non-empty keyword in config.yaml."
            )
        self._abort_pattern = re.compile(
            rf"\b{re.escape(abort_kw)}\b", re.IGNORECASE)

        # Voice-arm gate. Held until disarm/abort, not per command. If
        # the operator says "arm" once, subsequent commands inherit the
        # authorization until an explicit disarm or an abort fires.
        self._arm_pattern = re.compile(r"\barm\b", re.IGNORECASE)
        self._arm_authorized = False
        self._arm_authorized_lock = threading.Lock()

    # -- Lifecycle ------------------------------------------------------------

    def start(self):
        self.sub.start()
        self._spawn(self._run_command_thread,   "orch-command")
        self._spawn(self._run_telemetry_thread, "orch-telem")
        self._spawn(self._run_frame_thread,     "orch-frame")
        self._publish_status("idle", "startup")
        self.log.info("orchestrator up  vla=%s  control_hz=%.1f",
                      type(self.vla).__name__, self._control_hz)

    def _spawn(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self):
        self.stop_event.set()
        self.sub.stop()
        for t in self._threads:
            t.join(timeout=1.5)
        for s in (self.command_pull, self.status_pub, self.telem_sub):
            if s is not None:
                s.close()
        self._reset_req_socket()
        self._zmq.term()

    # -- Sockets --------------------------------------------------------------

    def _run_command_thread(self):
        poller = zmq.Poller()
        poller.register(self.command_pull, zmq.POLLIN)
        while not self.stop_event.is_set():
            events = dict(poller.poll(250))
            if self.command_pull not in events:
                continue
            try:
                frame = self.command_pull.recv()
                hdr = decode_header(frame)
                cmd = VoiceCommand(
                    ts_ns=int(hdr.get("ts_ns", now_ns())),
                    text=str(hdr.get("text", "")),
                    is_abort=bool(hdr.get("is_abort", False)),
                )
                self.log.info("command: %r (abort=%s)", cmd.text, cmd.is_abort)
                # Short-circuit aborts: dispatch directly, clear the
                # arm-authorization, drop instruction.
                if cmd.is_abort or self._abort_pattern.search(cmd.text):
                    self._on_abort(cmd)
                    continue
                # Latch arm authorization on first "arm" until disarm/abort.
                if self._arm_pattern.search(cmd.text):
                    with self._arm_authorized_lock:
                        self._arm_authorized = True
                # Replace the current instruction. Continuous: VLA picks
                # this up on its next tick; no per-command mission.
                with self._instruction_lock:
                    self._instruction = cmd.text
            except Exception:
                self.log.exception("command decode error")

    def _on_abort(self, cmd: VoiceCommand):
        """Abort fast-path. Bypasses the VLA, dispatches abort directly,
        clears in-flight instruction and arm authorization."""
        self._publish_status("aborted", f"abort keyword: {cmd.text!r}")
        with self._instruction_lock:
            self._instruction = None
        with self._arm_authorized_lock:
            self._arm_authorized = False
        result = self._dispatch_tool_call(ToolCall(
            request_id=VLA.new_request_id(),
            name="abort",
            args={},
        ))
        if not result.ok:
            if self._bridge_offline():
                self._publish_status(
                    "bridge_offline",
                    f"abort {cmd.text!r} did not reach the flight bridge",
                )
            else:
                self._publish_status(
                    "abort_failed",
                    f"abort rejected by bridge: {result.error}",
                )

    def _run_telemetry_thread(self):
        poller = zmq.Poller()
        poller.register(self.telem_sub, zmq.POLLIN)
        while not self.stop_event.is_set():
            events = dict(poller.poll(250))
            if self.telem_sub not in events:
                continue
            try:
                frame = self.telem_sub.recv()
                hdr = decode_header(frame)
                with self.latest_telem_lock:
                    self.latest_telem = hdr
            except Exception:
                self.log.exception("telem decode error")

    def _run_frame_thread(self):
        while not self.stop_event.is_set():
            fs = self.sub.get(timeout=0.25)
            if fs is None:
                continue
            now = time.monotonic()
            with self.latest_fs_lock:
                self.latest_fs = fs
                self.latest_fs_ts = now

    # -- Control loop ---------------------------------------------------------

    def run(self):
        """Continuous control loop at control_hz. On each tick:
        1. Build context from latest frame / telemetry / instruction.
        2. Call VLA.decide().
        3. Dispatch the resulting tool call (if any).
        4. Sleep to the next tick.
        """
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            ctx = self._build_context()
            try:
                decision = self.vla.decide(ctx)
            except Exception as e:
                self.log.exception("VLA decide() raised unhandled: %s", e)
                self._publish_status("vla_error", f"vla_error: {type(e).__name__}")
                # Don't kill the loop on a single bad inference; the
                # next tick may recover. Skip this tick's dispatch.
                next_tick += self._control_period_s
                self._sleep_to_next_tick(next_tick)
                continue

            if decision.tool_call is not None:
                self._publish_status("executing",
                                      decision.thought or decision.tool_call.name)
                self._dispatch_tool_call(decision.tool_call)
            else:
                self._publish_status("idle", decision.thought or "idle")

            next_tick += self._control_period_s
            self._sleep_to_next_tick(next_tick)

    def _sleep_to_next_tick(self, next_tick: float):
        """Sleep until next_tick (monotonic), responsive to stop_event."""
        while not self.stop_event.is_set():
            now = time.monotonic()
            remaining = next_tick - now
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.05))

    def _build_context(self) -> VlaContext:
        with self.latest_telem_lock:
            telem = dict(self.latest_telem)
        with self.latest_fs_lock:
            age = (time.monotonic() - self.latest_fs_ts
                   if self.latest_fs is not None else None)
            rgb = (self.latest_fs.rgb.copy()
                   if self.latest_fs is not None and age is not None
                       and age <= self._frame_max_age_s
                   else None)
        with self._instruction_lock:
            instruction = self._instruction
        return VlaContext(
            instruction=instruction,
            rgb=rgb,
            telemetry=telem,
            safety=dict(self.safety_snapshot),
        )

    # -- Tool dispatch --------------------------------------------------------

    def _bridge_offline(self) -> bool:
        with self._bridge_lock:
            return self._bridge_offline_flag

    def _ensure_req_socket(self) -> zmq.Socket:
        if self._req_socket is None:
            s = self._zmq.socket(zmq.REQ)
            try:
                s.setsockopt(zmq.LINGER, 0)
                s.setsockopt(zmq.RCVTIMEO, 3000)
                s.setsockopt(zmq.SNDTIMEO, 3000)
                s.connect(self.flight_req_endpoint)
            except Exception:
                try:
                    s.close(linger=0)
                except Exception:
                    pass
                raise
            self._req_socket = s
        return self._req_socket

    def _reset_req_socket(self) -> None:
        if self._req_socket is not None:
            try:
                self._req_socket.close(linger=0)
            except zmq.ZMQError as e:
                self.log.warning("REQ close failed: %s", e)
            self._req_socket = None

    def _dispatch_tool_call(self, tc: ToolCall) -> ToolResult:
        if self._bridge_offline():
            self.log.warning("tool %s rejected: bridge_offline", tc.name)
            self._publish_status("bridge_offline",
                                  f"refusing {tc.name}: prior dispatch timed out")
            return ToolResult(request_id=tc.request_id, ok=False, error="bridge_offline")

        # voice_confirmed is the orchestrator's authoritative signal —
        # overwrite unconditionally on arm so a VLA-emitted value can't
        # bypass the bridge's spoken-arm gate.
        args = dict(tc.args) if tc.args else {}
        if tc.name == "arm":
            with self._arm_authorized_lock:
                args["voice_confirmed"] = self._arm_authorized
        # Disarm clears the arm-authorization latch on the orchestrator
        # side too (the bridge clears its own armed_with_voice via
        # subscribe_armed; this keeps the two in step).
        if tc.name == "disarm":
            with self._arm_authorized_lock:
                self._arm_authorized = False

        try:
            req = self._ensure_req_socket()
            req.send(json.dumps({
                "request_id": tc.request_id,
                "name": tc.name,
                "args": args,
            }).encode("utf-8"))
            reply = req.recv()
            data = json.loads(reply.decode("utf-8"))
            if not isinstance(data, dict):
                raise TypeError(f"non-dict reply: {type(data).__name__}")
            result = ToolResult(
                request_id=data.get("request_id", tc.request_id),
                ok=bool(data.get("ok", False)),
                error=data.get("error"),
                data=data.get("data"),
            )
            with self._bridge_lock:
                self._bridge_offline_flag = False
            if not result.ok:
                self.log.warning("tool %s failed: %s", tc.name, result.error)
            else:
                self.log.debug("tool %s ok", tc.name)
            return result
        except zmq.error.Again:
            self.log.warning("tool %s timed out", tc.name)
            self._reset_req_socket()
            with self._bridge_lock:
                self._bridge_offline_flag = True
            self._publish_status("bridge_offline",
                                  f"{tc.name} timed out — bridge unreachable")
            return ToolResult(request_id=tc.request_id, ok=False, error="timeout")
        except zmq.ZMQError as e:
            self.log.warning("tool %s zmq error: %r", tc.name, e)
            self._reset_req_socket()
            with self._bridge_lock:
                self._bridge_offline_flag = True
            self._publish_status("bridge_offline",
                                  f"{tc.name} zmq error: {e!r}")
            return ToolResult(
                request_id=tc.request_id, ok=False, error=f"wire_error: {e!r}"
            )
        except (ValueError, TypeError) as e:
            # Bridge replied — just garbled. Reset REQ state but don't
            # flag offline; the next dispatch should try again.
            self.log.warning("tool %s parse error: %r", tc.name, e)
            self._reset_req_socket()
            return ToolResult(
                request_id=tc.request_id, ok=False, error=f"wire_error: {e!r}"
            )

    def _publish_status(self, state: str, note: str):
        msg = OrchestratorStatus(
            ts_ns=now_ns(),
            state=state,
            last_thought=note,
        )
        try:
            self.status_pub.send(encode_header(msg))
        except zmq.ZMQError as e:
            self.log.warning("status_pub send failed (%s): %r", e, msg)


def cli() -> int:
    p = argparse.ArgumentParser(description="Lexaire orchestrator (VLA)")
    p.add_argument("--config", help="path to config.yaml")
    args = p.parse_args()

    cfg = load_config(args.config)
    orch = Orchestrator(cfg)

    def _stop(*_):
        orch.stop_event.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    orch.start()
    try:
        orch.run()
    finally:
        orch.stop()
    return 0


if __name__ == "__main__":
    sys.exit(cli())
