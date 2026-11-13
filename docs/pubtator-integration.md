# PubTator独立参考与缓存

2026-11-06接入。**9B实体抽取、Bonsai关系抽取照常执行；PubTator有结果也不跳过本地模型。** 新知识提取批次默认启用，文献全量采集任务不触发该接口。接口使用公开PMID获取已有标注，不上传本地文稿，不需要新增模型或GPU。

## 实际边界

本地链保持为：入选证据片段 → 9B实体 → Bonsai关系 → 脚本组装及交付。之后运行独立PubTator参考任务，范围必须与`selected-batch/packets.json`一致，并核对文章版本和原文没有变化。

两套输出分别保存：

- 原9B、Bonsai、知识包、目标适配包保持原样。
- `BATCH/pubtator/reference.json`保存外部标注、与本地文章版本/段落的对应、可补充的实体或ID候选、拒绝原因，以及请求/缓存统计。
- 原始API响应保留外部关系，但本轮不自动采用这些关系；补充候选也不自动写进知识包或送入Bonsai。它们用于独立比较与补全依据，不能将上下文分子直接绑定为疗效条件。

新批次通过`scripts/run_minimal_pipeline.py run`或调用它的`scripts/run_literature_batch.py`自动执行参考任务。已有冻结批次重放仍沿用原配置；可独立补参考，不重跑模型：

```bash
.venv/bin/python -B scripts/pubtator_reference.py \
  --batch var/experiments/ten-paper-closure-20261102
```

新建最小批次可用`run --no-pubtator`关闭；配置一经冻结，不在同批次中切换。`--pubtator-cache`可指定缓存根目录，默认如下。

## 缓存、限速与恢复

- 共享缓存：`/appdata/02_resource/apps/biomed/resources/pubtator3/`。`objects/`按响应SHA-256保存不可变JSON；`index/`按PMID和全文/摘要模式保存指针、请求URL、获取时间和有效期。旧响应不会因刷新而被删除。
- 全文与摘要分别缓存。全文请求可能只有摘要，状态明确为`abstract_only`，不能当作取得全文；两种响应的关系内容也可能不同。
- 成功标注缓存7天；缺失结果、全文模式仅有摘要时缓存24小时。缓存命中校验内容摘要与PMID，损坏标记`cache_invalid`，不冒充成功。
- PubTator共享目录的跨进程锁保证单路请求，每批最多10个PMID，请求开始间隔至少2秒，响应读取上限256 KiB/s、16 MiB。现有文献采集限速未修改；这是每条请求链的应用层读取限制，不是服务器总带宽限制。
- 临时HTTP/连接错误等待5秒、15秒后重试，最多3次。连续失败、永久错误或较长`Retry-After`进入至少30分钟来源冷却；其他批次遵守同一冷却时间，不循环冲击API。
- API缺失、超时、缓存忙或故障记录在独立参考状态里，不改写本地模型成功/失败和已交付知识。没有PubTator结果仍能使用原本的9B＋Bonsai流程。
- 已生成的参考文件为本批次快照，重放直接复用，不因缓存过期改写旧来源。中断前已落盘的响应可复用；若已有`partial/unavailable`报告，冷却结束后用新的`--output`目录补抓，保留旧报告：

```bash
.venv/bin/python -B scripts/pubtator_reference.py \
  --batch var/experiments/ten-paper-closure-20261102 \
  --output var/experiments/ten-paper-closure-20261102/pubtator-retry-1
```

## 补充候选规则

1. 外部段落与本地段落经Unicode/空白标点规范化后匹配；长段相似度至少0.98，短段必须一致。实体本身必须校验外部偏移、映射到本地精确原词和字符范围。未对齐文本不生成候选。
2. Gene只有在外部NCBI Gene ID与本地名称/别名唯一一致时，才提供独立gene或链接候选；PSA等同名歧义不强选。
3. Chemical仅补已有同名drug/treatment，且外部标准名须唯一对应本地RxNorm成分；不把所有Chemical都当作药物。新药仍由原本地新增机制处理。
4. Disease映射不自动采用，保留现有主癌种/亚型判断；候选与既有同类型实体ID冲突时拒绝，不覆盖原结果。
5. 9B失败的片段不能用外部标注冒充本地抽取成功。所有候选标记`relation_inferred=false`，保留文章版本、段落位置及外部响应来源。

标准字典复用现有`knowledge-candidates-20261023/standards-snapshot.json`，本轮没有重新读取tumor_api或写生产库。原比较和已知错例见[027实际对照](research/027-pubtator-comparison.md)。

## 实际验证

复用十篇闭环中真正进入提取的9篇、182个片段组，未请求已过滤的第10篇，也未重跑模型：

| 项目 | 结果 |
| --- | --- |
| 首次API读取 | 2次请求，842,784字节，包含对齐/字典处理共15.47秒 |
| 标注范围 | 9篇均有返回，3篇有全文，其余为摘要 |
| 新参考版本复用缓存 | 18个模式缓存命中，0次请求、0网络响应字节，处理8.03秒 |
| 共享缓存占用 | 857,947字节，22个文件 |
| 独立补充候选 | 296处，涉及26个不同标准概念；同实体在不同片段组重复计处数 |
| 原文定位 | 70个唯一段落对齐，6,562次标注引用逐一通过本地字符范围核对 |
| 本地结果 | 输入、9B库存、Bonsai组装文件及已有交付文件共12个文件内容未变化 |

候选数量不是新增知识数量，也不是正确率；没有对182组重新做医学语义评分。首次/缓存复用的逐组参考结果完全相同，未调用新模型。

记录位于`var/experiments/pubtator-integration-20261106/validation.json`；主批参考位于`var/experiments/ten-paper-closure-20261102/pubtator/reference.json`，两者实际在`/appdata/02_resource/apps/biomed/research/artifacts/`下。

48项针对性测试及ruff检查通过，覆盖限速、重试、冷却、缓存复用/损坏、跨进程锁、原文定位、模型失败隔离、旧批次兼容，以及API有结果时本地两步模型仍执行。实现位于`apps/curation/pubtator.py`、`scripts/pubtator_reference.py`及批次入口；没有新增依赖或数据库迁移。
