"""长期记忆 query 路由测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.memory.query_router import MemoryQueryRouter


def test_explicit_order_query_skips_auto_memory():
    router = MemoryQueryRouter()

    decision = router.decide("查询订单DX123的物流")

    assert not decision.should_auto_inject


def test_commitment_query_routes_to_source_events():
    router = MemoryQueryRouter()

    decision = router.decide("昨天你不是说要给我一张优惠券嘛，到我账户了吗")

    assert not decision.should_auto_inject
    assert decision.route == "events"


def test_plain_policy_query_does_not_force_memory():
    router = MemoryQueryRouter()

    decision = router.decide("优惠券多久到账")

    assert not decision.should_auto_inject
    assert decision.reason in {"realtime_query", "no_clear_memory_need"}


if __name__ == "__main__":
    test_explicit_order_query_skips_auto_memory()
    test_commitment_query_routes_to_source_events()
    test_plain_policy_query_does_not_force_memory()
    print("长期记忆 query 路由测试通过")
