# 013 · 附加实体解耦与英文原文核验

2026-10-29，北京时间。复用012同5篇、15组原始9B输出；本轮没有新9B调用或微调。实际Astra只核验K07、K12及证据摘录，未重写全组评分。未改采集、tumor_api或生产知识。

[研究013](http://192.168.124.2:50011/management/research/?experiment=core-gaps-20261029)。实验保存在`var/experiments/core-gaps-20261029/`，实际目录为`/appdata/02_resource/apps/biomed/research/artifacts/core-gaps-20261029/`。

| 同15组指标 | 012名称对齐后 | 013 |
|---|---:|---:|
| 核心内容通过 | 14/15 | 14/15 |
| 结构与定位通过 | 14/15 | 15/15 |
| 核心与技术均通过、可导出 | 13/15 | 14/15 |
| 新9B调用 | — | 0 |

新增可导出项是K07。**收益来自附加实体与核心事实解耦，不是模型更准。** 原始模型核心及原名评分仍为13/15；012大小写对齐后的14/15也保持不变。脚本处理约0.070秒，不含文件读写、导出序列化及Astra研究审核。14条仍含K04/K01内容重叠，不等于14条独立知识。

## K07：附加ID不应连带阻断核心

原文的趋势证据是：

> Patients with early progression had a trend toward higher scores for this TREM2-associated gene signature than patients who achieved disease control (P = 0.054; Fig. 4B)

文章为ipilimumab与nivolumab联合治疗的前列腺癌队列。药物、癌种、签名及其成员均有出处；P=0.054保留为“趋势”，不改成显著关联，也不把签名拆成7条独立耐药基因关系。

`K07.e20`的`TREM2+ TAMs`只是额外选入的ID：上游无原文位置，既未出现在所选核心事实文本中，也不是该药物—癌种—签名关系的必要参与项。本轮只将它移入`unresolved_entity_refs`，保留原词、原始ID选择和原因，不伪造位置或确认ID。未知ID、无法定位的核心类型、出现在事实中的必要词以及有损坏位置的实体仍不能这样延期。

## K12：原文清楚，生成字段把细胞写成小鼠

原文明确写道：

> We also tested combination on 4T1.2-HER2 and MB231-HER2 cells. Combined treatment with T-DXd (4 nM) and TTP-488 (0.5 μM) resulted in significantly lower viability than T-DXd alone in both models (Supplementary Figure 3).

旧输出却在`conditions`中写入`T-DXd used at 4 nM in mouse models`，并在`scope`混入文章级in vivo概述。**错误在生成的条件与范围绑定，不是该原文含糊。** 原文中的both models承接前句cells；不能把4 nM借给同篇另一段的小鼠研究。本轮保留失败输出，K12仍不可导出，没有通过删实体或删证据级别放行。

乳腺癌是业务癌种，细胞、动物、人体是证据上下文，两者分别记录。不能由“关注乳腺癌”推出只收人体研究，也不自动删除临床前药物关系。

## 展示与下一轮边界

页面K07、K12主文展示经Astra核验并由脚本验证位置的**英文原文摘录**；旧模型概括在展开字段中。其余卡片仍显示012生成陈述供对照，全部卡片均保留文章、完整原段落和原始审核。摘录展示不代表已运行新的证据句抽取模型。

取消生成概括可能减少新增断言，也可能丢掉跨句关系、指代和检索所需结构。**013未比较这两种方式，不能据此决定整体取消概括。** `quote-projection-discussion.json`只是单例前瞻讨论，未执行。下一轮须用同15组、同模型对照原输出与较窄输出契约，再比较错误、遗漏、可用性和耗时；目前没有该轮结果。

- `evaluation.json`：15组前后结构状态、原始/流程审核、延期原因、脚本耗时及源哈希。
- `knowledge-items.json/jsonl`：14条研究条目；原始事实、名称对齐视图、延期项、英文全文段与两层出处完整保留。
- `astra-gap-audit.json`：K07/K12实际核验、精确英文引文与位置；Astra是模型参考，不是人工金标准。
- `executed-code.json`、`decisions.json`：实际代码与本轮边界；旧012全部保持冻结。
