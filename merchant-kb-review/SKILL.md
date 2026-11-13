---
name: merchant-kb-review
description: 逐行 LLM 复核商户知识库（商户名/关键词/类目），支持规则引擎预复核、全量可中断续跑、分批复核、子代理并行，并汇总成与原全量复核工作簿同格式的 Excel。
---

# 商户知识库逐行复核

## 用途

对 `merchant_kb.csv`（`merchant_name,keywords,category`）做逐行语义复核，补足规则无法判断的名称、关键词归属和类目问题。

核心原则：**模型逐行判断，不依赖规则结论；不确定就标待核实，不臆造商户、关键词或类目。**

## 目录约定（每个功能完全独立）

```text
merchant-kb-review/
├── SKILL.md       功能说明与操作手册
├── prompts/       子代理提示词
├── scripts/       功能脚本
├── data/          输入数据与导出结果（不进 git）
└── work/          运行状态、批次与结果（不进 git，可随时中断续跑）
```

新增功能时在仓库根目录新建同级 `<feature>/` 目录，并在 `.claude/skills/<feature>/SKILL.md` 放一个指向它的入口；功能之间不共享代码、数据或运行状态。

## 全量模式（可中断续跑，推荐）

用 `full_run.py` 把 874,777 行全部纳入，状态存 SQLite，Excel 只是导出结果；任何时候中断，重跑 `status`/`next` 即可续上。

以下命令在 Agent 根目录执行。`--run-dir` 默认 `work/full`，导出默认 `data/merchant_kb_reviewed_AI.xlsx`，均位于本功能目录内，无需传参。

```bash
# 1. 初始化：导入 CSV；data/ 下有原规则工作簿时自动带上 A–M 列
python merchant-kb-review/scripts/full_run.py init \
  --kb merchant-kb-review/data/merchant_kb.csv

# 2. 生成待复核批次（已有 open 批次会原样返回，不会重复分配）
python merchant-kb-review/scripts/full_run.py next --n 1000 --batch-size 100

# 3. 子代理按 merchant-kb-review/prompts/REVIEWER.md 复核，结果写到 merchant-kb-review/work/full/results/batch_XXXXX.jsonl

# 4. 回收结果；缺失的结果行会自动退回 pending
python merchant-kb-review/scripts/full_run.py ingest

# 5. 查看进度（同时写 progress.json）
python merchant-kb-review/scripts/full_run.py status

# 6. 随时导出全量工作簿；未检测行仍显示原规则结果
python merchant-kb-review/scripts/full_run.py export
```

## 规则复核引擎（先跑规则，再跑 AI）

`rule_review.py` 用从 AI 结论中提炼的高精度机械规则处理可确定的重复劳动；规则只删除「去掉前缀/噪声后与同列表另一保留词完全相同」的变体，因此不会降低银行流水匹配覆盖。语义判断（类目、他人关键词、待核实、删除行）仍交给 AI。

```bash
SKILL=merchant-kb-review/scripts

# 持续沉淀：用已有 AI 结论评估各规则命中与一致率，输出 rule_analysis.json
python $SKILL/rule_review.py analyze

# 对全部 pending 行应用规则；命中行写回状态库（review_source=RULE）
python $SKILL/rule_review.py apply
python $SKILL/rule_review.py apply --dry-run   # 只预览
python $SKILL/rule_review.py stats             # 覆盖率/规则分布
```

当前规则（v1.1，均以 AI 结果验证）：

| 规则 | 内容 | AI 一致率 | 覆盖 |
|---|---|---|---|
| `R001_payment_prefix` | 删除 `SQ/SMP/ZLR/EZYPAY` 前缀变体，保留等价干净词 | 85.1%（ZLR/EZYPAY 100%、SQ 88.9%、SMP 78.7%） | 34,422 行 |
| `R002_noise_variant` | 删除交易噪声变体（CASH OUT/EFTPOS/BPAY/TRANSFER DEBIT 等），保留等价干净词 | 96.8% | 1,027 行 |
| `R003_name_repeat` | 合并商户名尾部相邻重复短语（如 `X PERTH PERTH` → `X PERTH`） | 65% 会被 AI 改名；仅作建议，置信度中 | 2,312 行 |

