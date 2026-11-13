# 文献知识的离线目标导出

最新修复见[028 · 核心知识与目标兼容优化](research/028-target-optimization.md)：映射v6配合复合实体拆分、必要入组上下文及独立事实抽取，在两篇真实论文上形成3条知识、2条兼容包。原常驻72条记录未被改写或伪造分子触发条件。兼容通过与独立质量核验分别记录；本轮未连接tumor_api。

入口：`scripts/export_knowledge_candidates.py`。输入沿用 `knowledge-candidates-v1`，不调用模型，不连接或修改 tumor_api，不要求语义核验通过。输出每条知识的目标载荷、适配状态、具体缺口，以及完整的 biomed 伴随记录。

```bash
python scripts/export_knowledge_candidates.py \
  --bundle var/experiments/RUN/bundle.json \
  --output var/experiments/RUN/tumor-api-export.json
```

输出已存在且内容一致时复用；内容不同则拒绝覆盖，须指定新版本。`--dictionary` 和 `--mappings` 可选，不提供时仍导出知识及具体缺口，不生成虚构 UUID。

## 常驻首批为何29条结构合格、目标通过为0（2026-11-06）

已再次只读核对目标源码：`EvidenceConclusionInputSerializer.validate`仍要求每项结论至少一个靶点入口，`LogicalTargetEventType`仍只有mutation、cnv、fusion；serializer SHA-256与冻结合同一致。预后等类别可以存在，但表达量不能作为DNA变异入口。当前限制来自证据中心真实接口，不是biomed额外要求每个数字与原文逐字一致。

首批39条中29条形成结构。对原模型输出重新运行本地映射和离线验收，0条通过；历史单药对照的`E-34096690-0002`仍通过，真实映射通路没有退化。此前“跑通”是有明确分子条件的对照通路通过，不是所有肿瘤知识都可导入。

