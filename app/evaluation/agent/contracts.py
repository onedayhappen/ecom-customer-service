# 文件说明：定义静态与动态评测题包、预算、证据、评分及采集记录的严格契约。
"""Version 4 dynamic scenarios; version 3 static scenarios remain supported."""
from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, model_validator

DIMENSIONS = ("task_completion", "context_consistency", "reply_accuracy")
COLLECTIONS = ("tasks", "tool_calls", "replies", "writes", "operations", "events", "models", "objects")
DYNAMIC_COLLECTIONS = (*COLLECTIONS, "user_actions", "sessions", "memory")
EvidenceSource = Literal["tasks", "tool_calls", "replies", "writes", "operations", "events", "models", "objects", "user_actions", "sessions", "memory"]


def collections_for(capture):
    return DYNAMIC_COLLECTIONS if capture.schema_version == 4 else COLLECTIONS


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Predicate(Record):
    path: list[str | StrictInt] = Field(min_length=1)
    op: Literal["equals", "contains", "one_of", "decimal_equal", "instant_equal"] = "equals"
    value: JsonValue

    @model_validator(mode="after")
    def valid(self):
        if any(type(p) is int and p < 0 for p in self.path):
            raise ValueError("Negative paths are not supported")
        if self.op == "contains" and (not isinstance(self.value, (str, dict)) or not self.value):
            raise ValueError("contains requires nonempty text or object")
        if self.op == "one_of" and (not isinstance(self.value, list) or not self.value):
            raise ValueError("one_of requires alternatives")
        if self.op in {"decimal_equal", "instant_equal"} and not isinstance(self.value, str):
            raise ValueError("Decimal amounts and instants are strings")
        return self


class Check(Record):
    id: str = Field(min_length=1)
    source: EvidenceSource
    where: list[Predicate] = Field(default_factory=list)
    assertions: list[Predicate] = Field(default_factory=list)
    quantifier: Literal["exists", "all", "none", "count"] = "exists"
    count: StrictInt | None = Field(default=None, ge=0)
    after_step: str | None = None
    before_step: str | None = None

    @model_validator(mode="after")
    def meaningful(self):
        if (self.quantifier == "count") != (self.count is not None):
            raise ValueError("Only count checks need a count")
        if self.quantifier in {"none", "count"} and self.assertions:
            raise ValueError("none/count use where predicates")
        if self.quantifier == "none" and not self.where:
            raise ValueError("Forbidden-event checks require a selector")
        return self


class Rubric(Record):
    id: str = Field(min_length=1, max_length=100)
    criterion: str = Field(min_length=1, max_length=2000)
    pass_when: str = Field(min_length=1, max_length=2000)
    partial_when: str = Field(min_length=1, max_length=2000)
    fail_when: str = Field(min_length=1, max_length=2000)
    evidence_sources: list[EvidenceSource] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.evidence_sources)) != len(self.evidence_sources):
            raise ValueError("Duplicate evidence sources")
        return self


class Dimension(Record):
    applicable: bool = True
    reason: str | None = None
    checks: list[Check] = Field(default_factory=list)
    rubrics: list[Rubric] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def configured(self):
        if not self.applicable and (not self.reason or self.checks or self.rubrics):
            raise ValueError("Non-applicability requires a reason and no checks")
        if self.applicable and not (self.checks or self.rubrics):
            raise ValueError("An applicable dimension needs rubrics or factual checks")
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError("Duplicate check IDs")
        if len({r.id for r in self.rubrics}) != len(self.rubrics):
            raise ValueError("Duplicate rubric IDs")
        return self


class Expectations(Record):
    task_completion: Dimension
    context_consistency: Dimension
    reply_accuracy: Dimension


class ReadRule(Record):
    tool: str
    user: str
    arguments: dict[str, JsonValue]
    data: dict[str, JsonValue]
    state_key: str | None = None
    limitations: list[str] = Field(default_factory=list)