- v1.1 加固：与商户名完全相同的关键词永不删除（避免 `XXXX TILING`/`POS CAFE` 这类噪声模式误伤本名）；删除后做不动点校验，等价保留词自身也被删时恢复，避免整行删空。
- 扩展尝试均被数据否决，勿重复试：后缀变体去重（AI 只删 7.9%）；把「包含关系」放宽为删除较短词（覆盖保证不成立）；删除较长词（AI 同意率仅 39–45%）。当前 802,192 行 pending 中精确规则命中为 0，规则侧已饱和。

- `apply` 只把规则命中的行标为 done（`建议修改`），未命中行保持 pending 留给 AI；默认不做“干净行=无需更改”的批量判定（实测会误标约 6.8%）。
- `review_source` 区分 `AI` / `RULE`；`review_rule` 记录命中规则，审计明细在 `run_dir/rule_results/rule_<时间>.jsonl`。
- 每条新规则先用 `analyze` 看一致率，稳定后再加入 `PAYMENT_PREFIXES` / `NOISE_PATTERNS`；低于约 75% 的规则不启用。

## 快速模式（大批量，完成即补位）

积压多时用：并发上限取决于 `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`（本机 20）。

1. 一次分配超过并发数的批次，如 `next --n 3000 --batch-size 100`（30 批，20 并发 + 10 排队）。
2. 一次性启动 20 个子代理；**每完成一个就启动下一个排队批次**，不要等整波结束。
3. 所有结果文件齐了再 `ingest`。`ingest` 只处理已有结果文件的批次，但半写入的文件会被误判 `partial` 并把缺行退回 pending，所以不要在波次中途 ingest。
4. 实测：10,000 行（100×100，20 并发 + 补位）约 33 分钟（≈3.3 分钟/千行，100 行/批可摊薄代理启动开销）；2,000 行（20×100，单波 20 并发）约 8.4 分钟（≈4.2 分钟/千行）；2,000 行（40×50）约 12 分钟（≈6 分钟/千行）；积压 ≥2 波时 3,000 行约 16 分钟（≈5.3 分钟/千行）；原 10 并发整波模式约 9.2 分钟/千行。只有 1 波时无补位可摊薄，耗时=最慢代理，实测 1,000 行（20×50）约 9 分钟——快速模式只对 ≥2 波积压有收益。

小模型粗筛已测试、不采用：Haiku 跑 100 行仅 62 秒、结论一致率 95%，但 Sonnet 标记的 5 行里漏掉 3 行（召回 40%），且漏的恰是需要外部核实的类目修正，不能作为最终判定。

## 每轮执行流程（模型照此操作）

1. 先运行 `status`。若 `pending_result_files > 0`，先 `ingest`。
2. 运行 `rule_review.py apply`，让规则先消化可机械修复的 pending 行；随后 `status` 确认 `done_rule` 增长。
3. 运行 `next --n 1000 --batch-size 100`，拿到 `open_batches`（每批 100 行，一轮约 1,000 行）。
4. 按 `open_batches` 并行启动子代理；每个子代理负责 1–2 个批次文件，提示词：

   ```text
   读取 <skill目录>/prompts/REVIEWER.md 并严格执行。
   逐行复核以下输入文件：<batch 文件绝对路径列表>。
   将结果写入 <run_dir>/results/ 下的同名文件（batch_XXXXX.jsonl），每个输入 idx 输出一行 JSON。
   不要遗漏或重复 idx；写完后核对行数与 idx 集合，再汇报结论分布。
   ```

5. 子代理全部结束后运行 `ingest`，再运行 `status` 确认 done 增长、errors 为 0。
6. 需要交付时运行 `export`；未检测的行 N 列仍为 `否`，可下一轮继续。

用户中断后重新进入，只需从第 1 步开始；`status` 的 `next_queued_idx`/`open_batches` 就是继续位置，已 done 的行不会重复分配。

## 规则与限制

