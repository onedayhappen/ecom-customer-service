# 文件说明：计算政策检索的召回、排序指标及题集汇总结果。
"""Policy retrieval metrics and aggregation.

Relevance unit: (source_id, version, section). Duplicate hits consume rank slots
but receive no further credit. Unjudged hits count as irrelevant.
"""
from math import log2


def retrieval_metrics(labels, ranked, k):
    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive integer")
    if any(type(grade) is not int or not 0 <= grade <= 3 for grade in labels.values()):
        raise ValueError("Relevance grades must be integers from 0 to 3")
    positives = {key for key, grade in labels.items() if grade > 0}
    if not positives:
        return {name: None for name in ("recall", "precision", "mrr", "ndcg")}
    seen, grades = set(), []
    for key in ranked[:k]:
        grades.append(labels.get(key, 0) if key not in seen else 0)
        seen.add(key)
    hits = sum(g > 0 for g in grades)
    dcg = sum((2 ** g - 1) / log2(rank + 2) for rank, g in enumerate(grades))
    ideal = sorted(labels.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / log2(rank + 2) for rank, g in enumerate(ideal))
    return dict(recall=hits / len(positives), precision=hits / k,
                mrr=next((1 / rank for rank, g in enumerate(grades, 1) if g > 0), 0.0),
                ndcg=dcg / idcg)


def aggregate(values):
    values = [value for value in values if value is not None]
    return {"mean": sum(values) / len(values) if values else None, "count": len(values)}
