# biomed 产品架构与数据契约

2026-10-12。按用户确认的“真实标准库 + 临床证据 + 文本处理工作流”实施；AI 通过允许发布，人工抽查。英文论文 PDF 为首批输入；具体模型尚未确定。

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

- `SourceRelease`：命名空间、官方版本、来源地址、使用权利、文件 SHA-256、预期记录数及当前版本标记。
- `Concept`：以 `(release, identifier)` 唯一，保存规范名称、同义词、父级、交叉引用和定义。
- 部分下载/导入失败不切换当前版本。重试按内容校验值与唯一标识补齐暂存批次；完整旧版本仍然保留。
- MONDO 的 `xref` 作为交叉引用原样保存，不把所有交叉引用自动提升成等价映射。RxNorm 当前仅导入 `IN`（ingredient），不混入需额外许可的完整 UMLS 数据，也不声明适应症。
- 基因查询兼容 NCBI Gene ID、名称和同义词。MANE 索引当前以 accession base 建立联系，查询 `NM_005228.999` 只能解析到基因，必须显示“版本未核验”。后续变异/转录本精确校准应引入来源中的具体 accession 版本、参考组装和映射关系，不依赖字符串截断。

官方来源：[MONDO](https://obofoundry.org/ontology/mondo.html)、[RxNorm IN API](https://lhncbc.nlm.nih.gov/RxNav/APIs/api-RxNorm.getAllConceptsByTTY.html)、[RxNav 使用条款](https://lhncbc.nlm.nih.gov/RxNav/TermsofService.html)。

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

真实模型尚未配置，因此现在没有实际 AI 生成/发布的证据。发布状态机通过隔离测试中的受控适配器验证，不把测试候选加入运行数据库。

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

## 后续按实际问题推进

1. 为首批英文 PDF 建立人工核对集，记录文本页码/表格/引用恢复的具体缺陷。
2. 选定模型运行位置后实现适配器，验证抽取和独立 AI 审核，按用户规则发布并安排人工抽查。
3. 补充证据类型/强度的领域分类、重新抽取比较、抽查任务分配和可度量质量指标。
4. 来源订阅与更新责任、指南许可、进一步转录本版本校准按实际使用确定。

这些是明确的后续产品能力，不通过隐藏脚本、临时数据或修改数据库状态冒充已经实现。
