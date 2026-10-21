"""Measure synthetic evaluations without recording prompts or credentials."""
import time


class Measurement:
    def __init__(self, raw):
        self.calls = []
        self.original = raw.chat.completions.create
        raw.chat.completions.create = self.create

    def create(self, **kwargs):
        started = time.monotonic()
        row = dict(success=False, input_tokens=None, output_tokens=None)
        try:
            result = self.original(**kwargs)
            row['success'] = True
            usage = getattr(result, 'usage', None)
            if usage is not None:
                row['input_tokens'] = getattr(usage, 'prompt_tokens', None)
                row['output_tokens'] = getattr(usage, 'completion_tokens', None)
            return result
        finally:
            row['seconds'] = round(time.monotonic() - started, 3)
            self.calls.append(row)

    def report(self, since=0):
        calls = self.calls[since:]
        known = all(c['input_tokens'] is not None and c['output_tokens'] is not None for c in calls)
        return dict(model_calls=len(calls), model_seconds=round(sum(c['seconds'] for c in calls), 3),
                    failed_calls=sum(not c['success'] for c in calls),
                    input_tokens=sum(c['input_tokens'] for c in calls) if known else None,
                    output_tokens=sum(c['output_tokens'] for c in calls) if known else None,
                    cost=None, cost_note='No billing rate supplied; token counts are provider-reported, not an invoice.')
