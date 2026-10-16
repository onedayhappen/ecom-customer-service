# CSDS 买家多轮对话提取

已从作者公开下载链接取得原始 train/val/test，共 10,701 段；仅保留 QRole 为“用户”的 9,910 段买家会话。没有生成对话、改写原话、补充客服回复或使用旧种子集。

## 本次产物

| 原分集 | 原始会话 | 保留买家会话 | 待标注用户轮次 |
| --- | ---: | ---: | ---: |
| train | 9,101 | 8,413 | 96,068 |
| val | 800 | 750 | 8,542 |
| test | 800 | 747 | 8,142 |
| 合计 | 10,701 | 9,910 | 112,752 |

排除 791 段商家、物流人员等身份的咨询。保留数据共 258,388 条用户/客服消息，其中用户消息 129,409 条。候选轮次要求前面至少已有一条用户消息和一条客服消息；所以不是每条用户消息都成为候选。

**意图标签尚未标注。** 112,752 是同批会话展开后的候选前缀数量，不是独立会话数量，也不是已完成的训练样本数。后续还需按 31 类逐轮标注、检查单订单/商品范围及近似重复，才能生成正式微调文件。

## 文件与字段

- `train_dialogues.jsonl / val_dialogues.jsonl / test_dialogues.jsonl`：每行一段完整对话。包含原 DialogueID、Session_id、原分集及 messages。每条消息保留原 source_turn，Q 映射为 user，A 映射为 assistant，content 原样保存。
- `train_candidates.jsonl / val_candidates.jsonl / test_candidates.jsonl`：待标注索引，记录 dialogue_id、目标消息下标与原始轮次。使用对应对话的第 0 条至目标消息构造输入，禁止包含目标之后的客服回复。
- `excluded_dialogues.jsonl`：被排除会话的 ID、角色及原因，不包含整段文本。
- `extraction_report.json`：来源、源文件及输出文件 SHA-256、逐集合统计、检查结果与限制。

采用“对话存一份、前缀存索引”的方式，避免把同一长对话重复复制数十次。索引不包含 intent、expected_intent 或摘要，label_status 为 unlabeled，不能直接传给当前训练脚本。

## 原文与隔离规则

1. 只读取原 Dialogue，不把 QA、Topic、UserSumm、AgentSumm、FinalSumm 当对话或标签。
2. 保留原文分词空格、占位符、用户和客服连续发送的消息，不强制伪造 user/assistant 交替。后续 SFT 导出需要单独处理连续角色及开头客服消息；本次是原文提取层。
3. 原分集保持不变。同一 Session_id 及同一完整对话不得重复；已对全部保留会话检查，均未发现重复。完整对话对比忽略空白，但不会修改保存的原文。
4. 完整对话检查不等于语义去重。后续标注和导出还应检查跨集合近似重复、相同前缀及多对象问题。
5. 标注只看目标消息及此前上下文。即使另存有完整对话，也不能用后文或整段摘要推断当前意图。问候、收尾与 others 也在后续 31 类标注范围内。
6. 数据仍是未标注候选；不能因为缺少订单号就认定 others，也不能直接继承上游粗粒度主题标签。

## 复现与查看

在项目根目录执行：

```powershell
python scripts/extract_csds_dialogues.py --download
python scripts/extract_csds_dialogues.py --check
```

已存在本地来源缓存时，执行 `python scripts/extract_csds_dialogues.py` 即可重建。查看某个候选时，从 candidates 文件取 dialogue_id 和 target_source_turn：

```powershell
python scripts/extract_csds_dialogues.py --preview <dialogue_id> --turn <target_source_turn>
```

预览只输出截至目标用户消息的 role/content，不输出摘要、未来回复或来源元数据作为模型输入。轮次从 0 开始。

## 来源

- [官方仓库](https://github.com/xiaolinAndy/CSDS)
- [论文：CSDS: A Fine-Grained Chinese Dataset for Customer Service Dialogue Summarization，EMNLP 2021](https://aclanthology.org/2021.emnlp-main.365/)
- [作者提供的下载目录](https://drive.google.com/drive/folders/1IrpEUTR2ZanN0ZKmKcbaXjruTAD9JLkF)
- [字段说明](https://github.com/xiaolinAndy/CSDS/blob/main/utils/dataset_introduction.md)

原始缓存位于 `data/source_cache/csds/`，提取文本和索引留在本地并继续被 Git 忽略；仅脚本、说明和不含对话文本的统计报告纳入可跟踪文件范围。独立数据许可尚未核实，不把论文许可自动套用于数据。