| 具体知识 | 本次核对 | 影响目标适配的原因 |
| --- | --- | --- |
| [E-42117884-0029：二期与三期试验汇总ORR](http://192.168.124.2:50011/knowledge/candidates/?candidate=648bce65-8d1d-548b-9c74-b8deb5d386dd) | 保留7%／24%及跨癌种、跨试验范围 | 没有具体分子适用条件；不能补成某药针对某变异的疗效 |
| [E-42825379-0026：HDAC6表达与前列腺癌预后](http://192.168.124.2:50011/knowledge/candidates/?candidate=85be7031-9eca-5de0-a90a-e726a71e4e9c) | 原文是表达量及生存关联 | 即使归到预后类别，也不能把表达升高写成HDAC6扩增或突变 |
| [E-42825379-0031：tubastatin A体外作用](http://192.168.124.2:50011/knowledge/candidates/?candidate=93753504-3846-5c0d-8c11-24c7560c443b) | 前列腺癌已有目标ID，药物已有本地身份并生成自动新增操作 | 即使补齐药物，机制／体外实验仍没有给出基因组触发条件，不能仅补ID获得通过 |
| [E-34096690-0002：KRAS G12C、NSCLC、sotorasib](http://192.168.124.2:50011/knowledge/candidates/?candidate=65bb89c1-57d5-5213-a86b-a45b87ca513d) | 当前映射重放仍通过1条 | 明确基因、位点、疾病和治疗；既有数值人群问题及未验证的实际报告匹配仍独立保留 |

本次修复的是**把目标范围不适用误标成待映射**的问题。新增`biomed.target-readiness.v1`，由`apps/curation/knowledge_target_readiness.py`共用在导出、工作流汇总和知识页面：先分提取结构缺口，再分离线通过、自动实体补齐、目标范围不适用和真正待处理。不会放宽目标必填条件、填造靶点或将质量核验加入提取门槛。

规则只解释当前已存事实及引用片段，不断言整篇文章没有可用知识。完整提取有精确引用，且事实、实体和引用中均没有突变／CNV／融合线索时，表达／蛋白／签名、机制／体外功能或无分子条件的结果分为当前目标不能表达。若源文存在基因组事件线索，即使模型未抽到也保留待处理；图号S1A和分期T2a不作为变异。缺引用、部分或失败提取不能据此判断原文不适用。原适配字段、错误、原文及独立核读不覆盖。

首批自动分流为：**28条目标范围不适用、1条仍需处理、10条结构待处理**。剩余`E-42825379-0001`的主事实是体外机制，但同时引用了引言中DNHD1在其他疾病／癌种的背景突变，保守规则不自动删除这些线索。本次原文核读可见它们不能直接作为本条治疗适用条件；它不是“补一个药物ID就能导入”的案例。网页保留此疑点，不冒充已自动解决。

新版映射、完整导出及阳性对照在`/appdata/02_resource/apps/biomed/operations/knowledge-processing/compatibility-20261106/`的`first-batch/`和`positive-control/`。原`final/`文件不改，未增加模型调用、未重抽知识、未连接目标数据库。旧知识页展示新的范围诊断，同时保留原始适配记录；后续批次自动生成同口径统计。

### 进一步根因：通用闭环尚未成立

上述分流解释了首批0条，但不能证明整个自动映射流程已经足够通用。继续核对实际模型输出、提示词和映射代码后，发现三个影响产出的工程问题：

1. **输入与出口目标不一致。** 常驻服务按已有JATS全文、标题中的trial／treatment等词领取，三分类只要求实体瘤；第二批实际进入的是宫颈癌治疗中的镇静、肺癌居家给药可行性及金属配合物实验。它们可以贡献广义知识，却不能保证有目标证据中心需要的分子条件。大量元数据不等于已有大量这类全文。
2. **抽取要求限制了召回。** 当前Bonsai提示词明确要求每个ANCHOR只生成ONE knowledge item。PMID 40445863的实际段落同时报告全体32例ORR 31%与ctDNA亚组结果，自动记录`E-40445863-0019`只选择后者；检查全篇已存结果，没有总体ORR 31%这一条。不能把“这次没抽到目标可用事实”解释成“原文没有”。该总体事实仍需正常提取、字段组装与来源验证，不能把本次诊断算作新增自动产出。
3. **映射实现比目标接口更窄。** 自动生成路径目前仅覆盖单基因、明确氨基酸替换、单药和受限英文句式；目标本身支持的CNV、fusion尚未形成自动生成路径。真实`E-34096690-0001`已写出KRAS p.G12C、NSCLC和sotorasib，但前两者合在疾病实体里，缺独立分子ref便被判未映射。按同文已核实身份构造的诊断载荷可以通过离线目标字段合同；这只是定位工程缺口，没有修改原知识或宣称自动修复。

句式对照复现了第3项：5个明确标记的合成输入保持相同核心疾病／变异／单药，只有原模板句式通过；将patients改为participants、加入daily或剂量后缀、分两句说明人群与治疗，均被`source_does_not_prove_variant_selected_treatment`拦住。该结果只证明句式依赖，不是模型准确率评估。

完整源文、原事实、诊断载荷及5个对照结果保存在上述核对目录的`root-cause-probe.json`。本次模型调用0、数据库访问0、目标写入0。后续优先使用这两篇已有全文修复条件拆分、必要字段衔接和同段独立事实遗漏；先证明原文明确、目标可表达的知识能自动产出，再扩大适配验收范围。单个阳性对照通过不再作为通用生产闭环成立的依据。

本轮目标是当前网站“证据中心”的**临床证据、靶点规则**两个入口。已只读核对 `tumor_html/src/router/routes/modules/evidenceCenter.ts`；旧辅助诊断、预后和基因功能库属于其他模块，不把当前证据中心的限制泛化为所有知识库都不能收纳，也不为绕过限制自动转存旧库。所有结果仅供用户核对，不向 tumor_api 发起创建或导入请求。

## 真实目标字典快照

2026-11-01 13:13（北京时间）通过运行中的 `zhuqin-tumor-api` 配置，只读取得501个药物、752个癌种、15条靶点规则，以及本批相关基因的11个既有直接触发条件、87条位点库记录。快照为 `var/experiments/target-compatibility-20261101/target-dictionary.json`，记录实际读取时间、范围、源码版本及规则 AST/条件，不包含患者、样本、报告或账号数据。

```bash
python scripts/snapshot_tumor_dictionary.py \
  --output var/experiments/NEW_RUN/target-dictionary.json
```

该命令以独立驱动执行固定的 SELECT，在一个 PostgreSQL `REPEATABLE READ / READ ONLY` 事务中完成，并设置查询和锁等待超时；不启动目标 Django，不修改目标源码或数据库。凭据只在原容器进程内使用，不写入快照或日志。既有快照直接复用，重复路径拒绝覆盖。相关位点查询有明确基因范围，不冒充完整位点库。

当前目标提交已到 `1ba2ba69`，证据中心模型、创建 serializer 和逻辑靶点实现相对原冻结合同未变化；本轮快照同时记录当前源码哈希。名称映射成功不等于触发条件适配成功，也不等于报告会实际匹配：目标癌种不递归覆盖下级，mutation 没有“任意突变”通配。

## 本次30条核对结果（2026-11-01）

选定方案15条和新5篇首批15条均保留原知识、文章和片段出处。89个参与药物/癌种引用中，16个唯一原名匹配、31个本次提出的主癌种对应，合计47个引用对应4个药物ID和8个癌种ID；后31个须由用户核对，不能说全部已确认。另41个当前规则未匹配，1个病程短语不能作为癌种身份；未命中不等于目标字典不存在。

| 目标适配结果 | 数量 | 具体含义 |
| --- | ---: | --- |
| 当前证据中心不能按原意表达 | 26 | 15条没有必要分子触发条件，11条为表达、蛋白、血清指标等目标不支持的条件 |
| 条件或实体仍待明确 | 3 | K15的H3家族范围、E-42050770-0001的实体缺口、E-42845214-0002的APC泛突变范围 |
| 目标草稿字段冲突 | 1 | E-42272324-0001的类别未定但带药物字段，同时条件未明；保留原文，不为通过校验强填治疗类别 |
| 离线适配就绪 | 0 | 不能仅补目标UUID或伪造变异来获得通过 |

这是目标兼容结果，不是语义有效率。旧15条已存的独立核验仍保留，新15条仍未核验；本轮没有新模型调用或生产导入。用户核对页为[研究022](http://192.168.124.2:50011/management/research/?experiment=target-compatibility-20261101)。

最终文件均在 `var/experiments/target-compatibility-20261101/`（实际位于 `/appdata/02_resource/apps/biomed/research/artifacts/target-compatibility-20261101/`）：`baseline/tumor-api-export-v3.json`、`e2e/tumor-api-export-v3.json`、`minimal-selected-mappings-v3.json`、`minimal-e2e-mappings-v3.json`、`review-notes-v3.json`、`acceptance-v3.json`。早期v1/v2草稿保留，不作为当前核对入口。重新导出时使用同一bundle、`target-dictionary.json`及对应v3 mappings；原文知识不回写或重抽。

## 身份与角色

`fact.entity_roles` 使用本地实体 ref 为键，值为 `disease/intervention/comparator/molecular/context` 列表；也可读取 mention anchor 的同名角色。只有 `intervention` 下的具体药物和 `disease` 下的癌种进入目标关系。旧 `entity_ids` 库存不证明实体参与关系，缺角色时记录 `participant_roles_pending`。

目标字典是单独的只读快照：

```json
{
  "schema_version": "tumor-api-dictionary-v1",
  "source": {"snapshot_id": "真实快照标识及时间"},
  "drugs": [{"id": "真实目标UUID", "name": "原目标名", "aliases": []}],
  "tumors": [{"id": "真实目标UUID", "name": "原目标名", "aliases": []}],
  "rules": [{"id": "真实目标UUID", "review_status": "approved", "is_active": true, "is_locked": false}]
}
```

这里展示格式，字符串占位符不是有效输入。已有药物和癌种接受唯一的名称/别名精确匹配，或外部明确 crosswalk；NCBI、RxNorm、MONDO、biomed ID 都不能代替目标 UUID。模糊或多重匹配保持未决。已确认具体药物缺少目标条目时，按下述自动新增操作处理，不再要求人工补字典。

`--mappings` 为按 `group_id` 索引的对象；每条可有：

- `entity_ids`：本地实体 ref → 目标 UUID；提供 `basis` 记录映射来源。
- `evidence_category`：无法由关系类别唯一决定时的明确目标类别。
- `trigger_scope`：记录必要分子条件、非触发的机制/结果/背景实体及原文引用；`kind` 为 `none/unsupported_marker/direct/rule/unresolved`。有分子名不等于有适用条件，抽取缺实体也不等于原文没有条件。
- `treatment_scope`：记录 `none/single/combination/alternatives/sequential/unspecified`、实际参与实体、分组及原文引用。别名保留原词；互斥方案和序贯不平铺成一组联合药物。映射不能将 comparator/context 提升为干预或分子条件。
- `trigger_bindings`：每项包含 `entity_refs`、规范化来源 `basis` 和目标 `input`。`input` 是真实 `direct` 三元组（event_type、gene_symbol、target_key）或已有 `rule` 的 target_rule_id。只绑定必要条件；复合规则须提供 `expected_expression/expected_conditions`，与快照中的真实 AST/条件一致，不能用一句说明代替 AND/OR/NOT 核对。多个具体变异 key 都须有来源支持，不能把单个 K27M 扩成 K27M/K28M/K27I。

模型不得生成目标 UUID、证据等级、触发规则或由“获益”推定药敏。原文知识没有分子触发条件时，当前目标接口不能表达；蛋白表达和血清标志物不能为通过检查而写成 DNA mutation。

## 状态的含义

| 状态 | 含义 |
| --- | --- |
| `offline_validated` | 已有来源和角色、映射完整，符合冻结源码约束，引用 UUID 存在于所给快照 |
| `ready_after_entity_sync` | 其他离线检查通过，执行自动药物查重/新增并回填返回的 UUID 后继续；不是人工审批状态 |
| `preview_validated` | 隔离目标预览库中的 ID 回填和结构检查通过，不计为生产离线就绪 |
| `pending_mapping` | 缺角色、类别、唯一身份或明确的分子条件映射 |
| `not_representable` | 已明确没有目标要求的分子触发条件，或是目标不支持的表达/蛋白等条件；知识仍完整留在 biomed |
| `invalid_payload` | 字段、枚举或跨字段约束不符 |
| `invalid_source` | 缺合法来源版本、原文件哈希或原文定位 |

`offline_ready` 仅指冻结源码与提供快照的离线检查。`matching_validation.snapshot_consistency` 只说明与快照逻辑键/规则是否一致，`live_sample_matching` 始终为 `unverified`，不把既有证据的键相同当成真实报告可匹配。所有结果 `submit_ready=false`，预览载荷 `is_active=false`：本工具没有执行目标实时 serializer、检查导入时的规则/字典状态和生产重复证据，亦不发布目标知识。语义核验单独保留，不参与导出状态判定。

目标证据中心不能结构化保存联合/序贯、研究语境和逐句出处；导出描述逐字段渲染已存内容，完整语义、原文、版本、定位和核验信息仍在 `biomed_companion`。不能仅保留目标载荷而丢弃伴随记录。

依据：2026-10-23 的原冻结契约为 `biomed.target-contract-assessment.v2`；2026-11-01 结合真实字典和条件/药物角色检查更新适配合同为 `v3`，目标创建 serializer 本身未变化。检查覆盖必填/类型/长度/枚举、药物与类别约束、direct/rule互斥、规则可用状态及重复结论。单测使用明确标记的合成字典；本次30条使用真实只读字典，不混用两者。

## 批量适配与新变异键（2026-11-02）

`scripts/prepare_knowledge_target_mapping.py` 从知识记录、既有目标快照和本地实体规则快照生成映射，不再依赖固定样本的手工表，也不访问目标数据库。完整批次入口为 `scripts/run_literature_batch.py`，见[025 · 十篇批量提取](research/025-ten-paper-pipeline.md)。

本轮只读核对目标创建源码后，将适配合同更新为 `biomed.target-contract-assessment.v4`：`direct` 允许创建新的具体变异键，不要求旧证据库已经存在同键。旧快照只覆盖少数基因，不能把“快照未命中”当成“目标不允许”。明确的基因与单一氨基酸变异须同时有抽取条件、原文适用人群和本地正式基因符号支持；其余范围继续待定，不能以新增为名补造泛突变或表达条件。

新键可以通过离线字段检查，但记录 `snapshot_consistency=not_covered` 和 `live_sample_matching=unverified`。真实报告的转录本、变异标准化与匹配仍须在后续接入阶段验证。旧研究包不改写，所有新包仍 `submit_ready=false`，没有向 tumor_api 写入数据。

025实际批次已采用映射方法 `biomed.knowledge-target-mapping.v4`：允许原文明确的同病种写法、同一文章内定义的缩写和唯一单药剂量句；非分子性的试验背景、剂量及治疗史保留在描述和伴随记录中，不能说目标trigger会执行这些限制。ctDNA/VAF动态、表达、复合变异、否定条件及结果组中的额外分子限定仍不能被压成单一变异。

实际单药试验 `E-34096690-0002` 通过离线结构检查，其他两条缺独立分子参与实体仍待定；主批和ctDNA对照均为0条通过。该通过记录的两处数值人群问题独立保留，不代表语义核验通过。完整结果、实际文件版本和逐条核对入口见025；不将离线结构通过数解释为可直接生产使用数。

## 已确认药物自动新增（2026-11-06）

此前35个未匹配名称的上游身份问题，现由[每批共用的自动实体归一](entity-normalization.md)处理。其输出是带来源和规则的本地身份；本节负责在目标库查重、新增具体药物并回填ID，两者依次执行。

用户确定：**本地已识别的具体药物，目标库缺少时自动新增，不设逐药人工审批。** 唯一已确认规则指向 active 的 `drug_ingredient` / `molecule`，且本条原文有对应名称时，v5映射生成 `ensure_drug`。身份确认可以来自字典、规则或模型；无需人工重复确认，也不强制外部标准ID。药物类别、联合方案和未明确身份不作为具体单药新增。

统一执行：按本地标准实体合并重复操作 → 查已有对应关系、完整标准名及已确认的全局别名 → 已有则复用、缺失则新增 → 保存目标返回的 UUID → 回填所有知识引用。文章内缩写不扩成全局别名，增加已确认别名后仍复用同一目标ID。单个实体冲突不阻断其他实体，原始知识和出处保留。

导出包 `entity_operations` 保存操作，`target_payload_template` 用 `$entity_operation` 引用返回结果，`contract_validation_scope` 明确检查对象。普通 `target_payload` 不使用占位UUID，不能将未执行操作称为已创建。批量入口自动生成 `target-entity-operations.json`；旧 `review_only` 标记仍指不写生产，并非人工批准门槛。

已只读核对目标依赖 `drf_somatic_biomeddb==3.41.1`：Drug 的 `name_cn` 是最长255字符的必填唯一显示名，`name_en` 可空；无中文名时沿用原始标准名，不伪造翻译。字段合同与源码哈希保存在 `apps/curation/target_entity_sync.py`。

当前执行器只写**独立 SQLite 目标预览库**，生产写入连接尚未接入。新增和对应关系在同一事务保存，重跑、中断恢复和并发调用复用ID；拒绝写入其他应用数据库。未来生产适配器需要保持同样的原子查重、创建及回执语义，不重新拉目标库作为本地底库。

```bash
.venv/bin/python -B scripts/preview_target_entity_sync.py \
  --bundle var/experiments/ten-paper-closure-20261102/final-v2/knowledge.json \
  --dictionary var/experiments/target-compatibility-20261101/target-dictionary.json \
  --registry var/experiments/ten-paper-closure-20261102/registry-snapshot.json \
  --preview-database var/experiments/target-entity-sync-20261106/target-preview.sqlite3 \
  --output var/experiments/target-entity-sync-20261106/NEW_RESULT_VERSION
```

实际182条主批重放：自动新增7种药物（sulindac、piroxicam、tretinoin、alitretinoin、Liarozole、TALAROZOLE、dordaviprone），回填94处引用，失败0；第二次新增0、复用7，ID一致。它们是已确认身份、目标快照缺项的药物，不是把此前35个未识别名称全部当新药。原始知识及出处逐条保持不变；未重新抽取或扩大归一范围。

结果位于 `var/experiments/target-entity-sync-20261106/` 的 `main-first/`、`main-replay/` 及补齐全局别名后的 `main-alias-replay/`，实际目录 `/appdata/02_resource/apps/biomed/research/artifacts/target-entity-sync-20261106/`。主批仍有157条其他映射缺口、18条字段约束问题、7条来源问题，完整通过数仍为0；补药不掩盖其他缺口。新模板额外暴露11条“非治疗类别携带药物”的既有关系约束问题，原始关系未改。测试覆盖完整自动创建/回填、重跑/中断/并发去重、别名复用、歧义隔离、类别约束、原文保留及预览和生产状态分离。
