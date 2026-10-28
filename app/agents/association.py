"""Structured association capability of the single customer-service Agent."""
import json
from typing import Literal

from pydantic import Field, model_validator

from app.tasks.models import StrictModel


DetailField = Literal[
    "order_id", "item_id", "product_id", "sku_id", "item_name", "quantity",
    "reason", "conditions", "platform_code", "store_id", "channel", "category", "tracking_number",
    "after_sale_id", "invoice_title", "invoice_type", "invoice_id", "tax_id", "email", "address",
]


class Quote(StrictModel):
    session_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=4000)


class Detail(StrictModel):
    field: DetailField
    value: str | int
    evidence: Quote


class AssociationDecision(StrictModel):
    sequences: list[int] = Field(min_length=1, max_length=100)
    action: Literal["create", "update", "clarify", "ignore"]
    task_id: str | None = None
    task_version: int | None = Field(default=None, ge=1)
    effect: Literal["new", "supplement", "amend", "answer", "pause", "cancel", "resume", "none"] = "none"
    goal: str | None = Field(default=None, min_length=1, max_length=2000)
    details: list[Detail] = Field(default_factory=list, max_length=20)
    evidence: list[Quote] = Field(min_length=1, max_length=100)
    question_id: str | None = None
    clarification_id: str | None = None
    clarification: str | None = Field(default=None, min_length=1, max_length=1000)
    candidate_task_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def shape(self):
        if len(set(self.sequences)) != len(self.sequences) or len({d.field for d in self.details}) != len(self.details):
            raise ValueError("Duplicate source or detail")
        if self.action == "create":
            if not self.goal or self.task_id or self.task_version or self.effect != "new" or self.question_id:
                raise ValueError("Invalid new task")
        elif self.action == "update":
            if not self.task_id or self.task_version is None or self.effect not in {"supplement", "amend", "answer", "pause", "cancel", "resume"}:
                raise ValueError("Invalid existing task")
            if self.effect == "answer" and not (self.question_id or self.clarification_id):
                raise ValueError("Answers need a question reference")
        elif self.task_id or self.task_version or self.goal or self.details or self.effect != "none" or self.question_id:
            raise ValueError("Clarification/ignore cannot mutate tasks")
        if self.action == "clarify":
            if not self.clarification:
                raise ValueError("Missing clarification")
        elif self.clarification or self.candidate_task_ids:
            raise ValueError("Unexpected clarification fields")
        if self.question_id and self.effect != "answer":
            raise ValueError("A question reference requires an answer")
        if self.clarification_id and self.action not in {"create", "update"}:
            raise ValueError("A clarification answer must select or create a task")
        return self


class AssociationProposal(StrictModel):
    decisions: list[AssociationDecision] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_question(self):
        if sum(d.action == "clarify" for d in self.decisions) > 1:
            raise ValueError("Ask one distinguishing question at a time")
        return self


def complete_quoted_clarification_sources(proposal, context):
    """Complete source bookkeeping only when the model already cited every input.

    This never chooses a task, adds evidence, or accepts an expired question.
    The transaction still checks publication, ownership, versions and quotes.
    """
    result = proposal.model_copy(deep=True)
    questions = {c["clarification_id"]: c for c in context.clarifications}
    current = {m["sequence"] for m in context.current_messages}
    for decision in result.decisions:
        question = questions.get(decision.clarification_id)
        if not question or not question["valid"] or not question["published"]:
            continue
        sources = question["sources"]
        if all(any(q.session_id == s["session_id"] and q.sequence == s["sequence"]
                   and q.quote in s["content"] for q in decision.evidence) for s in sources):
            combined = set(decision.sequences) | {s["sequence"] for s in sources}
            if len(combined) <= 100 and combined & current:
                decision.sequences = sorted(combined)
    return result


def omit_ungrounded_details(proposal, context):
    """An optional bad extraction must not discard an otherwise grounded goal.

    Never repair values or invent quotes. Keep the goal and all source evidence
    for the transaction guard. Validated question option mappings remain legal.
    """
    result = proposal.model_copy(deep=True)
    candidates = {c["task_id"]: c for c in context.candidates}
    for decision in result.decisions:
        if not decision.goal:
            continue
        question = candidates.get(decision.task_id, {}).get("question") or {}
        def retain(detail):
            if not isinstance(detail.value, str) or detail.value.casefold() in detail.evidence.quote.casefold():
                return True
            return (decision.effect == "answer" and question.get("valid")
                and question.get("question_id") == decision.question_id and question.get("field") == detail.field
                and any(o.get("value") == detail.value for o in question.get("options", [])))
        decision.details = [detail for detail in decision.details if retain(detail)]
    return result


def propose(client, model, context):
    from app.config.settings import settings
    from app.prompts.association import ASSOCIATION_PROMPT
    schema = json.dumps(AssociationProposal.model_json_schema(), ensure_ascii=False)
    template = {
        "model": model, "temperature": 0, "max_tokens": 3500, "timeout": 25,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": ASSOCIATION_PROMPT + "\nJSON 结构：\n" + schema}],
        **settings.chat_completion_options,
    }
    params = context.request(template)
    from app.observability.interactions import call_model
    response = call_model(client, params)
    content = response.choices[0].message.content
    if not isinstance(content, str) or len(content) > 100000:
        raise ValueError("Invalid association output")
    proposal = complete_quoted_clarification_sources(AssociationProposal.model_validate_json(content), context)
    return omit_ungrounded_details(proposal, context)
