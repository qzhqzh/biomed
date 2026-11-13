# biomed 产品架构与数据契约

2026-11-06用户确定：本地已确认药物缺少目标实体时，由导出流程自动查重、新增并回填目标ID，不要求人工逐条批准；真实身份冲突或缺乏依据另行保留。当前已实现自动新增指令及隔离目标库执行验证，生产连接尚未接入。详见[目标药物自动新增](tumor-api-export.md#已确认药物自动新增2026-11-06)。

同日接入[通用实体归一](entity-normalization.md)：任意知识批次先复用biomed规则与标准字典，陌生药物／疾病名再调用现有9B，结果追加为本地实体和有作用范围的规则，随后进入目标适配。完整批次保存时先写规则再写知识，后续批次读取更新后的本地库；不按某批35个名称编写特例，也不重新拉取tumor_api。原始知识和独立质量记录不覆盖。

2026-10-12。按用户确认的“真实标准库 + 临床证据 + 文本处理工作流”实施；AI 通过允许发布，人工抽查。英文论文 PDF 为首批输入；具体模型尚未确定。

2026-10-22 补充[文献知识预提取试验](evidence-extraction-pilot.md)：固定前 20 篇并补充 10 篇实体瘤，已完成规则定位及小模型与 Astra 参考的开发集分类对照，尚未训练或生产接入。biomed 独立实施，tumor 生产项目只作只读结构参考。方法、分母、逐篇依据与失败结论保存到[研究历程](research-history.md)；用户确定下一轮只做实体瘤、血液肿瘤、非肿瘤三分类，旧九分类结果保留为历史记录。

2026-11-01用户调整：**文献知识提取、目标格式适配与语义核验解耦。** 先产出并保存biomed知识及tumor_api兼容包，语义核验独立绑定知识版本、原文版本和标准版本，逐条记录有效/无效/待判定，不作为提取主流程的固定调用或完成前提。按目标知识类型所需实体、关系和必需字段评价，允许省略目标外信息；缺目标ID记适配缺口，错实体/错关系记质量问题，遗漏其他知识记覆盖问题。当前为后续实施方案，018既有发布逻辑与历史记录未改；下文已上线AI发布机制描述运行现状，不能再据此要求新提取包先核验才能形成。完整例子与约定见[提取与核验契约](evidence-extraction-pilot.md#extraction-and-evaluation-contract)。

## 模块与边界

| 模块 | 职责 | 当前实现 |
| --- | --- | --- |
| React / Vite | 标准实体、资料版本、证据审阅和任务状态 | 唯一浏览器入口 50011，Django 根据 manifest 加载一个版本的入口模块 |
| Django / DRF | 持久领域模型、校验、权限、API、既有分析链路 | `apps/curation` 为新知识领域；`apps/annotations` 保留已有生信能力 |
| annotation worker | 既有变异注释任务 | 同一代码/镜像、独立进程认领 AnnotationJob，恢复中断任务；一次执行固定同一知识库及检索索引快照 |
| curation worker | 获取、解析、抽取、AI 审核 | 同一代码/镜像、独立进程，无公开端口；队列写入数据库 |
| 标准数据 | 来源版本、ID、名称及关系 | NCBI Gene / MANE 沿用已有不可变文件快照；MONDO / RxNorm 使用版本化关系表 |
| 原始资料 | 不可覆盖文件与来源授权 | `var/library/blobs/<sha prefix>/<sha256>`，数据库只保存摘要和版本关系 |
| 内部教学模块 | 已验证的虚构病例流程及问题记录 | FastAPI 在私有网络运行，Django 同源代理；不发布 18765，不再提供 HTML 前端 |

目前一个 Django 业务数据库即可保持资料、任务和审核状态的一致性。SQLite 使用 `IMMEDIATE` 写事务和 30 秒等待，两类 worker 各限定为单实例；大来源分批暂存，校验完整后原子切换。扩大并行写入或机构多人使用时应迁移 PostgreSQL，并对任务认领与吞吐重新验收，不能直接增加 worker 副本。

## 页面归属与处理记录

知识库按基因、疾病、药物组织；转录本属于基因详情。证据库负责临床证据与文本资料来源（文献、指南）；工作流归入管理。新增语义路由 `/knowledge/diseases/`、`/knowledge/drugs/`、`/evidence/clinical/`、`/evidence/sources/`（含 papers/、guidelines/ 子路由）、`/management/workflows/`；原 `/knowledge/standards/`、`/knowledge/evidence/`、`/library/`、`/workflows/` 继续渲染相同组件，保留原查询参数使用方式。

`versions/{id}/processing/` 投影此原文件版本的 parse / extract 任务，独立计算最新阶段状态，历史分页按创建时间与主键稳定倒序。空记录返回 null 阶段，不生成持久任务。具体任务通过既有 workflow_detail 读取每次尝试；本轮没有迁移数据、修改持久模型或引入新的任务存储。指南计划是待确认的产品设计，不是已实现的知识结构配置器或章节解析器。

## 标准实体

2026-11-01新增独立的本地实体归一层（`0012_entity_registry`），不替换标准资源、文章原词或原知识。`CanonicalEntity`维护业务实体和主癌种／亚型，`EntityResolutionRule`维护带类型及全局／文章范围的可复用名称规则，`EntityRegistryMention`关联已有知识的实际参与原词，`EntityRegistryDecision`追加保存分类和审核历史，`EntityRegistryImport`绑定不可变导入批次。首次基础快照导入后封存；服务运行和后续文献扩展没有tumor_api连接或刷新步骤。

实体身份、业务层级、分子／部位／分期条件分别保存。“脑胶质瘤→弥漫性中线胶质瘤”采用用户明确的业务层级；未知层级保持未定。“HER2阳性转移性乳腺癌→乳腺癌＋原文条件”属于`qualified_form`，不能变成不带条件的完全同义别名。规则／字典先处理，模型仅处理剩余歧义；独立参考通过的本批模型规则可记为AI依据确认，人工抽查独立。不确定规则不自动合并，确认或撤销仅重算派生关联，不改原fact和定位。写入复用会话、CSRF及`curation.change_evidence`权限，版本冲突拒绝覆盖。页面为`/knowledge/entities/`，只加载当前页和展开的详情；边界与实际对照见[023](research/023-entity-resolution.md)。

2026-11-01用户明确本环节验收口径：**疾病对齐的主要正确性以主癌种是否对上为准**。例如转移性去势抵抗性前列腺癌，只要主类对上前列腺癌就算本项正确；亚型、转移、耐药／治疗状态等另记信息完整度及核对结果，不因重复亚型ID缺失、`MATCH/NEW`标签或对应方式不同而否决正确主类。缺少某种条件组合不意味着必须新增癌种。模型直接给出的主类、脚本可恢复的主类和仅在解释中识别到的主类须分开报告，不能把脚本补全计作模型直接输出。药物身份单独评价，本次没有放宽不同药物／异构体的区分。原文与适用条件继续保留；本次新口径复评不覆盖旧实验或自动改变已入库知识和审核状态。

- `SourceRelease`：命名空间、官方版本、来源地址、使用权利、文件 SHA-256、预期记录数及当前版本标记。
- `Concept`：以 `(release, identifier)` 唯一，保存规范名称、同义词、父级、交叉引用和定义。
- 部分下载/导入失败不切换当前版本。重试按内容校验值与唯一标识补齐暂存批次；完整旧版本仍然保留。
- MONDO 的 `xref` 作为交叉引用原样保存，不把所有交叉引用自动提升成等价映射。RxNorm 当前仅导入 `IN`（ingredient），不混入需额外许可的完整 UMLS 数据，也不声明适应症。
- 2026-10-24新增独立`ONCOTREE`固定版癌种分类，复用SourceRelease/Concept与不可变原文件，无schema迁移。MONDO及OncoTree可按直接父子关系分页浏览，查询固定release，保留多父级及外部上级；分类图按release缓存，不向浏览器发送整个本体。新增分类资源不自动扩展正式证据实体审核允许范围。资源覆盖、许可和药物关系扩展见[标准资源](standard-resources.md)。
- 2026-10-25通过增量migration `0009_reference_resources`新增五表：`ResourceDataset`固定来源快照、版本与许可；`ResourceEntity`保存类别/分子/靶点；`ResourceAssertion`保存直接类别关系和带条件的机制；`DrugMoleculeLink`保存RxNorm与ChEMBL名称匹配候选；`EntityTranslation`保存带语言、来源和reference/project_draft状态的显示别名。原Concept与文献表不替换，类别及机制不写入Concept.parents，也不自动生成正式Evidence。完成manifest校验后分批暂存并切换dataset，失败不启用部分下载，旧版本保留。药物资源由`/api/v1/curation/drug-resources/`按成分读取，标准实体响应增加translations并支持中文检索；中文未覆盖项保持英文。
- 基因查询兼容 NCBI Gene ID、名称和同义词。原MANE索引仍以accession base建立联系，查询`NM_005228.999`只能解析到基因，显示“版本未核验”；其原接口保持兼容。2026-10-28新增独立转录本结构库，以完整`accession.version`查询，未收录的精确版本不回退到其他版本，见下节。

官方来源：[MONDO](https://obofoundry.org/ontology/mondo.html)、[RxNorm IN API](https://lhncbc.nlm.nih.gov/RxNav/APIs/api-RxNorm.getAllConceptsByTTY.html)、[RxNav 使用条款](https://lhncbc.nlm.nih.gov/RxNav/TermsofService.html)。

## 转录本结构与外显子总数（2026-10-28）

采用“一份完整版本转录本的核验总数 + 多条按组装、位点、来源保存的映射记录”，不设`exon_count_hg19`和`exon_count_hg38`两个含义不清的正式数字。独立SQLite参考快照不写文献采集数据库；每次本地导入完整校验后原子切换`current.json`，旧版保留。GenBank原件按SHA-256去重保存，版本中的硬链接固定来源。

| 表/字段 | 含义 |
| --- | --- |
| `transcripts.accession` | 完整RefSeq转录本版本，如`NM_000348.4`；另保留base编号供未指定版本检索 |
| `transcripts.exon_count` | 该版本转录本的全部外显子数（含UTR外显子）；未核验为null，不用0或原始映射块数填充 |
| `exon_status / exon_source_id / exons` | 来源核验状态、原始记录引用及转录本1-based闭区间 |
| `mappings` | 多条GRCh37/GRCh38记录，保留原始`raw_exon_count`、染色体、方向、0-based半开区间、全部位点及来源 |
| `aliases` | 原始MANE summary中的完整ENST版本与RefSeq版本对应，独立于旧的无版本索引 |
| `sources` | 来源URL、SHA-256、来源版本、获取/观察时间、时间依据和原件位置；未知版本或获取时间不推测 |

只有完整人类RefSeq RNA记录的`exon`特征通过版本、物种、区间及序列长度校验，才填正式总数。模糊位置、缺失或覆盖不完整保留待核验；截断、响应身份错误或同版本互相矛盾的记录阻止发布。不得将NCBI Gene的基因级外显子数、CDS片段数或基因组对齐块数混用。SRD5A2 `NM_000348.4`正式总数为5，GRCh37原始6块、GRCh38原始5块同时保留，差异不改写原参考文件。

查询接口`/api/v1/knowledge/transcript-structures/?q=NM_000348.4`支持精确版本、无版本候选、基因符号/GeneID和MANE ENST映射。每页默认20、最多100；仅读取本页结构及来源，不加载全库到前端。基因页提供正式总数、两组装映射块数和显式详情按钮，来源核验与临床审核无关。

批量获取与导入使用脚本，模型仅用于必要的异常解释，不逐条浏览网页；运行方式与当前覆盖见[标准资源](standard-resources.md#transcript-structures)。

## 文献知识候选的增量结构（2026-10-30）

`0010_knowledge_candidates`仅新增`KnowledgeEntity`、`KnowledgeCandidate`、`KnowledgeAnchor`。本地候选词条按文章版本、类型、子型、英文原词精确去重；不把未知缩写跨文献自动合并。治疗方式和生物标志物进入可查询的词条结构，标志物用途保存在关联角色中，可与variant/gene/protein身份并存。候选关系保留旧模型fact与独立参考状态，原文定位外键复用Passage/MaterialVersion/Material，不制造另一份资料来源。

受控导入先验证整包的文章身份、原文件SHA、解析器版本及原句切片，才进入短写事务；同批次同内容复用，冲突必须使用新版本。仅创建研究候选，不写Evidence/Review或排队模型任务，不改变AI通过即可发布的正式策略。GET接口`knowledge-entities/`、`knowledge-candidates/`提供分页查询和详情，页面为`/knowledge/candidates/`；研究历程继续读取静态记录，通过具体入库ID跳转，不在实验列表加载业务库。

本轮范围和实际例子见[017](research/017-treatment-biomarker-intake.md)。候选数据随既有Django数据库备份，无新增服务、模型或数据库引擎。

018新增`KnowledgeReview`不可变审核、`KnowledgeSearchTerm`已批准关系索引，候选增加独立AI/人工/发布状态。审核绑定原fact、实体、定位及同版本原文SHA，不继承旧assessment。AI通过且来源有效即可发布；人工flag或来源隔离撤回，重放不复活。词条按同一候选的当前审核精确匹配，各条件AND，不使用全文共现替代关系。未知标准ID保留来源内词条；既往、当前、拟议治疗由审核context和原fact区分。GET `knowledge/`及详情，POST `knowledge/<uuid>/review/`复用session/CSRF和`curation.change_evidence`权限；AI仅由受控命令导入。原Evidence/Review保持兼容。实际15组11条发布见[018](research/018-knowledge-publication.md)。

## 文本资料与原文定位

`Material` 表示一份逻辑资料，kind 为 paper / guideline / other。`MaterialVersion` 表示取得的一份具体文件，保存摘要、media type、获取时间、官方来源地址和权利声明。上传相同文件不会重复创建资料；新 PDF 可归入已有资料成为新版本。原文件不覆盖，保留上传文件的原始名称；磁盘存储仍以 SHA-256 寻址。

资料列表按 paper / guideline / other 查询，分类计数在类型筛选和分页之前计算。PDF 上传兼容原有立即解析行为，也接受 `start_parse=false` 仅归档；此模式不创建任何任务，不生成定位块或候选。前端指南当前仅归档、等待知识结构定义；其他资料默认仅归档，文献默认启动解析，后两者可调整。重复归档不取消既有任务或覆盖原版本。详情返回选定版本的 `parse_job`，区分“尚未启动”和“执行失败”，不能把未解析解释为无可提取文本。

`Passage` 按 `(version, parser_version, ordinal)` 唯一，包含原文文本和定位符。PDF 使用 `page:1` 等物理页码；JATS 使用 `body/p:1` 等稳定段落路径。不能将 PubMed 元数据或摘要装作整篇全文。

当前解析器为固定版 pypdf + JATS XML 解析。它只做文本恢复，不负责理解图形、表格、双栏阅读顺序或扫描 OCR；没有可用文本时任务明确待处理，原文件保留。模型看到的是已提取文本块，不能推断遗漏部分。资料类型不会改变审核边界，指南同样按版本与原文定位处理；获取指南需要合法来源，不自动抓取付费正文。

PubMed 由 E-Utilities 获取；PMC 只通过 [OAI-PMH 官方全文接口](https://pmc.ncbi.nlm.nih.gov/tools/oai/) 自动获取可复用内容，逐篇保存 permissions/license。没有相应全文或授权信息时保留元数据，提示补充合法 PDF。自动获取仅接收 PMID/服务端验证的 PMCID，不接受任意下载 URL。网络请求串行并控制在无需 NCBI API key 的速率以内。

来源身份在建立关联前校验：PubMed 只读取论文自身 `PubmedData/ArticleIdList`，不读取参考文献的 ArticleId；PMC 只读取 `front/article-meta/article-id`，核对 PMCID，并核对已知 PMID 和 DOI。缺失必要标识或出现冲突时阻断任务，不用标题相似度替代身份确认。

`MaterialVersion.integrity_status` 区分 valid / quarantined，隔离原因与时间保存在 `integrity_note`。发现来源错误时使用统一隔离操作：保留原始文件、定位块和审核历史，阻断其抽取/审核任务，撤回关联的已发布证据。模型调用前及发布事务内再次检查有效性，默认查询与资料详情排除隔离版本；用户仍可主动查看带警示的历史版本。重新获取正确全文会产生有效的新版本，不覆盖旧原文。

当前“自动”是导入后自动推进下游步骤；尚未启用定时文献检索订阅，不能把手动触发更新描述为已经持续监控新文献。

## 工作流

- `WorkItem` 保存输入、幂等键、当前状态、租约、结果和时间；`WorkAttempt` 保存每次执行的起止和结果。
- 输入内容和步骤类型的规范 JSON 产生幂等键；重复点击同一资料导入不会重复排队。标准库更新以来源和检查日期去重。
- worker 原子认领任务，使用有期限的 token；只有当前 token 可以完成状态写回。10 秒心跳、90 秒租约；中断后自动恢复排队。空闲心跳和任务认领遇到 SQLite 临时锁时退避重试，失败期间不更新健康时间；非锁类数据库错误仍报错。
- 每项任务在独立子进程内执行，单次最多 10 分钟，worker 容器限制内存和 CPU。临时网络失败最多自动尝试三次并退避；任务记录可人工重试，尝试历史不清空。
- 原始文件、资料版本、解析块和候选分别有持久去重键；抽取结果与后续审核任务在同一事务写入。抽取检查点已经保存时，恢复不能再次调用模型。
- 状态明确区分 queued、running、succeeded、failed、blocked、waiting_configuration。没有模型不是成功抽取，也不是数据丢失。

## 证据发布与人工抽查

`Evidence` 保存候选陈述、population / study_type / limitations 语境、精确引句和字符起止、关联标准实体的来源版本，以及生成它的抽取任务。空白事实保持 null，不自动补临床适用条件。

发布前由服务端核对：引句精确匹配该原文块；实体命名空间/ID 可以在标准库中确认；模型和提示词版本齐全；AI 审核返回明确通过/不通过及理由。浏览器没有“设置 AI 通过”的接口。

| 维度 | 状态 | 意义 |
| --- | --- | --- |
| AI 审核 | pending / pass / fail | 该版本模型和审核流程的结果 |
| 人工抽查 | unchecked / confirmed / flagged | 是否经具名用户核验及是否发现问题 |
| 发布 | draft / published / withdrawn | 是否进入默认正式证据查询 |

AI pass 可以将候选发布，无需等待人工批准。人工 confirm 记录核验；flag 必须提供理由，并撤回。撤回不删除原文、证据、AI 审核或历史人工记录，重试不能重新发布被撤回候选。数据库约束禁止“发布但 AI 未通过”及“人工发现问题仍发布”。新的 AI 复查不通过也会撤回已发布项。

`Review` 独立保存每次审核的种类、决定、理由、用户/任务和模型规则版本。审核用户只拥有 `curation.change_evidence` 权限，无管理员权限。多用户身份由同一 Django 账号体系扩展，不使用可自行填写的昵称冒充审阅者。

原Evidence自动抽取工作流的真实模型尚未配置，仍保留waiting_configuration，不生成模拟结果。018另对现有15组KnowledgeCandidate执行真实Astra核验并发布11条文献知识；这不等于配置了全库自动抽取，也未向原Evidence表写入测试数据。

## 模型适配器契约

配置项 `CURATION_MODEL_BACKEND` 指向可导入的 Python 类，默认为空。暂未绑定外部厂商或本地模型，也没有自动上传原文。

适配器需实现：

```python
class Backend:
    def describe(self) -> dict:
        # 全部为非空字符串；变更提示词时必须变更相应版本。
        return {"provider": "...", "model": "...",
                "extraction_prompt_version": "...", "review_prompt_version": "..."}

    def extract(self, passages: list[dict]) -> list[dict]:
        # passages: id, locator, text；返回最多 200 条候选。
        # candidate: passage_id, start, end, quote, assertion,
        # context: {population, study_type, limitations}（未知为 null）,
        # entities: [{namespace: NCBIGene|MONDO|RXNORM, identifier: 官方 ID}]
        ...

    def review(self, candidate: dict) -> dict:
        # 输入独立包含原文、引句、陈述、语境及已解析的标准实体。
        return {"decision": "pass", "reason": "可检查的审核理由"}
```

输入文献属于不可信资料；适配器必须将原文当成数据，不能执行原文中的指令、工具调用或脚本。启用适配器前确认资料发送边界、模型成本/时限、提示词版本及人工核对集；验证抽取覆盖率和误发布率。模型“抽取”与“审核”分别调用，不能用抽取函数的成功响应直接冒充 AI 审核。

当前界面支持资料解析和未完成任务重试；已完成抽取的再处理/跨模型比较尚未提供产品入口，后续应创建新的执行与证据记录，不能重写旧模型版本或已发布内容。

## API 与兼容

正式领域统一前缀 `/api/v1/curation/`：overview、sources、concepts、materials、versions/{id}/download、pubmed/search|import、workflows、evidence、session。GET 查询只读；浏览器写操作校验同源 CSRF；人工审核额外校验账号权限。

原 `/api/v1/knowledge/*`、注释、任务接口保留。`/api/v1/evidence/*` 是教学模块兼容接口，不等同正式证据库；教学 UI 位于 `/tools/teaching/`。原问题数据库完整保留，使用 `/knowledge/issues/` 查询、更新和导出。

<a id="solid-tumor-roadmap"></a>

## 实体瘤文献下一阶段讨论稿（2026-10-15）

**已确定的优先级：全部实体瘤，先采集近五年文献并分类，保留历史回填。** 首轮窗口为 2021-10-15 至 2026-10-15；先估算规模，再跑有限真实批次评估速度、流量与磁盘增长。后续模型与数据库选择仍是讨论方案；研究与官方文档核对日期为 2026-10-15。

### 起点与当前缺口

- 复用 `Material / MaterialVersion / Passage`、文件摘要寻址、来源身份校验和 `WorkItem / WorkAttempt`，不另建第二套文献库或执行状态。
- 既有 PubMed 搜索保留前 10 条预览及单 PMID 导入。新增 `LiteratureCollection / CollectionPartition / CollectionEntry / CollectionFetch` 复用同一工作队列，持久保存预算、日期分片、完整小分片 PMID 清单和获取账本；先估算再采集，支持暂停和续跑。定时增量订阅尚未接入。
- 当前全文获取使用 PMC OAI-PMH，PDF 上传与 JATS 文本解析已可用；尚未自动下载 PDF 附件、保留表格单元格结构或执行 OCR。
- `LiteratureClassification` 固定元数据版本与词表 SHA-256，保存多标签命中出处及未评审状态。`Evidence` 当前仍是有引句和语境的候选陈述，还没有独立的研究、试验组、疗效终点模型。真实模型未配置，采集分类不会触发模型训练或证据抽取。

### 全部实体瘤的采集与分类范围

建立有版本的癌种分类体系，覆盖器官部位、组织学类型及泛癌种研究，包括肉瘤、黑色素瘤、中枢神经系统肿瘤、神经内分泌肿瘤、生殖细胞肿瘤、间皮瘤、儿童实体瘤和原发灶不明等，不能只包含常见癌种。实体名称连接 MONDO 等已有标准来源，内部分类标签与标准实体标识分别保存。

首期建议使用分层、多标签分类：

| 分类轴 | 需要记录的内容 |
| --- | --- |
| 范围相关性 | 实体瘤相关、范围外、混合研究、信息不足；明确血液肿瘤和良性研究的边界 |
| 肿瘤类型 | 多个器官部位、组织学类型、泛癌种、原发灶不明；允许未能细分 |
| 研究对象 | 人体、动物、细胞/类器官、混合、未知 |
| 研究类型 | 随机试验、其他干预研究、观察性研究、系统综述/Meta 分析、其他综述、病例、基础研究、方案/其他 |
| 研究主题 | 治疗、生物标志物、诊断、预后、机制、流行病学等，可多选 |
| 记录状态 | 同行评议/预印本/未知，更正与撤稿状态，摘要/全文可用性 |

标题、摘要、MeSH、出版类型用于首轮筛选；MeSH 和规则提供的是弱标签，不能当作无误的训练答案。混合队列、罕见癌种和低置信结果进入待判定集合，避免通过一条排除血液肿瘤的检索式误删混合研究。英文为首批处理语言；其他语言保留可检索元数据及语言标签。

2026-10-16 将上述分类轴进一步拆成八个可独立核对的环节，加入研究对象 / 背景提及、原发 / 转移关系和后续处理分流，见[多环节分类设计](literature-classification-pipeline.md)。随后按授权并行续采到 5,000 条元数据，并对 20 篇开发材料完成 Qwen3-1.7B 两版提示词的离线试跑。原文校验、逐篇恢复和实验结果追加已实现，但试跑质量未达到规则基线；默认仍为 v3，训练未启动。最新数字、实测及未完成环节见[模型试跑记录](literature-model-pilot.md)。

### 文献获取设计

1. **发现与断点**：保存 `CollectionPlan` 的检索式、癌种词表版本、日期窗口、来源和预算；每次 `CollectionRun` 固定结果清单与获取时间。按日期和主题分片，支持分页、去重、暂停、恢复及失败重试；不把前 10 条或前 10,000 条当成全部结果。PubMed ESearch 的 10,000 条限制需采用可验证的日期分片或 EDirect 批处理处理，单纯增加 retstart 无法绕过；保存 PMID 清单，续跑不只依赖临时 History 状态。[NCBI E-Utilities 参数说明](https://www.ncbi.nlm.nih.gov/books/NBK25499/)
2. **来源分工**：PubMed 负责发现和元数据；先复用 PMC OAI-PMH，再按实际吞吐需要接入当前 PMC Cloud Service；Europe PMC 可作为经身份校验的补充来源。JATS 优先用于机器解析，同时保存官方提供且允许获取的 PDF；缺少全文时保留摘要/元数据状态，不冒充全文完成。PMC 官方明确并非所有收录内容都可用于复用，需逐篇保留许可。[PMC 开发接口](https://pmc.ncbi.nlm.nih.gov/tools/developers/)、[PMC Cloud Service](https://pmc.ncbi.nlm.nih.gov/tools/cloud/)、[Europe PMC API](https://europepmc.org/RestfulWebService)
3. **跟进现行分发**：PMC 已在 2026 年 8 月 24 日当周移除旧 FTP 和旧 Cloud 的 Article Dataset 文件。新批量下载器使用当前官方分发，不能沿用旧 `oa_package`/FTP 全文路径；现有 OAI-PMH 路线仍在官方允许接口列表中。[PMC 分发变更说明](https://pmc.ncbi.nlm.nih.gov/tools/ftp/)
4. **身份、版本与权利**：PMID/PMCID/DOI 联合去重，文件继续按 SHA-256 保存；摘要、JATS、PDF 是不同来源表示，保留各自版本与证据定位。补充可机读的 license URI、获取/本地处理/训练/再分发用途判定及判定依据，未知用途保持 unknown；有下载权限不自动等于可用于训练。PubMed 摘要也不能仅因可获取就默认可再分发。
5. **增量与质量**：同时考虑新收录、旧文献补充、更正及撤稿，保留检索水位和定期回查记录。报告发现数、去重数、全文可取得数、许可待定数、下载成功数和失败原因；全文下载成功率以合格且可取得的条目为分母，同时另报总体全文覆盖率，避免掩盖开放获取选择偏差。

首轮已确定近五年窗口 2021-10-15 至 2026-10-15，历史文献后续分批回填。首批 50 篇元数据与 5 篇 PMC 全文验证后，2026-10-16（北京时间）以相同限速续采至累计 1,000 篇元数据、50 次全文尝试（49 篇成功）并暂停。分类追加 v2 和 v2.1，原批次检索词表、断点及旧分类保持不变；全部来源版本通过文件校验。“覆盖全部实体瘤”指主题覆盖，不能据此宣称已经穷尽全部年份和全部出版商全文。

### 设备与模型实验

本机只读检查结果：4 张 NVIDIA GeForce RTX 2080 Ti，驱动实际报告每张 22,528 MiB（22 GiB）显存，驱动版本 535.309.01；系统可见 32 个逻辑 CPU、94.3 GiB 内存。检查时可用内存约 52.9 GiB，项目所在文件系统剩余约 11.8 TiB。各 GPU 已有显存占用；这些数字是设备快照，不代表已独占资源。

建议分开处理两个任务：

- **文献分类**：规则/MeSH 基线 → PubMedBERT 等编码器分类基线 → GLiNER2 或小型指令模型对照。采用多标签输出和可校准的置信度；分类标签不等于临床证据或疗效结论。
- **知识抽取**：先比较受 schema 约束的提示词方法，再用本项目校核样本进行 LoRA/QLoRA 任务微调。候选包括 Qwen3-4B-Instruct-2507 兼容性基线，以及较新的 Qwen3.5-4B、MedGemma 1.5 4B；选择以本地可运行性和实体瘤评测结果为准。模型官方条款和训练语料使用权需分别确认。[Qwen3 官方仓库](https://github.com/QwenLM/Qwen3)、[Qwen3.5-4B 官方模型卡](https://huggingface.co/Qwen/Qwen3.5-4B)、[MedGemma 1.5 模型卡](https://developers.google.com/health-ai-developer-foundations/medgemma/model-card)

这组设备值得先做单卡 2B–4B 参数模型的量化适配器实验，再评估 7B–8B 对照；尚未运行训练或吞吐测试，不能保证具体 batch、上下文长度或训练耗时。先用短段落验证前向、反向、存档和恢复，逐步增加上下文。4 张显卡不能直接视为一张 88 GiB 显卡，多卡并行需另外测量通信与框架支持。

2080 Ti 属于 Turing，训练栈应先验证 FP16/FP32 路径及兼容量化实现，不能照搬依赖 BF16 或标准 FlashAttention-2 新架构路径的配置。bitsandbytes 官方列出的 NF4/FP4 门槛允许覆盖这类硬件，但实际框架、CUDA 构建和模型算子仍需小样本实测；不为选型试验直接改动现用服务的全局驱动。[FlashAttention 硬件支持](https://github.com/Dao-AILab/flash-attention#nvidia-cuda-support)、[bitsandbytes 安装要求](https://huggingface.co/docs/bitsandbytes/main/en/installation)

先进行任务微调和必要的教师标注实验，不直接启动从零预训练或全文继续预训练。教师输出作为候选标签，需经过规则校验和独立核对后才进入可信训练集；不能把自动标签同时作为训练目标和测试答案。用户尚未确认外部模型发送范围，因此当前方案按本地处理设计，外部教师是可选分支。

### 数据集与验收

- 分类核对集按癌种、年份、研究类型和相关性分层，主动补充罕见癌种、混合研究及相似但无关的文献。可从约 1,000–2,000 篇的校核样本起步，规模是实验建议，不代表足以覆盖全部类别；根据各类错误和学习曲线补充。
- 按论文/研究或试验 ID 分组切分训练、验证和独立测试集，加入较新时间段测试。同一篇论文的摘要、全文、不同版本和相关试验报道不能散落到训练与测试两侧；独立测试集不能参与教师示例或提示词调优。
- 分类报告每类 precision/recall、macro-F1、混淆情况、罕见类别召回、拒判覆盖率以及每千篇耗时和成本。筛选阶段优先减少漏检；可将相关性召回 95% 作为待验证目标，同时检查精度和拒判量，不以多数类准确率验收。
- 抽取效果独立评估目标实体及关系；数值/单位、组别、时间点等只在目标字段要求或影响所存关系含义时计入。合法JSON、精确引文、目标接口可导入与语义有效分别报告，不用原文全部细节重建率代替业务知识有效性。
- 数据集版本记录来源、使用权利、切分、标注者/教师、修订记录；模型版本记录基座、adapter、提示词、schema、运行配置和评测报告。保留旧版本，支持比较与回退。
- 人工校核集用于训练和评测；线上仍沿用 **AI 通过即可发布、人工抽查独立** 的产品规则，不改成人工必审。

### 结构化知识与存储建议

建议以关系数据库保存事实、版本与审核状态，原文继续保存在现有文件存储，向量索引用于语义召回。第一批采集可沿用当前 SQLite 和单 worker；并行写入或批量吞吐成为瓶颈时，规划有备份、校验和回滚的 PostgreSQL 迁移。需要语义检索时，优先评估同库 pgvector，不先同时部署多套数据库。

| 层 | 建议保存什么 | 职责与边界 |
| --- | --- | --- |
| 原文文件 | JATS、PDF、附件、内容摘要 | 不可覆盖，继续使用 `var/library/blobs`；将来可适配对象存储 |
| 关系主库 | 文献/版本、采集计划、分类、标准实体、研究、证据、任务及审核 | 主记录和审计依据；结构化过滤与一致性约束 |
| 关键词及向量索引 | 定位块文本、embedding、模型版本及源记录 ID | 可重建的检索投影；撤稿、隔离、版本与发布状态由主库控制 |
| 可选图投影 | 经校验的实体关系和研究关联 | 有跨实体多跳问题时再评估，不代替原始证据与适用条件 |

当前知识结构先按tumor_api及biomed实际需要的知识类型确定必需/可选/不采字段，保留核心实体、关系和两层出处。`Study / StudyArm / OutcomeObservation / EvidenceSpan`可作为后续增强结构，不能要求每条知识先完整提取人群、样本量、终点数值、置信区间、时间点及局限才算成功。未结构化的目标外细节仍可从原文回查；影响实际关系含义的限定不能被错误泛化。未报告的字段不从常识补齐。

同一基因、药物或终点在不同分组、时间点和论文中不能合并成一个失去条件的事实。试验药物代码暂时不能映射 RxNorm 时保留原文 mention 和未解析状态，不能制造标准 ID。原文引用扩展为可关联多个段落/表格单元格的 EvidenceSpan，保留现有引句字段的兼容入口；具体 schema 在抽取阶段再用真实样本定版。

检索顺序建议为“结构化筛选 + 关键词与语义候选召回 + 重排 + 原文定位”。pgvector 官方支持与 PostgreSQL 全文搜索组合，并采用 RRF 或 cross-encoder 融合；原生全文搜索不直接等同 BM25。带癌种、许可和版本过滤的近似向量检索必须另测召回率，不能只看无过滤相似度测试。[pgvector 混合检索与过滤说明](https://github.com/pgvector/pgvector)

只有真实查询表明需要跨研究、跨实体多跳汇总时，再以同一批问题比较关系查询、普通混合检索和 GraphRAG；新增图索引须能追溯到具体证据事件，不能把模型猜测的关系当作主库事实。

### 近期研究对本项目的启示

下表区分论文结果和本项目推论，不用通用问答成绩替代实体瘤文献分类/抽取验收。

| 资料与时间 | 研究或官方资料支持的结论 | 对本项目的建议与限制 |
| --- | --- | --- |
| [Chen 等，Nature Communications，2025-04](https://www.nature.com/articles/s41467-025-56989-2) | 在所测 BioNLP 任务与模型中，专门微调方法在多数任务上优于零/少样本 LLM；医学问答与抽取的优势不同 | 分类与抽取都保留专门微调基线；该论文评测的模型较早，不能据此宣称优于所有 2026 模型 |
| [Gade 等，BioNLP 2025](https://aclanthology.org/2025.bionlp-1.9/) | 跨 61 个生物医学语料的关系三元组评测显示，跨数据集泛化仍有明显困难 | 测试集覆盖多个癌种和研究类型，不能用一个癌种的成绩代表全部实体瘤 |
| [GLiNER2，EMNLP 2025](https://aclanthology.org/2025.emnlp-demos.10/) | 编码器配合 schema 接口支持实体识别、分类和层次化结构提取 | 为低成本分类/浅层抽取提供候选；它不是已经验证的肿瘤领域模型 |
| [MedGemma 1.5，2026 官方发布](https://research.google/blog/next-generation-medical-image-interpretation-with-medgemma-15-and-medical-speech-to-text-with-medasr/) | 发布 4B 医学模型，扩展医学文档及影像能力 | 适合作为医学小模型对照；医学报告或问答成绩不能直接推定论文结果表格抽取可靠 |
| [生物医学关系提取基准，2026 预印本](https://arxiv.org/abs/2609.19071) | SNPPhenA 的句子/摘要分类与关联强度分类难度不同 | 将主题分类、关系抽取和证据强度判断分开评测；该预印本并非实体瘤基准，也不作为临床分级依据 |
| [GraphRAG-Bench，2025 预印本](https://arxiv.org/abs/2506.02404) | 针对教材多学科推理，分别评价图构建、检索和回答 | 图方法的收益需要任务级对照；该结果不能证明实体瘤知识库应优先部署图数据库 |

### 分阶段交付与顺序

| 阶段 | 可检查的交付 | 进入下一阶段前的核对 |
| --- | --- | --- |
| 1. 全实体瘤采集 | 版本化检索/分类词表、批次任务、日期分片、元数据去重、许可标记、PMC 全文/JATS 与可用 PDF 获取 | 首个有限批次能中断续跑；重复执行不重复入库；每条资料有来源和明确获取状态 |
| 2. 分类标准与基线 | 多标签定义、规则/MeSH 基线、分类结果与待判定列表、分层人工校核集 | 能解释类间边界和漏检；长尾、混合研究及非实体瘤负例均有覆盖 |
| 3. 分类小模型 | 编码器与小型指令模型对照，版本化训练集、模型和评测报告 | 独立测试表现、拒判量及吞吐相对基线有可复现收益，再纳入持续分类 |
| 4. 结构化知识提取与适配 | 目标实体/关系结构、来源跨度、biomed知识及tumor_api兼容包 | 先完成提取保存与目标结构检查；语义核验独立记录，按目标必需字段评价，不作为提取的前置模型步骤 |
| 5. 抽取模型适配 | 用校核数据做受控 LoRA/QLoRA，比较提示词基线与任务微调 | 抽取质量、误发布率和成本达到约定目标；能回退旧模型与历史证据 |
| 6. 检索与扩容 | 结构化查询、必要的 PostgreSQL 迁移、向量索引及混合检索 | 通过癌种/版本/状态过滤测试；有需要时再评估图查询或独立向量库 |

阶段 1 的有预算采集链路与阶段 2 的词表 / 规则骨架已实现，详细参数与真实首批结果见[采集说明](literature-collection.md)。分类评测集建立后再启动小模型训练；自动 PDF 附件、复杂表格、定时增量、向量索引和图投影仍待后续。分类校核投入、抽取字段优先级及外部教师使用方式尚未确定；本轮不发送文章给外部模型。
