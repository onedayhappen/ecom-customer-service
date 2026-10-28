"""Replay held-out receipts through publication and export inspectable answers.

Run: .venv/Scripts/python.exe scripts/eval_customer_reply_format.py
The pytest suite also exercises valid/invalid model selections and metadata
invariance. This report itself uses the deterministic no-model path.
"""
import html
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.settings import settings
from tests.test_customer_reply_variety import CASES, errors, publish_case


def main():
    settings.memory_enabled = settings.short_term_memory_enabled = False
    results = []
    with tempfile.TemporaryDirectory(prefix="customer-reply-eval-") as temporary:
        for i, case in enumerate(CASES):
            answer = publish_case(Path(temporary) / f"case-{i}.db", case)
            results.append(dict(id=case["id"], source=case["source"], user=case["user"],
                                tool=case["receipts"][0]["tool"], expected=case["must"],
                                answer=answer, errors=errors(case, answer), characters=len(answer)))
    destination = ROOT / "data/evaluation/customer_reply_variety_20261028"
    destination.mkdir(parents=True, exist_ok=True)
    failures = sum(bool(row["errors"]) for row in results)
    summary = dict(cases=len(results), passed=len(results)-failures, failed=failures,
                   archived_receipts=12, contract_fixtures=6, model_calls=0, business_operations=0,
                   scope="Final publication replay; not live multi-turn conversation or model evaluation")
    (destination / "replay.json").write_text(json.dumps(dict(summary=summary, results=results),
                                            ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    sections = []
    for row in results:
        source = "历史工具回执" if row["source"] == "archived" else "工具契约构造样例"
        sections.append(f'<section><h2>{html.escape(row["id"])} · {"失败" if row["errors"] else "通过"}</h2>'
                        f'<p>{source} · {html.escape(row["tool"])} · {row["characters"]} 字符</p>'
                        f'<p><b>用户：</b>{html.escape(row["user"])}</p>'
                        f'<pre>{html.escape(row["answer"])}</pre>'
                        f'<p>关键事实检查：{html.escape("、".join(row["expected"]))}</p>'
                        f'<p>{html.escape("；".join(row["errors"]))}</p></section>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>客服回复跨场景回归</title>
<style>body{font:16px/1.7 system-ui;max-width:1050px;margin:30px auto;padding:20px;background:#f6f8fb;color:#223047}
section{padding:18px 24px;margin:20px 0;border:1px solid #dbe2ed;border-radius:10px;background:white}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:16px/1.7 system-ui}h2{font-size:20px}</style>
<h1>客服回复：跨场景验证</h1>
<p>18 个新场景，不包含此前手机、锅具用例：12 份历史真实工具回执和 6 个按工具契约构造的边界样例。
用户问法为本次设计，历史回执来自演示业务数据。逐例检查关键事实、技术字段和回复长度。</p>
<p>本报告经过最终发布链路，关闭模型与记忆，不执行业务操作。配套测试另行覆盖正常模型选择、
无效模型输出、无模型，以及嵌套元数据注入和字段重排。不是新的完整多轮对话或模型理解能力评测。</p>
<p>修复涵盖：优惠券详情与范围明细分离；读取明确的嵌套商品参数；保留会员到期、配送时效、
退货快递要求；明确空记录含义；将旧限制中的内部枚举转为中文，并保留完整业务条件。</p>
'''
    page += f'<p>结果：{len(results)-failures}/{len(results)} 通过。</p><p><a href="replay.json">完整回放数据</a></p>'
    (destination / "report.html").write_text(page + "".join(sections) + "</html>", encoding="utf8")
    print(json.dumps(summary, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
