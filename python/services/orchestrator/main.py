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
import collections
import dataclasses
import json
import queue
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

from .vlm_base import VLM, VlmContext


# Tool calls that end a mission cleanly. After any of these the orchestrator
# stops re-prompting the VLM and returns to idle.
_TERMINAL_TOOLS = frozenset({"hold", "land", "abort", "return_to_launch", "kill", "disarm"})


@dataclasses.dataclass
class OrchestratorMission:
    """Active multi-step mission state. Surfaced to the VLM via VlmContext."""
    goal_text: str
    started_ts_ns: int
    current_step: int = 0
    step_history: list[dict] = dataclasses.field(default_factory=list)

    def record(self, tool_name: str, args: dict, result: ToolResult) -> None:
        self.step_history.append({
            "tool": tool_name,
            "args": args,
            "ok": result.ok,
            "error": result.error,
            "ts_ns": now_ns(),
        })
        self.current_step += 1

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _build_vlm(cfg) -> VLM:
    provider = cfg.get("perception.vlm.provider", "gemini")
    if provider == "gemini":
        from .vlm_gemini import build_gemini
        return build_gemini(cfg)
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
        # Telemetry ring buffer (oldest -> newest), pruned by wallclock age in
        # _run_telemetry_thread. Configured depth in seconds; the buffer
        # holds however many samples the bridge published in that window.
        self._telem_history_s = float(cfg.get("orchestrator.telemetry_history_seconds", 5.0))
        self._telem_history: collections.deque = collections.deque()

        # Hold onto the latest frame so the VLM sees current pixels.
        self.sub = SensorSubscriber(
            rgb_endpoint=cfg.sensor.channels.rgb,
            depth_endpoint=cfg.sensor.channels.depth,
        )
        self.latest_fs = None
        self.latest_fs_lock = threading.Lock()

        # Sockets
        # NOTE: command_pull is deferred — it's bound inside _run_command_thread
        # on a fresh zmq.Context to work around a reception failure when the
        # socket lives on the shared zmq.Context.instance() singleton.
        self.command_pull = None
        self.status_pub = transport.pub(cfg.get("services.orchestrator_status_pub",
                                                 "tcp://127.0.0.1:6102"))
        self.scene_sub = transport.sub(cfg.get("services.perception_scene_pub",
                                                "tcp://127.0.0.1:6100"))
        self.telem_sub = transport.sub(cfg.get("services.telemetry_pub",
                                                "tcp://127.0.0.1:6101"))
        self.flight_req_endpoint = cfg.get("services.flight_bridge_rep",
                                            "tcp://127.0.0.1:6300")

        self.safety_snapshot = {
            "max_altitude_m":    float(cfg.get("safety.max_altitude_m", 5.0)),
            "geofence_radius_m": float(cfg.get("safety.geofence_radius_m", 10.0)),
            "max_velocity_mps":  float(cfg.get("safety.max_velocity_mps", 1.5)),
        }

        # Bridge-offline detection: any successful tool reply updates this
        # timestamp; if no success within bridge_offline_threshold_s, the
        # orchestrator surfaces "bridge_offline" on its status PUB and rejects
        # new tool dispatches until a reply lands. Initialized to "now" so
        # we don't false-positive on startup before the first tool call.
        self._bridge_offline_threshold_s = float(
            cfg.get("orchestrator.bridge_offline_threshold_s", 3.0)
        )
        self._last_bridge_ok_ts = time.monotonic()
        self._bridge_lock = threading.Lock()

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
            if s is not None:
                s.close()

    # -- Sockets --------------------------------------------------------------

    def _run_command_thread(self):
        # Bind PULL on a fresh zmq.Context rather than the shared instance().
        # Observed: PULL sockets created on zmq.Context.instance() in this
        # process silently drop reception — poll() never fires POLLIN even
        # though the socket is listening and writes succeed on the wire.
        _cmd_ep = self.cfg.get("services.orchestrator_command_pull", "tcp://127.0.0.1:6200")
        from urllib.parse import urlparse as _urlparse
        _u = _urlparse(_cmd_ep)
        _bind_ep = f"tcp://*:{_u.port}" if _u.port else _cmd_ep
        _local_ctx = zmq.Context()
        _pull = _local_ctx.socket(zmq.PULL)
        _pull.setsockopt(zmq.RCVHWM, 16)
        _pull.setsockopt(zmq.LINGER, 0)
        _pull.bind(_bind_ep)
        self.command_pull = _pull
        poller = zmq.Poller()
        poller.register(_pull, zmq.POLLIN)
        while not self.stop_event.is_set():
            events = dict(poller.poll(250))
            if _pull not in events:
                continue
            try:
                frame = _pull.recv()
                hdr = decode_header(frame)
                cmd = VoiceCommand(
                    ts_ns=int(hdr.get("ts_ns", now_ns())),
                    text=str(hdr.get("text", "")),
                    is_abort=bool(hdr.get("is_abort", False)),
                )
                self.log.info("command: %r (abort=%s)", cmd.text, cmd.is_abort)
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
                now = time.monotonic()
                with self.latest_telem_lock:
                    self.latest_telem = hdr
                    self._telem_history.append((now, hdr))
                    # Prune samples older than the configured window. The
                    # bridge publishes at ~10 Hz so the buffer naturally
                    # grows to ~10*window-seconds entries.
                    cutoff = now - self._telem_history_s
                    while self._telem_history and self._telem_history[0][0] < cutoff:
                        self._telem_history.popleft()
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

        max_steps = int(self.cfg.get("orchestrator.mission_max_steps", 10))
        mission = OrchestratorMission(goal_text=cmd.text, started_ts_ns=now_ns())
        terminal_call: Optional[str] = None

        # Re-prompt loop: after each tool result we feed the new state back to
        # the VLM so it can react. Bounded by mission_max_steps to cap Gemini
        # API calls per mission. Preempted by a fresh voice command in the
        # queue (the next iteration of run() will pick it up) or by
        # orchestrator shutdown.
        while mission.current_step < max_steps and not self.stop_event.is_set():
            if not self.command_q.empty():
                self.log.info("mission preempted by new voice command")
                break

            ctx = self._build_context(cmd, mission=mission)
            step_label = f"step {mission.current_step + 1}/{max_steps}"
            self._publish_status("thinking", f"{step_label}: {cmd.text!r}")
            decision = self.vlm.decide(ctx)
            self.log.info("%s decision: %s -> %d tool_calls",
                          step_label, decision.thought, len(decision.tool_calls))

            if not decision.tool_calls:
                # VLM had nothing to say — treat as natural end of mission.
                break

            self._publish_status("executing", decision.thought)
            offline = False
            for tc in decision.tool_calls:
                if self.stop_event.is_set():
                    break
                result = self._dispatch_tool_call(tc)
                mission.record(tc.name, dict(tc.args or {}), result)
                if result.error == "bridge_offline":
                    offline = True
                    break
                if tc.name in _TERMINAL_TOOLS:
                    terminal_call = tc.name
                    break

            if terminal_call is not None or offline:
                break

        if terminal_call:
            end_reason = f"terminal:{terminal_call}"
        elif self._bridge_offline():
            end_reason = "bridge_offline"
        elif mission.current_step >= max_steps:
            end_reason = f"max_steps={max_steps}"
        else:
            end_reason = "no_tool_calls"
        self.history.append(
            f"user: {cmd.text} | steps: {mission.current_step} | end: {end_reason}"
        )
        self.history[:] = self.history[-20:]
        # Bridge-offline already published its own status during dispatch;
        # let it stick for the TUI rather than overwriting with "idle".
        if end_reason != "bridge_offline":
            self._publish_status("idle", f"mission done ({end_reason})")

    def _build_context(
        self,
        cmd: VoiceCommand,
        mission: Optional[OrchestratorMission] = None,
    ) -> VlmContext:
        with self.latest_scene_lock:
            scene = dict(self.latest_scene)
        with self.latest_telem_lock:
            telem = dict(self.latest_telem)
            telem_hist = [hdr for (_t, hdr) in self._telem_history]
        with self.latest_fs_lock:
            rgb = self.latest_fs.rgb.copy() if self.latest_fs is not None else None
        return VlmContext(
            user_command=cmd.text,
            telemetry=telem,
            scene=scene,
            rgb=rgb,
            history=list(self.history),
            safety=dict(self.safety_snapshot),
            telemetry_history=telem_hist,
            mission=mission.to_dict() if mission is not None else None,
        )

    def _bridge_offline(self) -> bool:
        """True if no successful tool reply within the offline threshold."""
        with self._bridge_lock:
            return (time.monotonic() - self._last_bridge_ok_ts) > self._bridge_offline_threshold_s

    def _dispatch_tool_call(self, tc: ToolCall) -> ToolResult:
        # Refuse to dispatch if the bridge has been silent past threshold —
        # the next round-trip would just hang for the REQ timeout. Caller
        # gets an immediate failure; the watchdog status PUB tells the user.
        if self._bridge_offline():
            self.log.warning("tool %s rejected: bridge_offline", tc.name)
            self._publish_status("bridge_offline",
                                  f"refusing {tc.name}: no bridge reply in "
                                  f"{self._bridge_offline_threshold_s:.1f}s")
            return ToolResult(request_id=tc.request_id, ok=False, error="bridge_offline")

        # Fresh zmq.Context per call — same workaround as the PULL in
        # _run_command_thread: zmq.Context.instance() silently drops traffic
        # on some sockets in this process.
        _ctx_local = zmq.Context()
        req = _ctx_local.socket(zmq.REQ)
        req.setsockopt(zmq.LINGER, 0)
        req.setsockopt(zmq.RCVTIMEO, 3000)
        req.setsockopt(zmq.SNDTIMEO, 3000)
        req.connect(self.flight_req_endpoint)
        # Every arm dispatched by the orchestrator is by definition the result
        # of a spoken voice command — the bridge's safety gate flips on this
        # flag to prove the arm was pilot-initiated, not LLM-hallucinated.
        args = dict(tc.args) if tc.args else {}
        if tc.name == "arm":
            args["voice_confirmed"] = True
        try:
            req.send(json.dumps({
                "request_id": tc.request_id,
                "name": tc.name,
                "args": args,
            }).encode("utf-8"))
            reply = req.recv()
            data = json.loads(reply.decode("utf-8"))
            result = ToolResult(
                request_id=data.get("request_id", tc.request_id),
                ok=bool(data.get("ok", False)),
                error=data.get("error"),
                data=data.get("data"),
            )
            # Any reply (ok or not, including safety_denied) means the bridge
            # is alive — reset the offline watchdog. Only timeouts leave it
            # un-bumped.
            with self._bridge_lock:
                self._last_bridge_ok_ts = time.monotonic()
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
            _ctx_local.term()

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
