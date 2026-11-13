# 005 · 实体瘤治疗证据段落筛选

日期：2026-10-23。开发试验；无微调、无新增模型下载，不写回生产知识库。

5篇已有全文、282段：修订后的Astra参考含122个目标段落。9B召回95.1%、精确率80.6%；4B召回76.2%、精确率78.2%。规则组合有明显取舍，文献三分类继续4B，段落环节优先验证9B。

## 冻结任务与样本

判断中心段落是否包含可继续抽取的治疗证据；标题、位置和前后段仅用于消除歧义，不能把邻段结论搬到中心段。

正例包括实际治疗效果、无效结果、耐药与不良反应，明确的治疗适用条件或标准治疗表述，以及标志物与治疗反应的观察关联。临床、动物、细胞实验均可保留，但不合并证据层级；引述研究的具体结果作为二手证据保留。

负例包括泛泛背景、研究目的与假说、仅有方案/剂量/资格/统计方法、无治疗联系的基线资料及机制、作者/伦理/经费信息。无法解释的表图片段或缺少必要上下文记待判定，不能猜测。

定义版本：`solid-treatment-passage-screening-v1`。完整英文提示词及输入范围见`var/experiments/evidence-passages-20261023/definition.json`。

| PMID / 原文 | 标题 | 实体瘤与研究类型 | 段数 |
|---|---|---|---:|
| [42869886](https://pmc.ncbi.nlm.nih.gov/articles/PMC13661532/) | Camrelizumab plus chemotherapy as induction therapy followed by concurrent chemoradiotherapy and camrelizumab consolidation in limited-stage small cell lung cancer: a randomized phase II trial. | 局限期小细胞肺癌；随机II期临床试验 | 57 |
| [42873849](https://pmc.ncbi.nlm.nih.gov/articles/PMC13666023/) | Immune biomarkers of response and resistance to ipilimumab and nivolumab in metastatic castration-resistant prostate cancer. | 转移性去势抵抗性前列腺癌；免疫联合治疗剂量扩展临床队列 | 50 |
| [42870461](https://pmc.ncbi.nlm.nih.gov/articles/PMC13662409/) | Evaluation of early C-reactive protein kinetics as an early response marker in biliary tract cancer treated with combined chemoimmunotherapy: A multicenter observational study. | 胆道癌；多中心回顾性观察队列 | 69 |
| [42467226](https://pmc.ncbi.nlm.nih.gov/articles/PMC13495545/) | Targetable S100-RAGE Signaling Mediates Intrinsic Trastuzumab Deruxtecan Resistance in Metastatic Breast Cancer. | 转移性乳腺癌；临床样本关联与细胞/动物转化研究 | 67 |
| [42872145](https://pmc.ncbi.nlm.nih.gov/articles/PMC13661092/) | Adult Thalamic Diffuse Midline Glioma, H3 K27-Altered, With KRAS Comutation and Potential Early Subependymal Dissemination. | 成人丘脑弥漫中线胶质瘤；单病例报告及叙述性讨论 | 39 |

按治疗相关性与研究类型多样性目的性选样，未使用规则命中数或模型输出选样。所有已解析段落参与，包括未被规则命中的段落。

仅评估已有解析器的282个全文p段落；源文共10表、25图，尚未完整恢复表格关系和图像内容。17个嵌套段落可能与上级文本重叠，不能当作独立事实数量。
补充文件未打开；全文自身JATS摘要参与，PubMed元数据摘要不重复计分。段落数量不是独立证据数量，也不代表全篇全部信息已经覆盖。

## 比较方法

原规则版本：`treatment-passage-rules-v1`。同一段落在以下三类词中至少命中两类：

| 类别 | 匹配内容示例 |
|---|---|
| treatment | treatment/treated、therapy、drug、chemotherapy、immunotherapy、radiotherapy、radiation、regimen、inhibitor、placebo、adjuvant/neoadjuvant |
| outcome | survival、response、progression、recurrence、resistance、benefit、toxicity、adverse events、hazard ratio、PFS/OS/ORR/DFS/DOR |
| biomarker | biomarker、mutation、fusion、expression、EGFR/HER2/KRAS/BRAF/ALK、PD-L1、MSI-H、dMMR、TMB |

使用不区分大小写的英文单词边界正则；不需要三类全命中，也不要求两类位于同一句。否定/不确定及比较词只记录提示，不排除负面结果。没有自动判断研究设计、结果真实性或因果关系。

<details><summary>本轮原始正则（可复用，不是新增词表）</summary>

```json
{
  "treatment": "\\b(?:treat(?:ment|ments|ed|ing)|therap(?:y|ies|eutic)|drug(?:s)?|chemotherap(?:y|ies)|immunotherap(?:y|ies)|radiotherap(?:y|ies)|radiation|regimen(?:s)?|monotherapy|combination therap(?:y|ies)|inhibitor(?:s)?|checkpoint blockade|placebo|adjuvant|neoadjuvant)\\b",
  "outcome": "\\b(?:survival|response(?:s)?|respond(?:ed|ers?|ing)|remission|progression|recurrence|relapse(?:d)?|resistan(?:ce|t)|sensitiv(?:ity|e)|benefit(?:s|ed)?|efficacy|effective(?:ness)?|toxicit(?:y|ies)|adverse events?|outcomes?|hazard ratio|odds ratio|relative risk|confidence interval|PFS|OS|ORR|DFS|DOR)\\b",
  "biomarker": "\\b(?:biomarkers?|mutations?|mutants?|variants?|fusions?|amplification|overexpression|expression|wild[- ]type|EGFR|HER[- ]?2|ERBB2|KRAS|NRAS|BRAF|ALK|ROS1|RET|NTRK[123]?|BRCA[12]?|PD[- ]?L1|MSI[- ]?H|dMMR|TMB)\\b",
  "negation_or_uncertainty": "\\b(?:no|not|neither|without|failed|uncertain|unclear|inconclusive|non[- ]significant|insufficient)\\b",
  "comparison": "\\b(?:versus|vs\\.?|compared|comparison|control|placebo|however|whereas|although|but)\\b"
}
```

</details>

- **原规则**：同一段至少命中治疗、结局、生物标志物三类词中的两类。否定词不用于排除。
- **Qwen3.5-4B**：逐段判断中心段落；标题和前后段仅供上下文。
- **Qwen3.5-9B**：相同输入、提示词和FP16协议，作为容量对照。
- **规则 AND 4B**：规则先筛，命中的段落再由4B判断；未命中直接不选。组合速度为离线估算。
- **规则 OR 4B**：规则命中即保留，未命中再由4B判断以补漏。组合速度为离线估算。

4B与9B使用同一冻结定义、段落顺序、FP16、单GPU、batch=1和关闭thinking；每次先预热一次再逐段计时。最大输入6144 token，禁止截断；输出上限64 token。null、非法输出、截断或超长均为待判定。

## 其他模型资料与选择

增加9B是已有权重、同系列的容量对照，可避免额外下载。官方通用基准不能保证本次证据筛选一定更好；模型选择结论以本轮结果为限。

Qwen3-Reranker、MedCPT与BGE reranker面向查询—文档相关性排序；需要另行定义查询并用独立数据校准阈值，不能直接把检索相关性当作治疗证据。当前没有已验证的任务优势，故本轮不扩展测试。GLiNER既往项目三分类较弱，但这不证明它必然不适合本任务。

- [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B)：test。
- [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)：defer。Query-document relevance scores need independent threshold calibration; not present locally.
- [Qwen3-Reranker paper](https://arxiv.org/abs/2506.05176)：background。
- [GLiNER2.5-Decide](https://huggingface.co/fastino/GLiNER2.5-Decide)：skip。Available, but weak previous project baseline and no direct evidence-screening validation; does not prove failure on this task.
- [MedCPT](https://huggingface.co/ncbi/MedCPT-Cross-Encoder)：defer。Biomedical retrieval ranking; different target and calibration, 512-token official example truncation.
- [BGE-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3)：defer。Query-passage relevance ranking, no verified superiority for therapeutic evidence decisions.

## 计分口径与结果

初始Astra盲标在模型推理前冻结。本次候选分歧复核修订后保留旧参考与初评，全部方案统一重算；最终修订参考不声称完全盲法。282段明确参考用于主指标。正例引文按原文字符位置验证，未决单列。

5篇论文的282段全部参与评测；Astra明确参考282段，其中阳性122、阴性160；参考未决0段排除于主指标。

精确率=TP/(TP+FP)，召回率=TP/(TP+FN)，F1=2TP/(2TP+FP+FN)。参考null不进入主指标；模型null或失败在参考正例上计漏检，在参考负例上不冒充确定阴性。selected_all与pending_all覆盖全部282段。

| 方法 | Precision | Recall | F1 | TP / FN / FP | 入选 / 待定（全部） | 处理秒数 | 平均秒/段 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 原规则 | 61.8% | 77.0% | 68.6% | 94 / 28 / 58 | 152 / 0 | 0.087 | 0.0003 |
| Qwen3.5-4B | 78.2% | 76.2% | 77.2% | 93 / 29 / 26 | 119 / 0 | 267.188 | 0.9475 |
| Qwen3.5-9B | 80.6% | 95.1% | 87.2% | 116 / 6 / 28 | 144 / 0 | 286.096 | 1.0145 |
| 规则 AND 4B | 85.9% | 59.8% | 70.5% | 73 / 49 / 12 | 85 / 0 | 148.365（估算） | 0.5261（估算） |
| 规则 OR 4B | 61.3% | 93.4% | 74.0% | 114 / 8 / 72 | 186 / 0 | 118.998（估算） | 0.4220（估算） |

模型时间为逐段分词、传输、推理、解码与输出校验的累计时间，不含加载、预热、落盘。组合按规则扫描加被调用段落的既有4B延迟求和，未另行实测流水线；不能将估算写成部署吞吐。

| 模型 | 模型加载秒数（单列） | 峰值显存 GiB | 续跑计时 |
|---|---:|---:|---|
| Qwen3.5-4B | 157.164 | 10.87 | 否 |
| Qwen3.5-9B | 204.753 | 19.88 | 否 |

加载为冷启动模型实例；操作系统文件缓存未隔离，不能据此比较无缓存冷读取性能。显存为NVML进程峰值，0.1秒采样。

## 按论文结果

| PMID | 方法 | TP / FN / FP | Precision | Recall | 入选 / 待定 |
|---|---|---:|---:|---:|---:|
| 42869886 | 原规则 | 23 / 3 / 14 | 62.2% | 88.5% | 37 / 0 |
| 42869886 | Qwen3.5-4B | 22 / 4 / 5 | 81.5% | 84.6% | 27 / 0 |
| 42869886 | Qwen3.5-9B | 25 / 1 / 5 | 83.3% | 96.2% | 30 / 0 |
| 42869886 | 规则 AND 4B | 20 / 6 / 3 | 87.0% | 76.9% | 23 / 0 |
| 42869886 | 规则 OR 4B | 25 / 1 / 16 | 61.0% | 96.2% | 41 / 0 |
| 42873849 | 原规则 | 16 / 5 / 18 | 47.1% | 76.2% | 34 / 0 |
| 42873849 | Qwen3.5-4B | 17 / 4 / 6 | 73.9% | 81.0% | 23 / 0 |
| 42873849 | Qwen3.5-9B | 19 / 2 / 5 | 79.2% | 90.5% | 24 / 0 |
| 42873849 | 规则 AND 4B | 13 / 8 / 3 | 81.2% | 61.9% | 16 / 0 |
| 42873849 | 规则 OR 4B | 20 / 1 / 21 | 48.8% | 95.2% | 41 / 0 |
| 42870461 | 原规则 | 13 / 10 / 7 | 65.0% | 56.5% | 20 / 0 |
| 42870461 | Qwen3.5-4B | 18 / 5 / 8 | 69.2% | 78.3% | 26 / 0 |
| 42870461 | Qwen3.5-9B | 23 / 0 / 11 | 67.6% | 100.0% | 34 / 0 |
| 42870461 | 规则 AND 4B | 8 / 15 / 0 | 100.0% | 34.8% | 8 / 0 |
| 42870461 | 规则 OR 4B | 23 / 0 / 15 | 60.5% | 100.0% | 38 / 0 |
| 42467226 | 原规则 | 30 / 7 / 9 | 76.9% | 81.1% | 39 / 0 |
| 42467226 | Qwen3.5-4B | 27 / 10 / 4 | 87.1% | 73.0% | 31 / 0 |
| 42467226 | Qwen3.5-9B | 36 / 1 / 3 | 92.3% | 97.3% | 39 / 0 |
| 42467226 | 规则 AND 4B | 23 / 14 / 4 | 85.2% | 62.2% | 27 / 0 |
| 42467226 | 规则 OR 4B | 34 / 3 / 9 | 79.1% | 91.9% | 43 / 0 |
| 42872145 | 原规则 | 12 / 3 / 10 | 54.5% | 80.0% | 22 / 0 |
| 42872145 | Qwen3.5-4B | 9 / 6 / 3 | 75.0% | 60.0% | 12 / 0 |
| 42872145 | Qwen3.5-9B | 13 / 2 / 4 | 76.5% | 86.7% | 17 / 0 |
| 42872145 | 规则 AND 4B | 9 / 6 / 2 | 81.8% | 60.0% | 11 / 0 |
| 42872145 | 规则 OR 4B | 12 / 3 / 11 | 52.2% | 80.0% | 23 / 0 |

## 发现与决定

- 原规则未针对本轮错误改词：同段治疗、结局、生物标志物三类提示词至少出现两类即可入选；否定及比较词只保留标记，不决定筛选。
- AND和OR均复用同一完整4B输出；组合耗时按实际逐段延迟离线估算，并非重新实测整条流水线。
- 仅评估已有解析器的282个全文p段落；源文共10表、25图，尚未完整恢复表格关系和图像内容。17个嵌套段落可能与上级文本重叠，不能当作独立事实数量。
- Astra为模型参考，非人工金标准。目的性选择的5篇开发样本，不能据此推断全库性能或临床适用性。
- 本轮原文为肺癌、前列腺癌、胆道癌、乳腺癌和成人中线胶质瘤，覆盖临床、转化和病例；282/282段定位可精确重建。模型只判断是否相关，脚本取回整段，不声称已抽出核心句或知识关系。
- 初始Astra参考v1先于小模型冻结。预测后对7处边界复核，确认前列腺癌文中方法段括号内报告2例疾病控制，原参考漏读；追加v2，只改这一段。复核者未读模型输出，但复核选样来自预测分歧，因此v2不声称完全盲法。
- 所有方案按v2重算；v1→v2的TP/FN/FP分别为：规则93/28/59→94/28/58；4B 93/28/26→93/29/26；9B 115/6/29→116/6/28；AND 73/48/12→73/49/12；OR 113/8/73→114/8/72。v1参考与evaluation.json完整保留，未重跑模型。
- 9B比4B多找回23段，召回提升18.9个百分点，误收增加2段；平均0.947→1.015秒/段，峰值显存10.87→19.88 GiB。本轮同卡FP16、batch=1、关闭思考；这是一次实测，不是所有长度或硬件下的固定关系。
- AND提高精确率至85.9%，却漏49/122段，不适合作为优先保留证据的入口。OR漏8段，但误收72段；其估算速度约119秒/282段可供低显存方案参考，需承担更多下游核验。9B选144段，比OR少42段且多找回2个目标。
- 常见误收是把邻段结论搬入只有日期、图表标题或方法的中心段；常见漏检包括脚注不良反应、正文中引用的治疗发现、阴性关联。不能仅按短段、章节或是否含数字删选。
- 查阅官方模型与论文后，仅增加已存在的Qwen3.5-9B实测。Qwen3-Reranker-0.6B、MedCPT和BGE主要解决查询相关性，尚需本任务独立阈值校准；GLiNER保留历史基线，未见足够本任务增益依据，本轮未重复测试。没有新增模型下载。
- Astra模型参考仍可能有遗漏，单段由一个标注者完成，未估计标注者一致性。本轮5篇是开发集，9B是证据段落环节的后续优先候选，不能据此直接宣称生产可用或改动文献三分类选型。
- Qwen3.5-9B FP｜PMID 42869886｜body/p:10：中心仅为数据截止日期，却判为治疗证据；相邻段有结果，也不能替中心段补出结论。
- Qwen3.5-4B FN｜PMID 42869886｜body/p:29：短脚注明确报告3例反应性毛细血管增生/皮疹，其中2例瘙痒，属于实际不良反应；4B漏掉，9B保留。

## 下一步

- 保留文献三分类4B；治疗证据段落优先沿9B方向，在新论文上检查召回与误收。若部署显存受限，保留4B加规则OR作候选，但先核算额外核验成本。
- 下一次只做中心段与邻段边界、图表标题/脚注和二手结果的针对性改进；旧282段作回归，新增独立论文评估泛化，不用本批反复调分后宣称提升。
- 段落入口稳定后推进S3/S4：从被保留段落及必要上下文提取疾病、药物、标志物原文跨度并规范化；实体/关系/组别与数值另设指标。保持采集原限速，暂不微调或全库知识抽取。

## 来源与复用

公开记录ID：`treatment-passages-five-papers-20261023`；身份指纹：`260f0d79e5f0367d8d2b65be6b88060e599b0927bf88824904406fc282437183`。公开页面不包含整段原文。

完整原始引用、每段输出、计时与中间文件留在本机实验目录。本文只是冻结结果投影，未执行数据库写入、全库分类或正式知识发布。

来源文件如下；完整SHA-256保留在公开JSON的provenance中。

- 冻结输入：`var/experiments/evidence-passages-20261023/inputs.json`
- 选样与范围：`var/experiments/evidence-passages-20261023/selection.json`
- 任务定义：`var/experiments/evidence-passages-20261023/definition.json`
- 当前Astra参考：`var/experiments/evidence-passages-20261023/astra-reference-v2.json`
- 全量离线评测：`var/experiments/evidence-passages-20261023/evaluation-v2.json`
- 模型资料与筛选：`var/experiments/evidence-passages-20261023/model-research.json`
- 保留的初始Astra参考v1：`var/experiments/evidence-passages-20261023/astra-reference-v1.json`
- 保留的初评v1：`var/experiments/evidence-passages-20261023/evaluation.json`
- 候选分歧复核：`var/experiments/evidence-passages-20261023/reference-review.json`
- 原规则：`apps/curation/pilot_candidates.py`
- 推理脚本：`scripts/benchmark_passage_screening.py`
- 评分脚本：`scripts/evaluate_passage_screening.py`
- 发布投影脚本：`scripts/publish_passage_screening.py`
- PMID 42869886 原文版本与覆盖：`var/experiments/evidence-passages-20261023/documents/42869886.json`
- PMID 42873849 原文版本与覆盖：`var/experiments/evidence-passages-20261023/documents/42873849.json`
- PMID 42870461 原文版本与覆盖：`var/experiments/evidence-passages-20261023/documents/42870461.json`
- PMID 42467226 原文版本与覆盖：`var/experiments/evidence-passages-20261023/documents/42467226.json`
- PMID 42872145 原文版本与覆盖：`var/experiments/evidence-passages-20261023/documents/42872145.json`
- Qwen3.5-4B manifest：`var/experiments/evidence-passages-20261023/qwen35-4b/manifest.json`
- Qwen3.5-4B summary：`var/experiments/evidence-passages-20261023/qwen35-4b/summary.json`
- Qwen3.5-9B manifest：`var/experiments/evidence-passages-20261023/qwen35-9b/manifest.json`
- Qwen3.5-9B summary：`var/experiments/evidence-passages-20261023/qwen35-9b/summary.json`
