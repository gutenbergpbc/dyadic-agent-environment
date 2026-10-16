import asyncio
import json
from types import SimpleNamespace

import pytest
from inspect_ai.tool import ToolCall

from workplace.evidence import Evidence
from workplace.pressure_runtime import (
    PressureEpisode,
    PressureTransport,
    bounded_drain,
    run_episode,
    validate_config,
)
from workplace.provider import BudgetLimit, LiveTransport


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class Transport:
    def set_admission_check(self, check):
        self.check = check

    async def generate(self, actor, messages, tools):
        self.check(actor)
        return {"role": "assistant", "content": "done"}


def setup(tmp_path):
    clock = Clock()
    machine = SimpleNamespace()
    ev = Evidence(tmp_path / "episode")
    config = dict(
        a_deadline_seconds=30,
        horizon_seconds=900,
        turns=256,
        max_tokens=8192,
        condition="on_call",
        variant="incompatible",
    )
    ep = PressureEpisode(machine, ev, config, Transport(), clock=clock)
    ep.start_mono = 100
    return ep, clock


def response(text="done", calls=None):
    return SimpleNamespace(
        output=SimpleNamespace(message=SimpleNamespace(text=text, tool_calls=calls))
    )


def test_exact_boundaries_and_b_continues(tmp_path):
    ep, clock = setup(tmp_path)
    clock.now = 129.999
    ep.admit("agent_a")
    clock.now = 130
    with pytest.raises(BudgetLimit):
        ep.admit("agent_a")
    ep.admit("agent_b")
    clock.now = 1000
    with pytest.raises(BudgetLimit):
        ep.admit("agent_b")


def test_cancellation_resistant_cleanup_is_bounded():
    async def run():
        released = asyncio.Event()

        async def stubborn():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await released.wait()

        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        task.cancel()
        assert await bounded_drain((task,), timeout=0.02) == {task}
        released.set()
        assert await bounded_drain((task,), timeout=0.1) == set()

    asyncio.run(run())


@pytest.mark.parametrize(
    "calls",
    [
        None,
        [
            ToolCall(
                id="1", function="message", arguments={"to": "group", "text": "late"}
            ),
            ToolCall(
                id="2", function="submit", arguments={"summary": "late", "claims": {}}
            ),
        ],
    ],
)
def test_late_plain_text_and_multitool_never_transition(tmp_path, calls):
    ep, clock = setup(tmp_path)
    clock.now = 131
    assert asyncio.run(ep.handle_output("agent_a", response(calls=calls))) is None
    assert not ep.handoffs
    assert not ep.messages
    assert ep.evidence.events[-1]["kind"] == "late_model_response"


def test_handoff_does_not_wait_on_peer_or_snapshot(tmp_path):
    async def run():
        ep, _ = setup(tmp_path)
        ep.active.add("agent_b")
        ep.snapshotting = True
        result = await asyncio.wait_for(ep.handoff("agent_a", "done", {}), 0.1)
        assert result["state"] == "on_call"
        assert ep.active == {"agent_b"}
        assert ep.evidence.events[0]["quiescent"] is False

    asyncio.run(run())


def test_each_tool_rechecked_after_admitted_foreground(tmp_path):
    async def run():
        ep, clock = setup(tmp_path)

        async def shell(actor, script):
            clock.now = 135  # admitted foreground completes after A deadline
            return {"exit_code": 0, "stdout": "admitted work completed"}

        ep.machine.shell = shell
        result = await ep.tool("agent_a", "shell", {"command": "sleep 35"})
        assert result["exit_code"] == 0
        denied = await ep.tool("agent_a", "message", {"to": "group", "text": "late"})
        assert denied["executed"] is False
        assert not ep.messages
        allowed = await ep.tool(
            "agent_b", "message", {"to": "group", "text": "still here"}
        )
        assert allowed["sender"] == "agent_b"

    asyncio.run(run())


def test_start_barrier_waits_for_both_actors(tmp_path):
    async def run():
        ep, _ = setup(tmp_path)
        ep.start_mono = None
        a = asyncio.create_task(ep.transport.generate("agent_a", [], []))
        await asyncio.sleep(0)
        assert not ep.started.is_set()
        assert not a.done()
        b = asyncio.create_task(ep.transport.generate("agent_b", [], []))
        await asyncio.gather(a, b)
        assert ep.started.is_set()
        assert ep.start_mono == 100

    asyncio.run(run())


def test_queued_transport_rechecks_actor_after_wait(tmp_path):
    async def run():
        ep, clock = setup(tmp_path)

        class Queued(Transport):
            async def generate(self, actor, messages, tools):
                await asyncio.sleep(0)
                clock.now = 131
                self.check(actor)

        queued = Queued()
        queued.set_admission_check(ep.admit)
        with pytest.raises(BudgetLimit):
            await queued.generate("agent_a", [], [])
        await queued.generate("agent_b", [], [])

    asyncio.run(run())


def test_historical_transport_rejected(tmp_path):
    ep, _ = setup(tmp_path)
    with pytest.raises(TypeError, match="actor-aware"):
        PressureEpisode(
            ep.machine, ep.evidence, ep.config, LiveTransport(None, ep.evidence, 8192)
        )


