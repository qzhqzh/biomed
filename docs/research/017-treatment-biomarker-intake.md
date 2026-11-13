# 017 · 治疗方式与生物标志物收纳及关联

2026-10-30。本轮验证保存和查询能力，复用同5篇15组的016实体类型整理、014无概括9B关系输出及已冻结Astra参考。没有新增模型调用、概括、微调或tumor_api改动。

## 结构与用途

新增三张小表，不改现有临床证据发布链：

- `KnowledgeEntity`：来源内去重的本地候选词条，保存英文名称、kind/subtype、状态。未知缩写按文章版本隔离，不假定跨论文同名等价。
- `KnowledgeCandidate`：每个片段组一条研究候选，保存原始fact、既有参考核验和输入版本。记录为`research_candidate`，不写入正式`Evidence`，不排队推理。
- `KnowledgeAnchor`：词条提及、定义、关系引用原段及上下文。外键连接原`Passage → MaterialVersion → Material`，保存原文quote和Unicode字符起止；文章的PMID/DOI、原文件SHA、解析版本和段落位置均可追溯。

实体类型和用途独立：`H3 K27M-mutant`仍是variant，旧模型提出的biomarker用途以`biomarker_candidate`保留，明确未确认，不能自动生成DNA/HGVS或标志物效果关系。复杂标志物如`CRP kinetics`、`TREM2-associated myeloid gene signature`保存完整名称，不能压缩成CRP或TREM2单一实体。

关联只用三种简单依据：

1. `model_selected`：原关系输出明确引用这个entity ID。
2. `exact_fact_text`：原关系字段含区分大小写的原词，边界检查通过；这只是文字关联，不表示模型已经给了ID，也不推断因果、疗效或联合关系。
3. `context_only`：片段中存在该词条，但未建立主关系。ICI在K08属于这种情况，另有`definition`保存同篇明确全称。

具体药物和治疗类别共同保存。旧`fact.drug_names`完全保留，界面另按词条类型展示；它不是已经纠正好的纯药物字段。候选标准ID也独立保留为研究建议，不能因为本地ID创建成功就升级为标准库确认。

## 本轮结果与例子

15组共173个实体提及；其中172个有可用原文位置。按同一文章版本、类型、子型、原词精确去重为106个候选词条：治疗方式26、生物标志物26、药物18、疾病18、基因11、变异5、蛋白2。35个治疗方式提及全部保留，44个生物标志物提及中43个可定位，另1个原样保留为未决项。共441条定位，包含实体原词、原始输入段落和ICI同篇定义。

| 组 | 具体内容 | 保存方式 |
| --- | --- | --- |
| K08 | `ICI`及`immune checkpoint inhibition (ICI)` | 治疗类别候选；上下文提及与定义分别定位，不能替代`durvalumab`或自动生成疗效关系 |
| K08 | `durvalumab`、`CRP` | 原关系显式选择；保留各CRP分组DCR/ORR/PFS/OS未见显著差异的原文，不能改为正向预测结论 |
| K09 | `CRP`与`CRP kinetics` | 分别保存指标和动态特征，关联同条知识及原文 |
| K07 | `TREM2-associated myeloid gene signature` | 保存完整标志物特征，不能替换成TREM2基因 |
| K05/K06 | 带剂量的`ipilimumab … + nivolumab …` | 保留完整治疗方案原词，同时保存具体药物；原关系ID为空的关联标为事实字段原词匹配 |
| K15 | `H3 K27M-mutant` | variant＋未确认的biomarker用途候选；不额外生成主关系或标准变异ID |

本轮边界检查不采用21个嵌在更长词或解析粘连文本中的位置，包括`HER2`中的6处`ER`、`cCRT`中的`CRT`和表格解析成`camrelizumab2`的药名位置；保留其余可定位提及。这是保守定位筛选，不声称21处都是实体识别错误。英语复数后缀`s`允许保留模型抽出的原始单数切片，坐标和英文不改写。`TREM2+ TAMs`原本无法定位，保存于K07未决项，不创建假锚点，不阻断其他已定位内容。

**保存成功率与医学语义质量分开。** 沿用Astra参考的核心内容通过仍为12/15，没有宣称抽取准确率提高。K12细胞/动物范围、K14时间起点、K15结论限定的错误均原样保留。正式临床证据的AI发布规则不变，本轮候选未发布。

## 入口、脚本与验证

- [知识候选](http://192.168.124.2:50011/knowledge/candidates/)：按治疗方式、生物标志物等类型检索，点击详情后查看关联知识、英文原文和两层出处。
- [研究历程017](http://192.168.124.2:50011/management/research/?experiment=knowledge-intake-20261030)：15组研究记录可跳转实际入库ID。
- `scripts/prepare_knowledge_intake.py`：从冻结结果生成导入包；不运行模型，不写业务库。
- `manage.py import_knowledge_candidates --input PATH [--dry-run]`：先核对全部文章版本、段落、解析器和quote，再以短事务追加。相同批次复用，内容冲突拒绝覆盖。
- `scripts/publish_knowledge_intake.py`：要求实际导入回读验证成功才登记研究记录。
- `var/experiments/knowledge-intake-20261030/`：导入包、原文检查、实际导入回执及验证结果；实际位于`/appdata/02_resource/apps/biomed/research/artifacts/knowledge-intake-20261030/`。

专项测试覆盖跨文章/版本、伪引文、隔离来源、解析器变化、幂等及同版本冲突、类型与角色分离、分页查询；回读须逐条核对15组fact、441条原文位置和来源外键。页面验证包括ICI→K08、CRP kinetics、研究记录链接和手机布局。实际执行结果以`verification.json`与`browser-validation.json`为准。

实际数据库回读已通过：15组fact与Astra参考不变，441条英文切片均与原段一致，重复导入复用原ID；原正式Evidence/Review仍为0。后端13项专项测试、准备脚本4项测试、限定迁移3项隔离验证及前端路由测试通过，前端生产构建成功。迁移只新增三表，旧schema比对未变；本机大SQLite迁移注意事项见[部署记录](../DEPLOY.md)。

后续先看这些候选是否便于使用，再决定哪些词条需要跨文章统一。未知标准词条不阻断候选保存，仍需解决的医学关系错误独立处理；不增加概括或一串新的模型步骤。
