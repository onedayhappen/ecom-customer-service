"""Opt-in model judging; errors remain unscored and never become a passing score."""
import json
import time
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from app.evaluation.rag.dataset import Record
from app.prompts.evaluation import (
    FAITHFULNESS_PROMPT, ANSWER_QUALITY_PROMPT, RAG_ANSWER_PROMPT,
)


class Claim(Record):
    claim: str = Field(min_length=1)
    verdict: Literal["supported", "unsupported", "contradicted"]
    evidence_indices: list[StrictInt]
    quotes: list[str]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def pairs(self):
        if len(self.evidence_indices) != len(self.quotes):
            raise ValueError("Each evidence reference must have a quote")
        if self.verdict == "supported" and not self.quotes:
            raise ValueError("Supported claims require evidence")
        return self


class Faithfulness(Record):
    claims: list[Claim] = Field(max_length=100)


class Rating(Record):
    score: StrictInt = Field(ge=1, le=5)
    reason: str = Field(min_length=1)


def unscored(status, reason):
    return dict(status=status, score=None, reason=reason)


class ModelGrader:
    def __init__(self, client, model, *, options=None, max_input_chars=60000):
        self.client, self.model = client, model
        self.options = dict(options or {})
        if set(self.options) & {"messages", "model", "temperature", "stream"}:
            raise ValueError("Model options cannot override grading instructions")
        self.max_input_chars = max_input_chars
        self.calls = []

    def call(self, prompt, data, purpose):
        payload = json.dumps(data, ensure_ascii=False)
        if len(prompt) + len(payload) > self.max_input_chars:
            raise ValueError("Evaluation input exceeds budget; evidence was not truncated")
        row = dict(purpose=purpose, model=self.model, success=False,
                   input_tokens=None, output_tokens=None)
        start = time.monotonic()
        try:
            response = self.client.chat.completions.create(
                model=self.model, temperature=0.0,
                messages=[dict(role="system", content=prompt), dict(role="user", content=payload)],
                **self.options,
            )
            usage = getattr(response, "usage", None)
            if usage:
                row.update(input_tokens=getattr(usage, "prompt_tokens", None),
                           output_tokens=getattr(usage, "completion_tokens", None))
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty model response")
            row["success"] = True
            return content
        finally:
            row["seconds"] = round(time.monotonic() - start, 4)
            self.calls.append(row)

    def judge_faithfulness(self, query, answer, evidence):
        if not answer or not answer.strip():
            return unscored("missing_answer", "No answer was supplied")
        try:
            verdict = Faithfulness.model_validate_json(self.call(
                FAITHFULNESS_PROMPT, dict(query=query, answer=answer, evidence=evidence), "faithfulness"))
            for claim in verdict.claims:
                for index, quote in zip(claim.evidence_indices, claim.quotes):
                    if not 0 <= index < len(evidence) or not quote.strip() or quote not in evidence[index]:
                        raise ValueError("Judge cited missing evidence or a fabricated quote")
            if not verdict.claims:
                return unscored("not_applicable", "Judge found no verifiable factual claims")
            return dict(status="scored", score=sum(c.verdict == "supported" for c in verdict.claims) / len(verdict.claims),
                        claims=[c.model_dump() for c in verdict.claims])
        except Exception as error:
            # Do not expose SDK exception bodies (which may include credentials).
            return unscored("error", type(error).__name__)

    def rating(self, prompt, data, purpose):
        try:
            result = Rating.model_validate_json(self.call(prompt, data, purpose))
            return dict(status="scored", score=result.score / 5, raw_score=result.score, reason=result.reason)
        except Exception as error:
            return unscored("error", type(error).__name__)

    def judge_answer_quality(self, query, answer, reference, evidence):
        if not answer or not answer.strip():
            return unscored("missing_answer", "No answer was supplied")
        return self.rating(ANSWER_QUALITY_PROMPT,
                           dict(query=query, answer=answer, reference_answer=reference, evidence=evidence), "answer_quality")

    def generate_answer(self, query, evidence):
        return self.call(RAG_ANSWER_PROMPT, dict(query=query, evidence=evidence), "answer_generation")