- `init` 只跑一次；要重来必须加 `--reset`，会清空该 run-dir 的状态和批次。首次可加 `--limit 1000` 验证流程，确认后再 `--reset` 全量初始化。
- 不传 `--rule-workbook` 时，若 `merchant-kb-review/data/merchant_kb_全量复核_漏检与调整.xlsx` 存在会自动使用；都没有则 A–M 列留空。
- **中断后的续跑顺序**：先跑 `status` 看 `resume_hint`；若 `pending_result_files > 0`，先 `ingest` 回收已有结果；再跑 `next` 拿 open 批次继续；全部 done 后 `export`。
- `next` 先返回已有 `open` 批次；进程被杀后重新执行即可，已 `done` 的行不会再次分配。
- `ingest` 是可重复执行的；某批只回传一部分时，该批标为 `partial`，缺失行退回 `pending`，下一轮自动补批。
- `rule_review.py apply` 可重复执行；它只处理 `pending` 行，已 done 行不会重复。AI 批次里若仍有规则可处理的变体，说明该规则尚未稳定，应先 `analyze` 再决定是否扩规则。
- `status` 输出 `done/done_ai/done_rule/queued/pending`、`last_done_idx`、`next_pending_idx`、`pending_result_files` 和 open 批次，并写入 `progress.json`；这是判断“从哪里继续”的唯一依据。
- 导出工作簿的 `说明` 工作表也记录“下一个待检测 idx”和未完成批次数，可直接对照。
- 默认导出到 `merchant_kb_reviewed_AI.xlsx`，不覆盖原规则工作簿。只有 `全量核查` 和 `说明` 两张表会被生成；原工作簿的 `复核问题明细`、`关键词冲突` 等辅助表不会重建。
- 全量约 874,777 行；按 100 行/批、10 个并行代理估算，约 7 天、6.5 亿 tokens，建议分多次运行。

全量工作簿 `全量核查` 列（默认 15 列，与原工作簿同格式 + 2 列）：
- A–M：`merchant_name`、`keywords`、`category`、`变更摘要（原 → 新）`、`更改后的关键词配置`、`更改后的类别`、`更改原因`、`是否需要更改`、`复核结论`、`复核说明`、`复核建议商户名`、`原核查状态`、`idx`。
- N：`是否已AI检测`，`是`/`否`（`是`=已有复核结果，含规则复核）。
- O：`review_source`，`AI` 或 `RULE`；未复核为空。
- N=`是` 时，D–M 为复核结果（`更改原因`/`复核说明` 填理由，`复核说明` 以 `[置信度X]` 开头）；N=`否` 时，D–M 暂显示原规则结果，仅作占位。
- 加 `--detail` 可导出 23 列审计版：A–M 完全保留原规则结果，N–V 为 `AI复核结论`、`AI置信度`、`AI问题类型`、`AI复核说明`、`AI建议商户名`、`AI更改后的关键词配置`、`AI更改后的类别`、`AI应用级别`，W 为 `review_source`。

## 输出字段

| 字段 | 说明 |
|---|---|
| `idx` | 数据行序号，从 0 开始 |
| `verdict` | `无需更改` / `建议修改` / `待核实` / `建议删除行` |
| `new_name` | 仅建议改名时填写 |
| `new_category` | 仅建议改类目时填写，必须是 22 个类目之一 |
| `new_keywords` | `建议修改` 时填写完整最终关键词列表；其他情况可为空 |
| `issues` | `名称` / `关键词` / `类目` / `重复` / `PII` / `其他` 的子集 |
| `reason` | 中文，简短说明依据 |
| `confidence` | `高` / `中` / `低` |

## 类目枚举

Home Improvement, Dining Out, Health, Groceries, Transport, Financial Institutions, Retail, Automotive, Personal Care, Information, Education, Entertainment, Travel, Gyms and other memberships, Rent, Pet Care, Donations, Insurance, Utilities, Telecommunications, Subscription TV, Department Stores

## 质量要求

- 商户名是真实商户，不是转账/退款/终端/个人交易描述。
- 关键词必须能匹配该商户的银行流水文本；删除他人关键词、纯通用词、交易噪声和 PII。
- 品牌缩写（LS、LSP、PIN、POI 等）只有在确认是交易描述时才删，不能因为短就删。
- 没有全局重复证据时，不自动合并行；只标记待核实。
- `建议修改` 必须给出完整最终关键词列表，不能只给删除/新增片段。
- 单条理由不超过 100 字，不复述整行数据。
