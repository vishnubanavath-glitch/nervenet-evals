"""Drive the real in-app chat (ConversationManager) for a test user and record everything.

One conversation = one new chat Session in the dataset's test tenant, titled
"[eval] ..." so `scripts/cleanup.py` can find and delete it afterwards.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from evals.config import Dataset
from evals.tracing import Recorder

EVAL_SESSION_PREFIX = "[eval] "


@dataclass
class TurnResult:
    prompt: str
    answer: str = ""
    thinking: str = ""
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    ui_tables: list[dict[str, Any]] = field(default_factory=list)   # rows shown to the user (LOCAL ONLY)
    warnings: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    performance: dict[str, Any] | None = None
    duration_ms: int = 0
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationResult:
    dataset: str
    user_label: str
    user_email: str
    session_id: int | None
    turns: list[TurnResult]


def _resolve(ds: Dataset, user_label: str):
    from accounts.models import User
    from tenancy.models import Tenant

    tu = ds.user(user_label)
    tenant = Tenant.objects.get(slug=ds.tenant_slug)
    user = User.objects.get(email=tu.email)
    return tu, tenant, user


def _consume(stream, turn: TurnResult) -> None:
    for raw in stream:
        for line in str(raw).splitlines():
            if not line.startswith("data: "):
                continue
            body = line[len("data: "):]
            if body == "[DONE]":
                continue
            try:
                evt = json.loads(body)
            except json.JSONDecodeError:
                continue
            if "text" in evt:
                turn.answer += evt["text"]
            if "thinking" in evt:
                turn.thinking += evt["thinking"]
            for key in ("tool_call", "tool_status", "tool_call_completed"):
                if key in evt:
                    turn.tool_events.append({key: evt[key]})
            if "tool_data" in evt:
                turn.ui_tables.append(evt["tool_data"])
            if "warning" in evt:
                turn.warnings.append(evt["warning"])
            if "error" in evt:
                turn.error = evt
            if "performance_metrics" in evt:
                turn.performance = evt["performance_metrics"]


def run_conversation(ds: Dataset, user_label: str, prompts: list[str], title: str = "",
                     between_turns: bool = False) -> ConversationResult:
    """Send the prompts one after another in a single chat session (multi-turn when >1).
    between_turns=True pauses between turns (EVAL_TURN_WAIT_SECONDS) like a person reading."""
    from conversation.manager import ConversationManager
    from conversation.models import Session

    tu, tenant, user = _resolve(ds, user_label)
    session = Session.objects.create(
        tenant=tenant, user=user, title=(EVAL_SESSION_PREFIX + (title or prompts[0]))[:255]
    )
    manager = ConversationManager(session)
    turns: list[TurnResult] = []
    for n, prompt in enumerate(prompts):
        if n and between_turns:
            time.sleep(float(os.getenv("EVAL_TURN_WAIT_SECONDS", "5")))
        turn = TurnResult(prompt=prompt)
        started = time.time()
        with Recorder() as trace:
            _consume(manager.process_message_stream(user_prompt=prompt, user=user), turn)
        turn.duration_ms = int((time.time() - started) * 1000)
        turn.trace = trace.to_dict()
        turns.append(turn)
    return ConversationResult(dataset=ds.name, user_label=tu.label, user_email=tu.email,
                              session_id=session.id, turns=turns)