def test_queued_wake_does_not_consume_after_deadline(tmp_path):
    async def run():
        ep, clock = setup(tmp_path)
        ep.life.park("agent_a")
        waiting = asyncio.create_task(ep.wake("agent_a"))
        await asyncio.sleep(0)
        message = {
            "message_id": 1,
            "sender": "agent_b",
            "to": "agent_a",
            "text": "late",
        }
        ep.life.message(message)
        clock.now = 130
        with pytest.raises(BudgetLimit):
            await waiting
        assert ep.life.pending["agent_a"][("message", 1)] == message
        assert not ep.life.delivered["agent_a"]
        assert ep.life.states["agent_a"] == "on_call"

    asyncio.run(run())


def test_deadline_crossing_during_staging_preserves_nondelivery(tmp_path):
    async def run():
        ep, clock = setup(tmp_path)
        ep.life.park("agent_a")
        message = {
            "message_id": 1,
            "sender": "agent_b",
            "to": "agent_a",
            "text": "queued",
        }
        ep.life.message(message)
        notices = await ep.wake("agent_a")
        assert not ep.life.delivered["agent_a"]
        clock.now = 130
        with pytest.raises(BudgetLimit):
            ep.complete_wake("agent_a", notices)
        assert ep.life.pending["agent_a"][("message", 1)] == message
        assert ep.life.states["agent_a"] == "on_call"
        assert not ep.life.delivered["agent_a"]

    asyncio.run(run())


def test_frozen_live_config_and_no_numerical_b_deadline(tmp_path):
    ep, _ = setup(tmp_path)
    assert "30 seconds" in ep.rules("agent_a")
    assert "900" not in ep.rules("agent_b")
    assert "45 seconds" not in ep.rules("agent_a")
    with pytest.raises(ValueError):
        validate_config({"a_deadline_seconds": 2, "horizon_seconds": 5})
    assert validate_config(
        {"a_deadline_seconds": 2, "horizon_seconds": 5, "scripted_canary": True}
    )


def test_http_dispatch_hook_rechecks_after_reservation_and_connection_wait(
    tmp_path, monkeypatch
):
    async def run():
        ep, clock = setup(tmp_path)

        class Budget:
            async def reserve(self, request, identity):
                return {"id": 1}

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                clock.now = 130
                await kwargs["extensions"]["trace"](
                    "http11.send_request_headers.started", {}
                )
                pytest.fail("late HTTP request admitted")

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")
        monkeypatch.setattr("workplace.pressure_runtime.httpx.AsyncClient", Client)
        transport = PressureTransport(Budget(), ep.evidence, 8192)
        transport.set_admission_check(ep.admit)
        with pytest.raises(BudgetLimit):
            await transport.generate("agent_a", [], [])
        assert not any(e["kind"] == "model_call_start" for e in ep.evidence.events)
        assert ep.evidence.events[-1]["unknown_charge_retained"] is True

    asyncio.run(run())


def test_real_petri_sessions_keep_late_output_as_evidence(tmp_path, monkeypatch):
    """Real Inspect/Petri channels, fake machine; does not validate Docker."""
    monkeypatch.setattr(
        "inspect_ai._util.appdirs.user_data_path",
        lambda *a, **k: tmp_path / "inspect-data",
    )
    monkeypatch.setattr(
        "inspect_ai._util.appdirs.user_cache_path",
        lambda *a, **k: tmp_path / "inspect-cache",
    )

    class FakeMachine:
        def __init__(self, image, evidence):
            self.evidence = evidence
            self.commands = {}
            self.frozen = False

        async def start(self):
            return {"fake": True}

        async def probe(self, expected):
            return {"accepted": {}, "inventory": {}}

        async def root(self, *args, **kwargs):
            return json.dumps(
                {
                    "accepted": {},
                    "health": 200,
                    "v1": 202,
                    "v2": 404,
                    "started": 0,
                    "ended": 0,
                }
            )

        async def processes(self):
            return {"processes": []}

        async def freeze_capture(self):
            self.frozen = True
            self.evidence.emit("freeze_confirmed")

        async def close(self):
            return {"exit_code": 0}

    class LateTransport(Transport):
        async def generate(self, actor, messages, tools):
            self.check(actor)
            if actor == "agent_a":
                await asyncio.sleep(0.6)
            return {"role": "assistant", "content": "finished"}

    monkeypatch.setattr("workplace.pressure_runtime.PressureMachine", FakeMachine)
    dest = tmp_path / "petri"
    summary = asyncio.run(
        run_episode(
            dest,
            {
                "a_deadline_seconds": 0.2,
                "horizon_seconds": 0.4,
                "settlement_seconds": 2,
                "scripted_canary": True,
                "image": "fake",
            },
            LateTransport(),
        )
    )
    events = [json.loads(x) for x in (dest / "events.jsonl").read_text().splitlines()]
    assert summary["horizon_reached"] and summary["freeze_confirmed"]
    assert not summary["failed"], events
    assert any(
        e["kind"] == "late_model_response" and e["actor"] == "agent_a" for e in events
    )
    assert summary["handoffs"] == {"agent_b": 1}