class WriteRule(Record):
    tool: str
    user: str
    arguments: dict[str, JsonValue]
    state_key: str
    changes: dict[str, JsonValue]
    review: dict[str, JsonValue]
    result_status: str


class Seed(Record):
    alias: str
    session: str
    goal: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    question: str | None = None


class Trigger(Record):
    kind: Literal["initial", "reply", "model_call", "tool_start", "committed", "confirmation"]
    where: list[Predicate] = Field(default_factory=list)
    occurrence: StrictInt = Field(default=1, ge=1)


class Step(Record):
    id: str
    session: str
    trigger: Trigger
    action: Literal["message", "confirm", "cancel_operation"] = "message"
    text: str | None = Field(default=None, max_length=4000)
    card: list[Predicate] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "message":
            if not self.text or not self.text.strip() or self.card:
                raise ValueError("Message needs text, not a confirmation selector")
        elif self.text or not self.card or self.trigger.kind != "confirmation":
            raise ValueError("Confirmation needs an explicit card selector and confirmation trigger")
        elif not all(any(p.path == ["review", field] and p.op in {"equals", "one_of"} for p in self.card)
                     for field in ("order_id", "action")):
            raise ValueError("Confirmation must explicitly select order and action")
        return self


class Budget(Record):
    seconds: float = Field(default=90.0, ge=0.1, le=600)
    model_calls: StrictInt = Field(default=30, ge=1, le=200)
    tool_calls: StrictInt = Field(default=50, ge=1, le=500)
    rounds: StrictInt = Field(default=12, ge=1, le=100)
    simulator_calls: StrictInt = Field(default=60, ge=1, le=600)
    memory_calls: StrictInt = Field(default=30, ge=1, le=200)


class UserFact(Record):
    value: str = Field(min_length=1, max_length=2000)
    disclosure: Literal["anytime", "when_asked", "never"] = "when_asked"


class ConfirmationRule(Record):
    review: dict[str, JsonValue] = Field(min_length=1)

    @model_validator(mode="after")
    def identifies_action(self):
        if not all(self.review.get(key) for key in ("order_id", "action")):
            raise ValueError("Confirmation rule requires order_id and action")
        return self


class SessionPlan(Record):
    id: str = Field(min_length=1)
    session: str = Field(min_length=1)
    goal: str = Field(min_length=1, max_length=4000)
    facts: dict[str, UserFact] = Field(default_factory=dict)
    behavior: str = Field(min_length=1, max_length=4000)
    confirmations: list[ConfirmationRule] = Field(default_factory=list)
    max_turns: StrictInt = Field(default=8, ge=1, le=30)


class Simulation(Record):
    phases: list[SessionPlan] = Field(min_length=1, max_length=12)
    memory: Literal["off", "enabled", "paired"] = "off"
    warmup_sessions: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def phases_valid(self):
        if len({p.id for p in self.phases}) != len(self.phases) or len({p.session for p in self.phases}) != len(self.phases):
            raise ValueError("Every phase needs a distinct ID and session")
        if self.memory == "paired":
            if not 0 < self.warmup_sessions < len(self.phases):
                raise ValueError("Paired experiments require warmup and subsequent sessions")
        elif self.warmup_sessions:
            raise ValueError("warmup_sessions is only used by paired experiments")
        return self


