# 文件说明：测试结构化与词法混合召回、缓存向量及来源约束的派生视图。
"""Hybrid recall, cached embeddings and evidence-backed derived views."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace

import pytest

from app.memory.compaction import build_views, resolve_view
from app.memory.long_term import MemoryFact
from app.memory.ltm_manager import LongTermMemoryManager
from app.memory.query_router import MemoryQueryRouter, temporal_query
from app.memory.retrieval import MemoryRetriever, content_hash, time_eligible
from app.session.context_budget import estimate_message_tokens, estimate_text_tokens


def fact(name, content, **fields):
    values = dict(content=content, category="preference", created_at="2025-01-01T00:00:00+00:00",
                  updated_at="2025-01-01T00:00:00+00:00", observed_at="2025-01-01T00:00:00+00:00",
                  memory_id=name, source_type="user_explicit", source_session="source",
                  source_event_id=f"source:{name}", source_span=content, confidence=.9,
                  schema_version=2, predicate="prefers_color", value="black",
                  qualifiers={"product_category": "headphones"}, metadata={})
    values.update(fields)
    return MemoryFact(**values)


def retriever():
    return MemoryRetriever(model_path=None, enabled=False, min_score=.3)


def test_structured_category_prevents_cross_product_slot_recall():
    headphone = fact("ear", "用户偏好白色耳机", value="white")
    shoe = fact("shoe", "用户偏好黑色跑鞋", qualifiers={"product_category": "shoes"})
    results = retriever().retrieve("我的耳机喜欢什么颜色", [shoe, headphone], top_k=5)
    assert results == [headphone]


def test_chinese_bm25_returns_source_and_abstains_on_unrelated_query():
    black = fact("ear", "耳机优先选择黑色")
    unrelated = fact("reply", "希望回复简洁", predicate="response_detail", value="concise", qualifiers={})
    assert retriever().retrieve("黑色耳机", [black, unrelated], top_k=2) == [black]
    assert retriever().retrieve("火星天气", [black, unrelated], top_k=2) == []


def test_lexical_recall_remains_available_without_local_model(tmp_path, monkeypatch):
    engine = MemoryRetriever(str(tmp_path / "missing-model"))
    calls = []
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=lambda *a, **kw: calls.append((a, kw))))
    value = fact("ear", "黑色耳机")
    assert engine.prepare_embeddings([value]) == [value]
    assert engine.retrieve("耳机颜色", [value], 1) == [value]
    assert calls == []


def test_model_is_loaded_local_only(tmp_path, monkeypatch):
    calls = []
    model = SimpleNamespace(encode=lambda values, **kwargs: [[1.0, 0.0] for _ in values])
    def constructor(path, **kwargs):
        calls.append((path, kwargs))
        return model
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor))
    engine = MemoryRetriever(str(tmp_path))
    engine.prepare_embeddings([fact("ear", "耳机")])
    assert calls[0][1] == {"local_files_only": True}


def test_persisted_vectors_are_reused_and_reads_encode_only_query(monkeypatch):
    calls = []
    class Model:
        def encode(self, texts, **kwargs):
            calls.append(list(texts))
            return [[1.0, 0.0] if "短信" in text or text == "别来电打扰" else [0.0, 1.0] for text in texts]
    engine = MemoryRetriever(None)
    monkeypatch.setattr(engine, "_model_key", lambda: "local-model-v1")
    monkeypatch.setattr(engine, "_get_model", lambda: Model())
    sms = fact("sms", "只使用短信联系", predicate="contact_channel", value="sms", qualifiers={})
    email = fact("mail", "邮件联系", predicate="contact_channel", value="email", qualifiers={})
    engine.prepare_embeddings([sms, email])
    assert len(calls) == 1 and len(calls[0]) == 2
    stored = [deepcopy(sms), deepcopy(email)]
    json.dumps([item.metadata for item in stored])
    engine.prepare_embeddings(stored)
    assert len(calls) == 1
    assert engine.retrieve("别来电打扰", stored, 1) == [stored[0]]
    assert calls[-1] == ["别来电打扰"]
    assert len(calls) == 2
    stored[0].content = "改为微信"
    stored[0].value = "chat"
    assert stored[0].metadata["embedding"]["content_hash"] != content_hash(stored[0])
    assert 0 not in engine._vector_scores("别来电打扰", stored)
    engine.prepare_embeddings(stored)
    assert len(calls[-1]) == 1 and "改为微信" in calls[-1][0]
    stored[0].metadata["embedding"] = "corrupt-cache"
    stored[1].metadata["embedding"]["vector"] = [float("nan")]
    assert engine._vector_scores("别来电打扰", stored) == {}
    engine.prepare_embeddings(stored)
    assert len(calls[-1]) == 2


def test_rrf_combines_semantic_and_exact_slot_channels(monkeypatch):
    engine = retriever()
    black = fact("ear", "耳机偏好黑色")
    detail = fact("detail", "回复应简短", predicate="response_detail", value="concise", qualifiers={})
    monkeypatch.setattr(engine, "_vector_scores", lambda query, facts: {0: .8, 1: .99})
    assert engine.retrieve("耳机颜色", [black, detail], top_k=2)[0] == black


def test_hot_profile_and_topic_budgets_do_not_remove_cold_recall():
    values = [fact(f"shoe-{i}", f"跑鞋偏好项{i}", qualifiers={"product_category": "shoes"}) for i in range(25)]
    cold = fact("rare-ear", "耳机型号偏好玄夜色", confidence=.1, metadata={"tier": "cold"})
    views = build_views([*values, cold], hot_limit=20, summary_tokens=300)
    assert len(views["profile"]["refs"]) == 20
    assert "rare-ear" not in {ref["memory_id"] for ref in views["profile"]["refs"]}
    assert retriever().retrieve("玄夜色耳机", [*values, cold], 1) == [cold]
    for view in views["topics"].values():
        assert estimate_text_tokens(json.dumps(view, ensure_ascii=False, separators=(",", ":"))) <= 300
        assert resolve_view(view, [*values, cold])


@pytest.mark.parametrize("change", ["version", "content", "retracted", "tamper"])
def test_derived_topic_is_rejected_after_any_stale_reference(change):
    value = fact("ear", "黑色耳机")
    view = build_views([value])["topics"]["headphones"]
    assert resolve_view(view, [value]) == [value]
    if change == "version":
        value.version += 1
    elif change == "content":
        value.content = "白色耳机"
    elif change == "retracted":
        value.status = "retracted"
    else:
        view["text"] = "捏造的新偏好"
    assert resolve_view(view, [value]) == []


def test_historical_validity_uses_world_time_and_excludes_retract_tombstones():
    old = fact("old", "原来喜欢白色耳机", value="white", status="superseded",
               valid_from="2025-01-01T00:00:00+00:00", valid_to="2025-06-01T00:00:00+00:00")
    current = fact("new", "现在喜欢黑色耳机", valid_from="2025-06-01T00:00:00+00:00")
    tombstone = fact("retract", "撤销旧偏好", operation="RETRACT", status="retracted")
    assert retriever().retrieve("耳机颜色", [old, current, tombstone], 5) == [current]
    assert retriever().retrieve("耳机颜色", [old, current, tombstone], 5, as_of="2025-03-01") == [old]
    assert set(f.memory_id for f in retriever().retrieve("耳机颜色", [old, current, tombstone], 5, history=True)) == {"old", "new"}
    # Recorded supersession is a different timeline from validity.
    current.superseded_at = "2025-01-01T00:00:00+00:00"
    current.valid_to = "2099-01-01T00:00:00+00:00"
    assert time_eligible(current)
    current.metadata["reset"] = True
    assert not time_eligible(current, history=True)
    archived = fact("legacy", "归档的旧偏好", status="historical", metadata={"archived_legacy": True})
    assert not time_eligible(archived, history=True)
    assert not time_eligible(archived, as_of="2025-03-01")
    cancelled = fact("cancelled", "撤销的未来偏好", status="retracted",
                     valid_from="2099-01-01T00:00:00+00:00", valid_to="2026-01-01T00:00:00+00:00")
    assert not time_eligible(cancelled, history=True)
    assert not time_eligible(old, as_of="invalid-date")


def test_manager_history_labels_budget_and_actual_injection_dedup(tmp_path, monkeypatch):
    manager = LongTermMemoryManager(None, "", user_id="owner", memory_dir=str(tmp_path),
                                    retrieval_enabled=False, retrieval_min_score=.15,
                                    semantic_retrieval_top_k=4, prompt_tokens=1200)
    current = fact("current", "喜欢黑色耳机", valid_from="2025-06-01T00:00:00+00:00")
    old = fact("old", "原来喜欢白色耳机", value="white", status="superseded",
               valid_from="2025-01-01T00:00:00+00:00", valid_to="2025-06-01T00:00:00+00:00")
    manager.ltm.facts = [current]
    monkeypatch.setattr(manager.ltm, "history_facts", lambda: [old, current])
    sections = manager.build_memory_prompt_sections("记得我耳机颜色吗")
    assert estimate_message_tokens(sections) <= 1200
    assert manager.injected_ids == {"current"}
    assert manager.retrieve_relevant_long_term_memory("耳机颜色", exclude_ids=manager.injected_ids) == []
    manager.set_injected_ids_from_messages([])
    assert manager.retrieve_relevant_long_term_memory("耳机颜色", exclude_ids=manager.injected_ids) == [current]
    sections = manager.build_memory_prompt_sections("2025-03-01我喜欢什么颜色的耳机")
    assert "历史版本" in sections[0]["content"]
    assert '"memory_id": "old"' in sections[0]["content"]
    assert '"memory_id": "current"' not in sections[0]["content"]


def test_router_separates_commitments_realtime_preferences_and_time():
    router = MemoryQueryRouter()
    assert router.decide("昨天你不是说要给我优惠券吗").route == "events"
    assert not router.decide("昨天你不是说要给我优惠券吗").should_auto_inject
    assert router.decide("查询订单DX123的物流").route == "realtime"
    assert router.decide("记得订单DX123的颜色吗").route == "realtime"
    assert router.decide("查询我耳机颜色偏好").should_auto_inject
    assert router.decide("查询耳机物流").route == "realtime"
    assert router.decide("推荐适合我的耳机").should_auto_inject
    history, at = temporal_query("昨天的偏好", datetime(2026, 10, 15, 8, tzinfo=timezone.utc))
    assert history and at.startswith("2026-10-14")


def test_reconfirming_an_unchanged_fact_reuses_the_persisted_vector(tmp_path, monkeypatch):
    from app.memory.long_term import LongTermMemory
    from datetime import timedelta
    calls = []
    class Model:
        def encode(self, texts, **kwargs):
            calls.append(list(texts))
            return [[1.0, 0.0] for _ in texts]
    memory = LongTermMemory(user_id='u', memory_dir=str(tmp_path))
    monkeypatch.setattr(memory.retriever, '_model_key', lambda: 'fixture-model')
    monkeypatch.setattr(memory.retriever, '_get_model', lambda: Model())
    source_time = datetime.now(timezone.utc) - timedelta(days=1)
    first = fact('first', '用户偏好黑色耳机', created_at=source_time.isoformat(),
                 updated_at=source_time.isoformat(), observed_at=source_time.isoformat(), source_event_id='s:1')
    second = deepcopy(first)
    second.source_event_id = 's:2'
    second.created_at = second.updated_at = second.observed_at = (source_time + timedelta(hours=1)).isoformat()
    memory.merge_and_save([first], source_batch_id='s:0:1')
    memory.merge_and_save([second], source_batch_id='s:1:2')
    assert len(calls) == 1
    assert memory.facts[0].metadata['embedding']['model'] == 'fixture-model'
