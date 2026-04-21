"""
Orchestrator service.

Ties voice commands, perception, and telemetry together. On every new user
command, it asks the VLM to decide what to do given the current state, then
dispatches the resulting ToolCalls to the flight bridge via REQ/REP.

Threads:
    * command_thread   — PULLs voice commands from the STT service queue.
    * scene_thread     — SUBs the perception scene channel.
    * telemetry_thread — SUBs the flight bridge telemetry.
    * frame_thread     — pulls RGB frames from the sensor subscriber so the VLM
                          has pixel context on every decision.
    * main thread      — decision loop: wake on a new command, call VLM,
                          dispatch tool calls, publish status.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import queue
import signal
import sys
import threading
import time

import zmq

from lexaire import logs, transport
from lexaire.config import load_config
from lexaire.messages import (
    OrchestratorStatus, ToolCall, ToolResult,
    VoiceCommand, decode_header, encode_header, now_ns,
)
from lexaire.subscriber import SensorSubscriber

from .tools import tool_schemas
from .vlm_base import VLM, VlmContext, VlmDecision
from .vlm_dummy import DummyVLM


def _build_vlm(cfg) -> VLM:
    provider = cfg.get("perception.vlm.provider", "dummy")
    if provider == "dummy":
        return DummyVLM()
    if provider == "gemini":
        from .vlm_gemini import build_gemini
        return build_gemini(cfg)
    if provider in ("claude", "openai"):
        raise NotImplementedError(f"{provider} backend not implemented yet")
    raise ValueError(f"unknown vlm provider: {provider}")


class Orchestrator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logs.configure("orchestrator", cfg.get("logging.level", "INFO"))
        self.vlm = _build_vlm(cfg)

        self.stop_event = threading.Event()
        self.command_q: queue.Queue[VoiceCommand] = queue.Queue(maxsize=16)
        self.history: list[str] = []

        self.latest_scene: dict = {}
        self.latest_scene_lock = threading.Lock()

        self.latest_telem: dict = {}
        self.latest_telem_lock = threading.Lock()

        # Sensor subscriber: we hold onto the latest frame for the VLM.
        self.sub = SensorSubscriber(
            rgb_endpoint=cfg.sensor.channels.rgb,
            depth_endpoint=cfg.sensor.channels.depth,
            imu_endpoint=cfg.sensor.channels.imu,
        )
        self.latest_fs = None
        self.latest_fs_lock = threading.Lock()

        # Sockets
        self.command_pull = transport.pull(cfg.get("services.orchestrator_command_pull",
                                                    "tcp://127.0.0.1:6200"))
        self.status_pub = transport.pub(cfg.get("services.orchestrator_status_pub",
                                                 "tcp://127.0.0.1:6102"))
        self.scene_sub = transport.sub(cfg.get("services.perception_scene_pub",
                                                "tcp://127.0.0.1:6100"))
        self.telem_sub = transport.sub(cfg.get("services.telemetry_pub",
                                                "tcp://127.0.0.1:6101"))
        self.flight_req_endpoint = cfg.get("services.flight_bridge_rep",
                                            "tcp://127.0.0.1:6300")

        self.safety_snapshot = {
            "max_altitude_m":          float(cfg.get("safety.max_altitude_m", 5.0)),
            "geofence_radius_m":       float(cfg.get("safety.geofence_radius_m", 10.0)),
            "max_velocity_mps":        float(cfg.get("safety.max_velocity_mps", 1.5)),
            "min_obstacle_distance_m": float(cfg.get("safety.min_obstacle_distance_m", 0.5)),
        }

    # -- Lifecycle ------------------------------------------------------------

    def start(self):
        self.sub.start()
        self._threads: list[threading.Thread] = []
        self._spawn(self._run_command_thread,   "orch-command")
        self._spawn(self._run_scene_thread,     "orch-scene")
        self._spawn(self._run_telemetry_thread, "orch-telem")
        self._spawn(self._run_frame_thread,     "orch-frame")
        self._publish_status("idle", "startup")
        self.log.info("orchestrator up  vlm=%s", type(self.vlm).__name__)

    def _spawn(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self):
        self.stop_event.set()
        self.sub.stop()
        for t in self._threads:
            t.join(timeout=1.5)
        for s in (self.command_pull, self.status_pub, self.scene_sub, self.telem_sub):
            try: s.close()
            except Exception: pass

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
                self.command_q.put(cmd, timeout=0.5)
            except queue.Full:
                self.log.warning("command queue full — dropping")
            except Exception as e:
                self.log.warning("command decode error: %s", e)

    def _run_scene_thread(self):
        poller = zmq.Poller()
        poller.register(self.scene_sub, zmq.POLLIN)
        while not self.stop_event.is_set():
            events = dict(poller.poll(250))
            if self.scene_sub not in events:
                continue
            try:
                frame = self.scene_sub.recv()
                hdr = decode_header(frame)
                with self.latest_scene_lock:
                    self.latest_scene = hdr
            except Exception as e:
                self.log.warning("scene decode error: %s", e)

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
            except Exception as e:
                self.log.warning("telem decode error: %s", e)

    def _run_frame_thread(self):
        while not self.stop_event.is_set():
            fs = self.sub.get_nowait()
            if fs is None:
                time.sleep(0.02)
                continue
            with self.latest_fs_lock:
                self.latest_fs = fs

    # -- Decision loop --------------------------------------------------------

    def run(self):
        while not self.stop_event.is_set():
            try:
                cmd = self.command_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._on_command(cmd)

    def _on_command(self, cmd: VoiceCommand):
        abort_kw = self.cfg.get("stt.abort_keyword", "abort").lower()
        if cmd.is_abort or abort_kw in cmd.text.lower():
            self._publish_status("aborted", f"abort keyword: {cmd.text!r}")
            self._dispatch_tool_call(ToolCall(
                request_id=VLM.new_request_id(),
                name="abort",
                args={},
            ))
            return

        ctx = self._build_context(cmd)
        self._publish_status("thinking", f"reasoning about: {cmd.text!r}")
        decision = self.vlm.decide(ctx)
        self.log.info("decision: %s -> %d tool_calls", decision.thought, len(decision.tool_calls))

        self._publish_status("executing", decision.thought)
        for tc in decision.tool_calls:
            self._dispatch_tool_call(tc)

        self.history.append(f"user: {cmd.text} | thought: {decision.thought} | calls: "
                            f"{[tc.name for tc in decision.tool_calls]}")
        # keep history bounded
        self.history[:] = self.history[-20:]

        self._publish_status("idle", "waiting")

    def _build_context(self, cmd: VoiceCommand) -> VlmContext:
        with self.latest_scene_lock:
            scene = dict(self.latest_scene)
        with self.latest_telem_lock:
            telem = dict(self.latest_telem)
        with self.latest_fs_lock:
            rgb = self.latest_fs.rgb.copy() if self.latest_fs is not None else None
        return VlmContext(
            user_command=cmd.text,
            telemetry=telem,
            scene=scene,
            rgb=rgb,
            history=list(self.history),
            safety=dict(self.safety_snapshot),
        )

    def _dispatch_tool_call(self, tc: ToolCall) -> ToolResult:
        """Open a short-lived REQ socket per call (simpler than managing a persistent one)."""
        req = transport.req(self.flight_req_endpoint, timeout_ms=3000)
        try:
            req.send(json.dumps({
                "request_id": tc.request_id,
                "name": tc.name,
                "args": tc.args,
            }).encode("utf-8"))
            reply = req.recv()
            data = json.loads(reply.decode("utf-8"))
            result = ToolResult(
                request_id=data.get("request_id", tc.request_id),
                ok=bool(data.get("ok", False)),
                error=data.get("error"),
                data=data.get("data"),
            )
            if not result.ok:
                self.log.warning("tool %s failed: %s", tc.name, result.error)
            else:
                self.log.info("tool %s ok", tc.name)
            return result
        except zmq.error.Again:
            self.log.warning("tool %s timed out", tc.name)
            return ToolResult(request_id=tc.request_id, ok=False, error="timeout")
        finally:
            req.close()

    def _publish_status(self, state: str, note: str):
        msg = OrchestratorStatus(
            ts_ns=now_ns(),
            state=state,
            last_thought=note,
            last_action=self.history[-1] if self.history else "",
        )
        try:
            self.status_pub.send(encode_header(msg))
        except Exception:
            pass


def cli() -> int:
    p = argparse.ArgumentParser(description="Lexaire orchestrator")
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
