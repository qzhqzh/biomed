# 文献抽取试验样本（2026-10-22）

原文献库按 `created_at, id` 固定的前20篇保持不变，不因相关性、全文可用性或模型表现替换样本。另从已有解析全文中定向补充10篇实体瘤文献，与原20篇的 `material_id` 无重复；10篇均有有效来源的 JATS 全文及已解析段落，合计703段，本轮选样没有联网下载。

补充集合包含9篇非病例研究和1篇单病例报告，实际覆盖10类癌种：肺、前列腺、胆道、结直肠、膀胱、鼻咽、乳腺、胃、甲状腺及胶质瘤。此前选样沟通中的“9癌种”是计数错误；样本内容未因此调整。

这是**目的性开发集，不是随机样本、人工金标准或独立保留集**。选样依据为已保存摘要的逐篇核读及来源状态，目的是检查分类、证据识别、实体与条件组装的具体失真。下表描述论文所研究的内容和需要检验的抽取行为，不将作者推断、候选标志物或模型输出认定为临床事实。

| PMID / 真实标题 | 癌种 | 研究类型 | 用它检验什么 | 关键局限 |
| --- | --- | --- | --- | --- |
| **42869886** — Camrelizumab plus chemotherapy as induction therapy followed by concurrent chemoradiotherapy and camrelizumab consolidation in limited-stage small cell lung cancer: a randomized phase II trial. | 局限期小细胞肺癌 | 人体随机II期治疗试验 | 将两组方案、诱导/放化疗/巩固阶段、ORR/PFS/OS和标志物阈值正确关联。 | 41例；不能把多阶段方案效果归为单药效果，探索性标志物不能自动成为用药条件。 |
| **42873849** — Immune biomarkers of response and resistance to ipilimumab and nivolumab in metastatic castration-resistant prostate cancer. | 转移性去势抵抗性前列腺癌 | 临床试验扩展队列及免疫标志物分析 | 区分三种联合用药剂量队列、不同分母的6个月疾病控制率，以及CD8/TREM2关联。 | 32例小队列；不能声称随机组间优越性，也不能把基因表达特征转为单基因突变触发。 |
| **42870461** — Evaluation of early C-reactive protein kinetics as an early response marker in biliary tract cancer treated with combined chemoimmunotherapy: A multicenter observational study. | 胆道癌 | 多中心回顾性预后标志物研究 | 保留四组CRP动态、6周/3个月窗口及各组PFS、OS、置信区间和ORR。 | 需前瞻外部验证；未提供的药名和样本量保持未知，预后关联不等于药物敏感或耐药。 |
| **42272324** — Predictive Biomarkers in Metastatic Colorectal Cancer Patients Treated With Bevacizumab: A Turkish Oncology Group (TOG) Study. | 不可切除转移性结直肠癌 | 前瞻性治疗队列与候选标志物研究 | 区分VEGF/PDGF-B/ET-1组合指数、药物谷浓度、Treg及各自测量时点和结局。 | 88例治疗队列的生存关联不能独立证明治疗预测效应；文中AI指Angiogenesis Index。 |
| **42050770** — Indirect Comparison of Neoadjuvant Treatment Strategies for Muscle-Invasive Bladder Cancer: ddMVAC and Perioperative Durvalumab-Gemcitabine-Cisplatin Versus Gemcitabine-Cisplatin: A Systematic Review and Network Meta-Analysis of Randomised Controlled Trials. | 可用顺铂的肌层浸润性膀胱癌 | 系统综述与网络Meta分析 | 保留方案比较方向、HR/OR及终点，区分试验结果和假设模拟阈值。 | 仅三项RCT且有间接比较；EV-P模拟HR不能写成已观察到的临床疗效。 |
| **42860552** — Development and validation of a metabolic syndrome-based nomogram to predict 1-year metastasis after radiotherapy in locally advanced nasopharyngeal carcinoma. | 局部晚期鼻咽癌 | 单中心回顾性预后模型开发与内部验证 | 关联训练/验证队列、1年转移、MetS/EBV DNA、C-index和探索性阈值。 | 1333例的同中心拆分不是外部验证；风险相关性不等于因果或治疗获益。 |
| **42467226** — Targetable S100-RAGE Signaling Mediates Intrinsic Trastuzumab Deruxtecan Resistance in Metastatic Breast Cancer. | 转移性乳腺癌 | 人体组织关联加细胞、动物机制验证 | 区分人体病灶关联与TTP488联合T-DXd的前临床实验，保留耐药方向和转移部位。 | 109是病灶数，不能写成患者数；联合药物实验不等于人体已验证疗效。 |
| **42871965** — PTTG1 drives intestinal-type gastric cancer progression via G2/M checkpoint-mediated cell-cycle reprogramming. | 肠型胃癌 | 人体组织发现加类器官、细胞和异种移植研究 | 保留PTTG1敲低/过表达方向及实验系统，区分正常、肠化生和癌组织。 | 初始组织来自五名患者；候选靶点不等于临床药物或已验证检测。 |
| **42068240** — Combination of Lenvatinib and Antibiotics: A Potential Approach to Enhance Sensitivity in Differentiated Thyroid Cancer. | 分化型甲状腺癌 | 细胞与三维球体体外联合药物研究 | 将lenvatinib分别联合tigecycline、eravacycline的两种方案及2D/3D实验正确拆开。 | 仅体外模型且存在药理局限；不能推断人体获益，摘要中的批准背景不能替代监管来源核验。 |
| **42872145** — Adult Thalamic Diffuse Midline Glioma, H3 K27-Altered, With KRAS Comutation and Potential Early Subependymal Dissemination. | 成人丘脑弥漫中线胶质瘤 | 公开发表的单病例及叙述性讨论，单列分析 | 区分H3F3A/KRAS/PHF6、多突变共现、实际放疗、未用temozolomide及背景药物讨论。 | 单病例不支持一般疗效结论；ONC201不是本病例实际用药，疑似播散不能改写成确诊。 |

选样时排除了保存元数据中明确标记的撤稿、勘误、关注声明，以及隔离来源。所选记录的发表状态均为保存时的 `unmarked`，仅表示快照未见标记，没有回查当前在线状态。全文已有段落也不代表表格、图像和所有阅读顺序均已恢复。

原20篇与补充10篇应分别报告结果；病例、前临床实验和人体研究保留各自层级。记录格式失败、漏检、无依据结论、条件配对错误及拒判，不只展示成功条目。此前评测暴露由冻结导出记录继续核对，不能因不在原20篇中就称为未见样本。

文件位置：

- 原20篇固定快照：[var/experiments/evidence-pilot-20261022-first20/snapshot.json](../var/experiments/evidence-pilot-20261022-first20/snapshot.json)。
- 定向选样文件：[var/experiments/solid-tumor-supplement-selection.json](../var/experiments/solid-tumor-supplement-selection.json)，保存完整标题、material ID、PMID、版本ID、摘要哈希及选样理由。
- 补充实验报告输出位置：[var/experiments/evidence-pilot-20261022-solid10/report.md](../var/experiments/evidence-pilot-20261022-solid10/report.md)。该报告由后续运行生成；本文只记录样本选择，不预先声明抽取结果或质量。

本轮仅在独立的 biomed 实验中使用这些资料，不修改或接入其他生产项目。