class Inputs(Record):
    sessions: dict[str, str] = Field(min_length=1)
    seeds: list[Seed] = Field(default_factory=list)
    objects: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    reads: list[ReadRule] = Field(default_factory=list)
    writes: list[WriteRule] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    simulation: Simulation | None = None
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def valid_inputs(self):
        if self.simulation and self.steps:
            raise ValueError("Choose dynamic simulation or static steps")
        if not self.simulation and (not self.steps or self.steps[0].trigger.kind != "initial" or self.steps[0].action != "message"):
            raise ValueError("First step must send an initial user message")
        if len(self.steps) > self.budget.rounds:
            raise ValueError("More user steps than round budget")
        if len({s.id for s in self.steps}) != len(self.steps) or len({s.alias for s in self.seeds}) != len(self.seeds):
            raise ValueError("Duplicate step or seed IDs")
        if any(s.session not in self.sessions for s in [*self.steps, *self.seeds]):
            raise ValueError("Unknown session")
        if self.simulation:
            if any(p.session not in self.sessions for p in self.simulation.phases):
                raise ValueError("Unknown phase session")
            if self.simulation.memory == "paired" and len({self.sessions[p.session] for p in self.simulation.phases}) != 1:
                raise ValueError("Paired memory experiments use one stable user")
        for rule in [*self.reads, *self.writes]:
            if rule.user not in self.sessions.values():
                raise ValueError("Rule user not in scenario")
            if rule.state_key:
                obj = self.objects.get(rule.state_key)
                if obj is None or obj.get("user_id") != rule.user:
                    raise ValueError("Stateful rule must bind to an owned object")
        for rule in self.writes:
            obj = self.objects[rule.state_key]
            if not obj.get("order_id") or not obj.get("platform_code"):
                raise ValueError("Writes need an order and platform")
            if set(rule.changes) & {"user_id", "order_id", "platform_code"}:
                raise ValueError("Write rules cannot change object identity")
        return self


class MemoryTarget(Record):
    id: str
    phase_id: str
    disclosed_phase: str
    fact_id: str
    predicate: str
    value: JsonValue
    qualifiers: dict[str, JsonValue] = Field(default_factory=dict)
    expected_state: Literal["active", "absent"] = "active"


class Scenario(Record):
    id: str = Field(min_length=1)
    description: str
    kind: Literal["complete", "stage"] = "complete"
    inputs: Inputs
    expected: Expectations
    memory_targets: list[MemoryTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        steps = {s.id for s in self.inputs.steps}
        phases = {p.id: p for p in self.inputs.simulation.phases} if self.inputs.simulation else {}
        if len({t.id for t in self.memory_targets}) != len(self.memory_targets):
            raise ValueError("Duplicate memory targets")
        for target in self.memory_targets:
            if target.phase_id not in phases or target.disclosed_phase not in phases or target.fact_id not in phases[target.disclosed_phase].facts:
                raise ValueError("Unknown memory target phase/fact")
        if not any(getattr(self.expected, d).applicable for d in DIMENSIONS):
            raise ValueError("Scenario needs an applicable dimension")
        for name in DIMENSIONS:
            dim = getattr(self.expected, name)
            for check in dim.checks:
                if any(s is not None and s not in steps for s in (check.after_step, check.before_step)):
                    raise ValueError("Unknown step boundary")
                if (check.after_step or check.before_step) and check.source in {"tasks", "objects", "operations"}:
                    raise ValueError("Use event history for temporal checks, not final snapshots")
        return self


class Suite(Record):
    schema_version: Literal[3, 4] = 3
    name: str
    description: str
    scenarios: list[Scenario] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self):
        if len({s.id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("Duplicate scenarios")
        if any(s.inputs.simulation for s in self.scenarios) and self.schema_version != 4:
            raise ValueError("Dynamic scenarios require schema_version 4")
        return self


class Capture(Record):
    schema_version: Literal[2, 4] = 2
    run_id: str
    scenario_id: str
    input_digest: str
    repetition: StrictInt = Field(ge=1)
    source: Literal["live_model", "scripted_regression"]
    started: bool = False
    termination: Literal["not_started", "settled", "timeout", "budget", "driver_mismatch", "error"] = "not_started"
    complete: dict[str, bool] = Field(default_factory=dict)
    observations: dict[str, list[dict[str, JsonValue]]] = Field(default_factory=dict)
    boundaries: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    branch: Literal["single", "memory_off", "memory_on"] = "single"
    simulator_validity: Literal["not_applicable", "valid", "invalid", "error"] = "not_applicable"


class Captures(Record):
    schema_version: Literal[2, 4] = 2
    captures: list[Capture]
