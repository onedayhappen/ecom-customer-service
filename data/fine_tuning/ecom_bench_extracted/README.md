# ECom-Bench 已公开对话提取与逐轮意图标注

这不是种子集，不含补写的对话。当前只从论文附录 A.2/A.3 提取已公开的两段模拟对话，并由本次助手逐轮标注；不是原始真人客服日志，也不是独立人工复核的金标。**尚不能构成 31 类分类器的 train/dev/test。**

## 来源与核查结果

- 官方仓库：[XiaoduoAILab/ECom-Bench](https://github.com/XiaoduoAILab/ECom-Bench)，核查版本 `bc5018daaf45eea12330941bce68cebd293cfa85`。
- 检查了该版本完整 ZIP 文件目录及 `envs/story/data/*.json` 的数据结构，没有发现公开对话日志；数据目录是订单、商品、发票等业务数据库。`tasks.py` 是模拟器任务说明，不能当作多轮对话。
- 上游 `.gitignore` 忽略 `results/` 等输出。README 所述 JSON 对话轨迹是运行后产物，不表示仓库已经提供这些日志。此结论只针对记录的版本，不断言作者没有其他数据。
- 实际文本来源：[Wang 等，ECom-Bench，arXiv:2507.05639v2，附录 A.2/A.3](https://arxiv.org/html/2507.05639v2#A1.SS2)。论文声明 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，本目录提取文本及标注沿用此许可并保留署名。没有复制上游业务数据库。
- 原始 ZIP、论文 HTML 缓存在被 Git 忽略的 `data/source_cache/ecom_bench/`。文件哈希、仓库文件清单、许可、排除记录与缺失标签列于 `source_audit.json`。

## 文件

| 文件 | 内容 |
| --- | --- |
| source_dialogues.json | 2 段原始轨迹，共 25 条消息，含 11 条自然用户发言和 2 条模拟器终止标记 |
| annotated_pool.jsonl | 9 个多轮对话前缀；每条最后一条消息为用户，expected_intent 是该轮唯一标签 |
| source_audit.json | 来源版本、哈希、统计、缺失类别及限制 |

保留英文原文和历史客服回复，只合并排版空白，并修复 `quali fied / installa tion / screen shot / num ber` 四种断词。没有翻译、补写、换商品名扩增或复用旧种子；没有下载图片或生成 OCR。源码记录保留开场客服问候及 `###STOP###`，分类输入去掉无业务内容的开场客服问候，不把终止标记当用户话语。

两条首轮用户发言没有此前用户上下文，因此不作为本次多轮样本；其余 9 条均保留此前用户/客服上下文，截断在目标用户轮。后续客服答复、模拟器画像、任务说明、预期工具操作不进入输入。历史客服即使说错了也保留其实际表述，不把它当正确业务结果。

## 逐轮标注

下表“当前话语”是中文摘要，JSONL 中是英文原文。U 为原对话中第几次自然用户发言。

| 来源 | 当前话语摘要 | 标签 | 判断依据 |
| --- | --- | --- | --- |
| A.2 U2 | 接下来预约周四安装 | others | 已购商品安装预约暂不在 31 类中 |
| A.2 U3 | 查询安装服务信息，并提供商品链接 | others | 延续具体安装服务，不是商品参数或一般退换货政策 |
| A.2 U4 | 查询订单信息，并提供订单号 | order_inquiry | 当前明确查询订单 |
| A.2 U5 | 最后请开个人发票 | invoice_request | 切换为开票 |
| A.2 U6 | 补充订单号、抬头、联系方式 | invoice_request | 回应客服开票追问 |
| A.3 U2 | 同意，将差价退到京东 E 卡 | price_protection_service | 前文正在确认价保，不是退货退款 |
| A.3 U3 | 接下来预约周四安装 | others | 安装预约未覆盖 |
| A.3 U4 | 最后开个人发票 | invoice_request | 切换为开票 |
| A.3 U5 | 抬头用本人姓名，联系方式查订单 | invoice_request | 仍在补齐开票资料，不是独立订单查询 |

标签数量：invoice_request 4、others 3、order_inquiry 1、price_protection_service 1。安装服务这三条归 others 是当前 taxonomy 的范围限制，不是识别失败，也不据此擅自新增类别。

每条标注依据保留在 `annotation.rationale` 以便评审；模型只接收 `messages`，目标仍只是一个意图。来源、理由、轮次编号与标签不能拼进输入。

## 划分与使用限制

两段轨迹共享附录 A.1 的同一任务设定，必须属于同一个 `source_group`。所有样本 `split=unassigned`；没有生成 train/dev/test，避免同一任务或其前缀跨集合泄漏。9 个前缀不是 9 段独立对话，只覆盖 4/31 类且为英文，不能证明中文 0.5B 分类器有效。

下一步需要获取更多已有的中文轨迹日志，再逐轮标注，并先按原任务/会话分组划分，再展开对话前缀。缺失的 27 类不使用种子或虚构历史补齐。若选择运行 ECom-Bench 模拟器，得到的是新生成的模拟数据，不能记作本次“提取已有对话”；本次没有运行模拟器。

复现（仅 Python 标准库及本项目 taxonomy）：

```powershell
python scripts/extract_ecom_bench_dialogues.py --download
python scripts/extract_ecom_bench_dialogues.py --check
python scripts/eval_intent_model.py --eval-file data/fine_tuning/ecom_bench_extracted/annotated_pool.jsonl --validate-only
```

最后一个命令只检查格式，不代表正式测试集或模型性能。旧 `multiturn_intent_sft/train_v4.jsonl / dev_v4.jsonl / test_v4.jsonl` 的 527 条改编种子已弃用，不作为本轮语料，也不再默认用于模型评测。
