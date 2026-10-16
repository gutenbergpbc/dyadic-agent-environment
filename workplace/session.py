"""Two Inspect samples, each owning its Petri channel and programmatic controller."""

import asyncio
import json
import time

import jsonschema
from inspect_ai import Task, eval_async
from inspect_ai.agent import agent
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatCompletionChoice, ChatMessageAssistant, ModelOutput
from inspect_ai.tool import ToolCall
from inspect_petri._auditor.auditor import audit_solver
from inspect_petri.target import (
    TOOL_RESULT,
    ExitSignal,
    Response,
    ResponseOutput,
    controller,
)

from .contracts import ASSIGNMENTS, RULES, SEEDS, TOOLS
from .evidence import save
from .provider import BudgetLimit


class StopResponse(Response):
    reason: str
    detail: str


class Episode:
    def __init__(self, machine, evidence, config, transport):
        self.machine = machine
        self.evidence = evidence
        self.config = config
        self.transport = transport
        self.messages = []
        self.expected = dict(SEEDS)
        self.finished = set()
        self.condition = asyncio.Condition()
        self.active = set()
        self.snapshotting = False
        self.observe_lock = asyncio.Lock()

    async def observe(self, label):
        async with self.observe_lock:
            obs = await self.machine.probe(self.expected)
            for jid, values in obs.get("inventory", {}).items():
                self.expected.setdefault(jid, values)
            for jid, values in obs["accepted"].items():
                self.expected.setdefault(jid, values)
            self.evidence.emit(
                "observation",
                label=label,
                observation=obs,
                active_commands=sorted(self.active),
            )
            return obs

    async def handoff(self, actor, summary, claims):
        # Block new foreground commands, wait for in-flight commands, freeze files,
        # then probe while foreground agents are quiescent. Background processes
        # remain visible and force reporting comparisons to unknown.
        async with self.condition:
            await self.condition.wait_for(lambda: not self.snapshotting)
            self.snapshotting = True
            await self.condition.wait_for(lambda: not self.active)
        try:
            await self.machine.snapshot(self.evidence.root / f"{actor}-submission.tar")
            obs = await self.observe("submission:" + actor)
            self.evidence.emit(
                "submission",
                actor=actor,
                summary=summary,
                claims=claims,
                observation=obs,
                quiescent=True,
            )
            self.finished.add(actor)
            return {"submitted": True}
        finally:
            async with self.condition:
                self.snapshotting = False
                self.condition.notify_all()

    async def tool(self, actor, name, args):
        if actor in self.finished:
            return {"error": "agent already submitted"}
        specs = {x["function"]["name"]: x["function"] for x in TOOLS}
        if name not in specs:
            return {"error": "unknown tool"}
        try:
            jsonschema.validate(args, specs[name]["parameters"])
        except jsonschema.ValidationError as exc:
            self.evidence.emit(
                "invalid_tool", actor=actor, name=name, args=args, error=exc.message
            )
            return {"error": exc.message}
        if name == "shell":
            async with self.condition:
                await self.condition.wait_for(lambda: not self.snapshotting)
                self.active.add(actor)
            start = self.evidence.emit(
                "command_start",
                actor=actor,
                command=args["command"],
                overlapping=sorted(self.active - {actor}),
            )
            try:
                result = await self.machine.shell(actor, args["command"])
                self.evidence.emit(
                    "command_end", actor=actor, start_id=start["id"], result=result
                )
                return {
                    k: v[:24000] if isinstance(v, str) else v for k, v in result.items()
                }
            finally:
                async with self.condition:
                    self.active.discard(actor)
                    self.condition.notify_all()
        if name == "message":
            message = {
                "message_id": len(self.messages) + 1,
                "sender": actor,
                "to": args["to"],
                "text": args["text"],
                "time": time.time(),
            }
            self.messages.append(message)
            self.evidence.emit("message", **message)
            return message
        if name == "read_messages":
            return {
                "messages": [
                    m
                    for m in self.messages
                    if m["message_id"] > args.get("after", 0)
                    and (m["to"] in ("group", actor) or m["sender"] == actor)
                ]
            }
        if name == "escalate":
            self.evidence.emit("escalation", actor=actor, reason=args["reason"])
            return {"recorded": True, "owner_reply": None}
        if name == "submit":
            return await self.handoff(actor, args["summary"], args["claims"])

    def target(self):
        episode = self

        @agent
        def workplace_target():
            async def execute(state, context):
                actor = controller().state.metadata["actor"]
                wire = []
                try:
                    await context.wait_for_resume()
                    state.messages[:] = [
                        await context.system_message(),
                        await context.user_message(),
                    ]
                    wire.extend(
                        {"role": m.role, "content": m.text} for m in state.messages
                    )
                    while True:
                        try:
                            raw = await episode.transport.generate(
                                actor, wire, getattr(episode, "tools", TOOLS)
                            )
                        except BudgetLimit as exc:
                            await context.send_response(
                                StopResponse(reason="budget", detail=str(exc))
                            )
                            return state
                        except Exception as exc:
                            await context.send_response(
                                StopResponse(reason="error", detail=repr(exc))
                            )
                            return state
                        # Preserve raw reasoning fields and tool arguments unchanged on
                        # the provider wire; typed Petri views are supplemental.
                        wire.append(raw)
                        calls = []
                        for call in raw.get("tool_calls") or []:
                            try:
                                arguments = json.loads(call["function"]["arguments"])
                            except (ValueError, TypeError):
                                arguments = {
                                    "_malformed": call["function"]["arguments"]
                                }
                            calls.append(
                                ToolCall(
                                    id=call["id"],
                                    function=call["function"]["name"],
                                    arguments=arguments,
                                )
                            )
                        msg = ChatMessageAssistant(
                            content=raw.get("content") or "", tool_calls=calls or None
                        )
                        state.output = ModelOutput(
                            model="z-ai/glm-5.3",
                            choices=[
                                ChatCompletionChoice(
                                    message=msg,
                                    stop_reason="tool_calls" if calls else "stop",
                                )
                            ],
                        )
                        state.messages.append(msg)
                        save(episode.evidence.root / f"{actor}-conversation.json", wire)
                        context.expect({TOOL_RESULT: {c.id for c in calls}})
                        await context.send_output(state.output)
                        if calls:
                            results = await context.tool_results(calls)
                            state.messages.extend(results)
                            wire.extend(
                                {
                                    "role": "tool",
                                    "tool_call_id": m.tool_call_id,
                                    "content": m.text,
                                }
                                for m in results
                            )
                            save(
                                episode.evidence.root / f"{actor}-conversation.json",
                                wire,
                            )
                        user = await context.user_message(required=not bool(calls))
                        if user is not None:
                            state.messages.append(user)
                            wire.append({"role": "user", "content": user.text})
                            save(
                                episode.evidence.root / f"{actor}-conversation.json",
                                wire,
                            )
                except ExitSignal:
                    return state

            return execute

        return workplace_target()

    def auditor(self):
        episode = self

        @agent
        def programmatic_controller():
            async def execute(state):
                ctrl = controller()
                actor = ctrl.state.metadata["actor"]
                peer = "agent_b" if actor == "agent_a" else "agent_a"
                await ctrl.stage_system(
                    RULES.format(
                        agent=actor,
                        peer=peer,
                        turns=episode.config["turns"],
                        deadline=episode.config["deadline"],
                    )
                )
                await ctrl.stage_user(ASSIGNMENTS[episode.config["variant"]][actor])
                try:
                    for turn in range(episode.config["turns"]):
                        response = await ctrl.resume()
                        if isinstance(response, StopResponse):
                            if response.reason == "budget":
                                episode.evidence.emit(
                                    "limit", actor=actor, reason=response.detail
                                )
                            else:
                                episode.evidence.emit(
                                    "agent_error", actor=actor, error=response.detail
                                )
                            break
                        if not isinstance(response, ResponseOutput):
                            raise TypeError("unexpected Petri response")
                        state.output = response.output
                        calls = response.output.message.tool_calls or []
                        if not calls:
                            await episode.handoff(
                                actor, response.output.message.text, {}
                            )
                            break
                        for call in calls:
                            if actor in episode.finished:
                                break
                            result = await episode.tool(
                                actor, call.function, call.arguments
                            )
                            episode.evidence.emit(
                                "tool_result",
                                actor=actor,
                                tool_call_id=call.id,
                                name=call.function,
                                result=result,
                            )
                            if actor not in episode.finished:
                                await ctrl.stage_tool_result(
                                    call.id, json.dumps(result)
                                )
                        if actor in episode.finished:
                            break
                    else:
                        episode.evidence.emit(
                            "limit", actor=actor, reason="model_turn_limit"
                        )
                except BudgetLimit as exc:
                    episode.evidence.emit("limit", actor=actor, reason=str(exc))
                except Exception as exc:
                    episode.evidence.emit("agent_error", actor=actor, error=repr(exc))
                finally:
                    await ctrl.end_conversation()
                return state

            return execute

        return programmatic_controller()

    async def run(self):
        infos = [
            dict(
                name=t["function"]["name"],
                description=t["function"]["description"],
                parameters=t["function"]["parameters"],
            )
            for t in getattr(self, "tools", TOOLS)
        ]
        task = Task(
            name="multiagent_workplace",
            dataset=[
                Sample(
                    id=a,
                    input=ASSIGNMENTS[self.config["variant"]][a],
                    metadata={"actor": a, "tools": infos},
                )
                for a in ("agent_a", "agent_b")
            ],
            solver=audit_solver(auditor=self.auditor(), target=self.target()),
        )
        # Mock default means no implicit auditor/judge API is configured or invoked.
        logs = await eval_async(
            task,
            model="mockllm/model",
            max_samples=2,
            log_dir=str(self.evidence.root / "inspect"),
            score=False,
            retry_on_error=0,
        )
        save(
            self.evidence.root / "inspect-status.json",
            [
                {
                    "status": l.status,
                    "error": l.error.model_dump(mode="json") if l.error else None,
                }
                for l in logs
            ],
        )
        if any(l.status != "success" for l in logs):
            self.evidence.emit(
                "execution_failure", stage="inspect", statuses=[l.status for l in logs]
            )
