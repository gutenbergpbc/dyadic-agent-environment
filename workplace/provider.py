"""Single frozen OpenRouter route; exact wire evidence and conservative reservations."""

import asyncio
import json
import os
from decimal import Decimal

import httpx

from .evidence import save

MODEL = "z-ai/glm-5.3"
ROUTE = "z-ai/fp8"
URL = "https://openrouter.ai/api/v1/chat/completions"
META = "https://openrouter.ai/api/v1/models/z-ai/glm-5.3/endpoints"
# Deliberately above verified list prices. Sent as provider routing price caps.
INPUT_CAP = Decimal(5) / 1000000
OUTPUT_CAP = Decimal(20) / 1000000


class BudgetLimit(Exception):
    pass


class Budget:
    def __init__(self, path, ceiling=100):
        if not 0 < ceiling <= 100:
            raise ValueError("ceiling must be in (0,100]")
        self.path = path
        self.ceiling = Decimal(str(ceiling))
        self.entries = []
        self.lock = asyncio.Lock()
        self.halted = False
        self.persist()

    def persist(self):
        save(
            self.path,
            {
                "ceiling_usd": str(self.ceiling),
                "accounted_usd": str(
                    sum(Decimal(e["accounted_usd"]) for e in self.entries)
                ),
                "entries": self.entries,
            },
        )

    async def reserve(self, request, identity):
        # UTF-8 bytes bound visible text token count conservatively; a large fixed
        # margin covers role/tool framing. No hidden tools, retries, or plugins.
        size = len(json.dumps(request, ensure_ascii=False).encode())
        if size > 800000:
            raise BudgetLimit("conservative request size limit")
        amount = (
            Decimal(size + 8192) * INPUT_CAP
            + Decimal(request["max_tokens"]) * OUTPUT_CAP
        )
        async with self.lock:
            if self.halted:
                raise BudgetLimit("batch halted after provider contract breach")
            if (
                sum(Decimal(e["accounted_usd"]) for e in self.entries) + amount
                > self.ceiling
            ):
                raise BudgetLimit("cannot reserve another call within total ceiling")
            entry = {
                "id": len(self.entries) + 1,
                "identity": identity,
                "reserved_usd": str(amount),
                "accounted_usd": str(amount),
                "status": "reserved",
            }
            self.entries.append(entry)
            self.persist()
            return entry

    async def settle(self, entry, response):
        # Unknown/failed/cancelled calls retain their full reservation permanently.
        usage = response.get("usage", {})
        cost = usage.get("cost")
        async with self.lock:
            if isinstance(cost, (int, float)) and cost >= 0:
                actual = Decimal(str(cost))
                entry["reported_cost_usd"] = str(actual)
                if actual > Decimal(entry["reserved_usd"]):
                    self.halted = True
                    entry["status"] = "reservation_breach"
                    entry["accounted_usd"] = str(actual)
                    self.persist()
                    raise RuntimeError(
                        "provider cost exceeded conservative reservation; stop batch"
                    )
                entry["accounted_usd"] = str(actual)
                entry["status"] = "settled"
            else:
                entry["status"] = "unknown_cost_retained"
            self.persist()


async def verify_provider(path):
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        r = await client.get(META)
        r.raise_for_status()
        metadata = r.json()
    save(path, metadata)
    endpoints = metadata["data"]["endpoints"]
    candidates = [x for x in endpoints if x.get("tag") == ROUTE]
    if len(candidates) != 1:
        raise RuntimeError("frozen route not listed exactly once")
    endpoint = candidates[0]
    prices = endpoint["pricing"]
    if (
        Decimal(str(prices["prompt"])) > INPUT_CAP
        or Decimal(str(prices["completion"])) > OUTPUT_CAP
    ):
        raise RuntimeError("provider price exceeds frozen caps")
    if endpoint.get("status", 0) != 0:
        raise RuntimeError("provider unavailable")
    return endpoint


class LiveTransport:
    def __init__(self, budget, evidence, max_tokens):
        self.budget = budget
        self.evidence = evidence
        self.max_tokens = max_tokens
        self.calls = {"agent_a": 0, "agent_b": 0}
        self.admission_check = None

    async def generate(self, actor, messages, tools):
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key or key.startswith("op://"):
            raise RuntimeError("provider credential not injected")
        self.calls[actor] += 1
        call_id = f"{actor}-{self.calls[actor]:03d}"
        request = {
            "model": MODEL,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
            "reasoning": {"effort": "high"},
            "stream": False,
            "provider": {
                "only": [ROUTE],
                "allow_fallbacks": False,
                "max_price": {"prompt": 5, "completion": 20},
            },
        }
        save(self.evidence.root / "wire" / f"{call_id}-request.json", request)
        if self.admission_check:
            self.admission_check()
        entry = await self.budget.reserve(
            request, f"{self.evidence.root.name}/{call_id}"
        )
        self.evidence.emit(
            "model_call_start", actor=actor, call_id=call_id, budget_id=entry["id"]
        )
        try:
            # httpx does not retry by default. Never include request headers in logs.
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(240, connect=30), trust_env=False
            ) as client:
                if self.admission_check:
                    self.admission_check()
                response = await client.post(
                    URL, json=request, headers={"Authorization": "Bearer " + key}
                )
            (self.evidence.root / "wire" / f"{call_id}-response.txt").write_text(
                response.text
            )
            self.evidence.emit(
                "model_call_end",
                actor=actor,
                call_id=call_id,
                http_status=response.status_code,
            )
            response.raise_for_status()
            data = response.json()
            await self.budget.settle(entry, data)
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            if data.get("provider") != "Z.AI":
                self.budget.halted = True
                raise RuntimeError("unexpected provider: " + str(data.get("provider")))
            return data["choices"][0]["message"]
        except BaseException as exc:
            self.evidence.emit(
                "model_call_failure",
                actor=actor,
                call_id=call_id,
                error=type(exc).__name__,
            )
            raise
