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
import concurrent.futures
import dataclasses
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

from .vlm_base import VLM, VlmContext


# Tool calls that end a mission cleanly. After any of these the orchestrator
# stops re-prompting the VLM and returns to idle.
_TERMINAL_TOOLS = frozenset({"hold", "land", "abort", "return_to_launch", "kill", "disarm"})

# Mission re-prompt loop bails when the same (tool, error) appears at least
# _STUCK_FAILURE_THRESHOLD times in the trailing _STUCK_FAILURE_WINDOW steps.
_STUCK_FAILURE_WINDOW = 4
_STUCK_FAILURE_THRESHOLD = 2


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
        self._threads: list[threading.Thread] = []

        self.latest_scene: dict = {}
        self.latest_scene_lock = threading.Lock()

        self.latest_telem: dict = {}
        self.latest_telem_lock = threading.Lock()
        # Telemetry ring buffer (oldest -> newest), pruned by wallclock age in
        # _run_telemetry_thread. Configured depth in seconds; the buffer
        # holds however many samples the bridge published in that window.
        self._telem_history_s = float(cfg.get("orchestrator.telemetry_history_seconds", 5.0))
        self._telem_history: collections.deque = collections.deque()

        # One private zmq.Context for every socket this orchestrator owns —
        # PUB, SUB, PULL, REQ, plus the SensorSubscriber's. Avoids the
        # process-wide singleton and gives stop() a clean term().
        self._zmq = zmq.Context()

        # Hold onto the latest frame so the VLM sees current pixels.
        self.sub = SensorSubscriber(
            self._zmq,
            rgb_endpoint=cfg.require("sensor.channels.rgb"),
            depth_endpoint=cfg.require("sensor.channels.depth"),
        )
        self.latest_fs = None
        self.latest_fs_lock = threading.Lock()

        self.command_pull = transport.pull(self._zmq, cfg.require("services.orchestrator_command_pull"))
        self.status_pub = transport.pub(self._zmq, cfg.require("services.orchestrator_status_pub"))
        self.scene_sub  = transport.sub(self._zmq, cfg.require("services.perception_scene_pub"))
        self.telem_sub  = transport.sub(self._zmq, cfg.require("services.telemetry_pub"))
        self.flight_req_endpoint = cfg.require("services.flight_bridge_rep")

        self.safety_snapshot = {
            "max_altitude_m":    float(cfg.get("safety.max_altitude_m", 5.0)),
            "geofence_radius_m": float(cfg.get("safety.geofence_radius_m", 10.0)),
            "max_velocity_mps":  float(cfg.get("safety.max_velocity_mps", 1.5)),
        }

        # Set only on REQ timeout, cleared on any reply.
        self._bridge_offline_flag = False
        self._bridge_lock = threading.Lock()

        # Lazy REQ socket; recycled on timeout because REQ doesn't tolerate
        # a missed recv (strict send/recv state machine).
        self._req_socket: Optional[zmq.Socket] = None

        # Word-boundary match so "laboratory" / "abortive" don't trip the
        # abort path. STT also flags is_abort up front; this is the
        # belt-and-suspenders check on text we receive directly.
        # Guard against empty/whitespace abort_keyword: an empty string
        # would compile to `\b\b`, which matches every word boundary,
        # turning every voice command into is_abort=True.
        abort_kw = cfg.get("stt.abort_keyword", "abort").strip()
        if not abort_kw:
            raise ValueError(
                "stt.abort_keyword is empty/whitespace — would treat every "
                "command as abort. Set a non-empty keyword in config.yaml."
            )
        self._abort_pattern = re.compile(
            rf"\b{re.escape(abort_kw)}\b", re.IGNORECASE)

        # Voice-arm authorization. The bridge's safety gate (safety.hpp)
        # only allows arm when args.voice_confirmed is true; this regex
        # decides whether the orchestrator is allowed to set that flag.
        # Set per command in _on_command, consulted in _dispatch_tool_call.
        # Without this, a VLM-emitted arm during an unrelated mission
        # ("take a picture") would still pass the gate.
        self._arm_pattern = re.compile(r"\barm\b", re.IGNORECASE)
        self._current_arm_authorized = False

        # Set by the command thread when an abort arrives so the main
        # thread can abandon any in-flight VLM call instead of waiting
        # out the full RTT (Gemini calls have no built-in timeout).
        self._preempt_event = threading.Event()

        # Two workers so a stale post-preempt VLM call doesn't block the
        # next normal command's submit. Stale futures keep running to
        # completion in the background; we just never read their result.
        self._vlm_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="orch-vlm")

    # -- Lifecycle ------------------------------------------------------------

    def start(self):
        self.sub.start()
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
        self._preempt_event.set()
        # Don't wait for in-flight VLM calls — they own external HTTP RTT.
        self._vlm_executor.shutdown(wait=False, cancel_futures=True)
        self.sub.stop()
        for t in self._threads:
            t.join(timeout=1.5)
        for s in (self.command_pull, self.status_pub, self.scene_sub, self.telem_sub):
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
                if cmd.is_abort:
                    # Abort jumps the queue: drain pending commands so the
                    # next get() returns abort, not the head of the FIFO.
                    drained = 0
                    try:
                        while True:
                            self.command_q.get_nowait()
                            drained += 1
                    except queue.Empty:
                        pass
                    if drained:
                        self.log.warning("abort drained %d pending commands", drained)
                    self.command_q.put_nowait(cmd)
                    # Wake the main thread out of any in-flight VLM call so
                    # the abort doesn't wait for Gemini's RTT (or forever
                    # if the call hangs).
                    self._preempt_event.set()
                else:
                    try:
                        self.command_q.put_nowait(cmd)
                    except queue.Full:
                        self.log.warning("command queue full — dropping")
            except Exception:
                # log.exception preserves traceback so a malformed payload
                # is debuggable; .warning('%s', e) loses the stack.
                self.log.exception("command decode error")

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
            except Exception:
                self.log.exception("scene decode error")

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
            except Exception:
                self.log.exception("telem decode error")

    def _run_frame_thread(self):
        while not self.stop_event.is_set():
            fs = self.sub.get(timeout=0.25)
            if fs is None:
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
        # Each new voice command gets a fresh attempt — without this, a
        # single REQ timeout would lock out the orchestrator forever
        # (the on-success clear in _dispatch_tool_call is unreachable
        # while the bridge_offline short-circuit at the top of that
        # function returns early).
        with self._bridge_lock:
            self._bridge_offline_flag = False

        # Clear any preempt signal from the previous command — we're
        # about to start consuming from the command queue and the
        # main-loop poll below should only react to NEW signals.
        self._preempt_event.clear()

        # Decide arm authorization once per user command. This flips
        # voice_confirmed in _dispatch_tool_call only when the operator
        # actually said "arm" — without this, a VLM-emitted arm during
        # any other utterance ("take a picture") would defeat the gate.
        self._current_arm_authorized = bool(self._arm_pattern.search(cmd.text))

        if cmd.is_abort or self._abort_pattern.search(cmd.text):
            self._publish_status("aborted", f"abort keyword: {cmd.text!r}")
            result = self._dispatch_tool_call(ToolCall(
                request_id=VLM.new_request_id(),
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
            # Aborts are the most safety-critical events; they MUST land
            # in the audit trail even though the rest of the mission state
            # machine is short-circuited above.
            suffix = "" if result.ok else f" ({result.error})"
            self.history.append(f"user: {cmd.text} | end: aborted{suffix}")
            self.history[:] = self.history[-20:]
            return

        max_steps = int(self.cfg.get("orchestrator.mission_max_steps", 10))
        mission = OrchestratorMission(goal_text=cmd.text, started_ts_ns=now_ns())
        # decision_count bounds VLM calls; mission.current_step counts
        # individual tool calls in a batch.
        decision_count = 0
        terminal_call: Optional[str] = None
        stuck = False
        vlm_error: Optional[str] = None
        preempted = False

        # Re-prompt the VLM with each tool result; bounded by max_steps
        # decisions and preempted by a fresh voice command (next run()
        # iteration picks it up).
        while decision_count < max_steps and not self.stop_event.is_set():
            if self._preempt_event.is_set() or not self.command_q.empty():
                self.log.info("mission preempted by new voice command")
                preempted = True
                break

            ctx = self._build_context(cmd, mission=mission)
            step_label = f"decision {decision_count + 1}/{max_steps}"
            self._publish_status("thinking", f"{step_label}: {cmd.text!r}")

            # Run the VLM call on a worker so we can poll _preempt_event
            # and bail when the operator says "abort" mid-call. The stale
            # future keeps running to completion; we just discard it.
            fut = self._vlm_executor.submit(self.vlm.decide, ctx)
            while not fut.done():
                if self.stop_event.is_set() or self._preempt_event.is_set():
                    break
                time.sleep(0.05)
            if self._preempt_event.is_set() or self.stop_event.is_set():
                self.log.info("VLM call abandoned (preempt or shutdown)")
                preempted = True
                break
            # fut.result() re-raises any exception thrown inside vlm.decide.
            # Backends should self-handle quota / network / parse errors and
            # return a vlm_error: VlmDecision; an unexpected raise here
            # would otherwise propagate to run() and kill the orchestrator
            # main thread, with worker threads still alive queueing commands
            # nobody dispatches.
            try:
                decision = fut.result()
            except Exception as e:
                self.log.exception("VLM decide() raised unhandled: %s", e)
                vlm_error = f"vlm_error: {e!r}"
                break
            decision_count += 1
            self.log.info("%s decision: %s -> %d tool_calls",
                          step_label, decision.thought, len(decision.tool_calls))

            if not decision.tool_calls:
                if decision.thought.startswith("vlm_error:"):
                    # Quota / network / key error from the VLM backend.
                    # Surface as its own status so the TUI shows red, not
                    # "idle" with a stale thought string.
                    vlm_error = decision.thought
                    self.log.error("VLM call failed: %s", decision.thought)
                break

            self._publish_status("executing", decision.thought)
            offline = False
            stuck = False
            for i, tc in enumerate(decision.tool_calls):
                if self.stop_event.is_set() or self._preempt_event.is_set():
                    self._record_skipped(mission, decision.tool_calls[i:],
                                         "skipped:preempted")
                    preempted = True
                    break
                result = self._dispatch_tool_call(tc)
                mission.record(tc.name, dict(tc.args or {}), result)
                if (result.error in ("bridge_offline", "timeout")
                        or (isinstance(result.error, str)
                            and result.error.startswith("wire_error"))):
                    self._record_skipped(mission, decision.tool_calls[i + 1:],
                                         "skipped:bridge_offline")
                    offline = True
                    break
                if not result.ok and result.error is not None:
                    matches = sum(
                        1 for step in mission.step_history[-_STUCK_FAILURE_WINDOW:]
                        if step["tool"] == tc.name and step["error"] == result.error
                    )
                    # End the mission once the VLM is stuck on a loop it can't
                    # escape on its own (canonical case: PX4 'Arming denied').
                    self._record_skipped(mission, decision.tool_calls[i + 1:],
                                         "skipped:prior_failure")
                    if matches >= _STUCK_FAILURE_THRESHOLD:
                        self.log.warning(
                            "mission stuck: %s failed %d times with %r; bailing",
                            tc.name, matches, result.error)
                        stuck = True
                        break
                    break
                if tc.name in _TERMINAL_TOOLS:
                    self._record_skipped(mission, decision.tool_calls[i + 1:],
                                         "skipped:after_terminal")
                    terminal_call = tc.name
                    break

            if terminal_call is not None or offline or stuck or preempted:
                break

        # Order matters: stop_event takes precedence over the no-flags
        # fallthroughs below, otherwise a SIGTERM mid-batch reports
        # "no_tool_calls" or "max_decisions=N" depending on where the
        # outer-loop budget was at, which is misleading.
        if self.stop_event.is_set():
            end_reason = "stopped"
        elif preempted:
            end_reason = "preempted"
        elif terminal_call:
            end_reason = f"terminal:{terminal_call}"
        elif self._bridge_offline():
            end_reason = "bridge_offline"
        elif vlm_error is not None:
            end_reason = "vlm_error"
        elif decision_count >= max_steps:
            end_reason = f"max_decisions={max_steps}"
        elif stuck:
            end_reason = "stuck_on_repeated_failure"
        else:
            end_reason = "no_tool_calls"
        self.history.append(
            f"user: {cmd.text} | decisions: {decision_count} "
            f"| tools: {mission.current_step} | end: {end_reason}"
        )
        self.history[:] = self.history[-20:]
        # Bridge-offline and vlm_error already / want to surface their own
        # state — don't overwrite with "idle" at end of mission.
        if end_reason == "vlm_error":
            self._publish_status("vlm_error", vlm_error or "vlm_error")
        elif end_reason != "bridge_offline":
            self._publish_status("idle", f"mission done ({end_reason})")

    @staticmethod
    def _record_skipped(mission: OrchestratorMission,
                         remaining: list, reason: str) -> None:
        """Drop k+1..N from a partially-executed batch into step_history
        as failures so the audit trail and the VLM's `mission` view
        reflect what actually didn't run, instead of vanishing them."""
        for skipped in remaining:
            mission.record(
                skipped.name,
                dict(skipped.args or {}),
                ToolResult(request_id=skipped.request_id,
                            ok=False, error=reason),
            )

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
        """True only after a dispatch has actually timed out and no later
        reply has reset the flag."""
        with self._bridge_lock:
            return self._bridge_offline_flag

    def _ensure_req_socket(self) -> zmq.Socket:
        if self._req_socket is None:
            s = self._zmq.socket(zmq.REQ)
            s.setsockopt(zmq.LINGER, 0)
            s.setsockopt(zmq.RCVTIMEO, 3000)
            s.setsockopt(zmq.SNDTIMEO, 3000)
            s.connect(self.flight_req_endpoint)
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
        # Once the offline flag is set, fail the call immediately rather
        # than wait out another REQ timeout. The flag clears on the first
        # successful reply, so the next dispatch after recovery goes through.
        if self._bridge_offline():
            self.log.warning("tool %s rejected: bridge_offline", tc.name)
            self._publish_status("bridge_offline",
                                  f"refusing {tc.name}: prior dispatch timed out")
            return ToolResult(request_id=tc.request_id, ok=False, error="bridge_offline")

        # voice_confirmed is the orchestrator's authoritative signal —
        # overwrite unconditionally on arm so a VLM-hallucinated value
        # can't bypass the bridge's spoken-arm gate.
        args = dict(tc.args) if tc.args else {}
        if tc.name == "arm":
            args["voice_confirmed"] = self._current_arm_authorized

        try:
            req = self._ensure_req_socket()
            req.send(json.dumps({
                "request_id": tc.request_id,
                "name": tc.name,
                "args": args,
            }).encode("utf-8"))
            reply = req.recv()
            data = json.loads(reply.decode("utf-8"))
            # The bridge always replies with a JSON object, but a wire-level
            # bug (or someone connecting another producer to the REP socket)
            # could send a list/string. data.get on those raises
            # AttributeError, which the catch-block widened.
            if not isinstance(data, dict):
                raise TypeError(f"non-dict reply: {type(data).__name__}")
            result = ToolResult(
                request_id=data.get("request_id", tc.request_id),
                ok=bool(data.get("ok", False)),
                error=data.get("error"),
                data=data.get("data"),
            )
            # Any reply (ok or error) means the bridge is alive; only
            # timeouts leave _bridge_offline_flag set.
            with self._bridge_lock:
                self._bridge_offline_flag = False
            if not result.ok:
                self.log.warning("tool %s failed: %s", tc.name, result.error)
            else:
                self.log.info("tool %s ok", tc.name)
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
            # Transport-level failure (wedged REQ state, ENOTCONN, ...).
            # Flag offline so the next dispatch short-circuits at the gate.
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
            # Status PUB is non-load-bearing for control flow, but a
            # send failure here means the TUI has stopped seeing us —
            # surface rather than swallow.
            self.log.warning("status_pub send failed (%s): %r", e, msg)


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
