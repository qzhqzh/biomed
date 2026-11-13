# 疾病、癌种与药物标准资源

更新于2026-10-25（北京时间）。本项目独立维护；未改动 tumor_api。疾病、癌种分类沿用已有版本，本轮增加可追溯的药理类别/机制资源和中文展示别名。RxClass快照已完整校验、入库并启用；文献采集保持原限速。

## 当前已收录

| 资源 | 运行版本与数量 | 用途与边界 |
| --- | --- | --- |
| NCBI Gene | 193,887 条记录，其中蛋白编码 20,622 条 | 包含非蛋白编码及其他基因组记录，不能称为19万个人类基因 |
| MANE | 1.5；Select 19,363、Plus Clinical 74 | 77,610 是转录本/蛋白 accession 索引键数，不是转录本数；精确版本和坐标校准仍需单独处理 |
| MONDO | v2026-10-06；32,134 个概念 | 通用疾病本体，保留名称、别名、定义、多父级和外部引用；不能默认全是人类疾病 |
| RxNorm | 05-Oct-2026；14,710 个 IN 成分 | 标准药物成分，尚不含完整品牌、剂型、规格和复方；外部类别与机制独立存储 |
| OncoTree | oncotree_2025_10_03；898 个节点 | 10月25日官方最新稳定版仍相同，原始树SHA一致。包括根及32个一级组织/分组，898不等于癌种数量 |
| ChEMBL机制子集 | 37；7,561条机制、6,816个分子、1,518个靶点，已入库 | 2,481条药物名称匹配候选，另有10个成分名称匹配到多个分子而保留歧义；关联候选不等于已确认的同一成分 |
| RxClass FDASPL/EPC | MEDRT 2026.09.08；762类、2,394条直接关系、2,231个本地成分，已启用 | 147类在当前直接成分快照中为空；类别→成分可查，未收录类别之间的父子关系；不覆盖全部14,710个RxNorm成分 |

页面：[疾病](http://192.168.124.2:50011/knowledge/diseases/)、[癌种分类](http://192.168.124.2:50011/knowledge/cancers/)、[药物](http://192.168.124.2:50011/knowledge/drugs/)。localhost 使用相同端口与路径。

<a id="transcript-structures"></a>

## 转录本外显子结构（2026-10-28）

转录本结构使用独立、不可变的 SQLite 发布库，不写入持续采集的 Django 数据库。以完整版本编号（例如 `NM_000348.4`）为主键，保存一个可信生物学外显子总数 `exon_count`；基因组映射作为多条子记录，分别保留 `assembly`、染色体、位点、正负链、原始 `raw_exon_count`、每段坐标及来源。不是将 hg19、hg38 的原始分块数写成两个可信外显子数字段。全部外显子包含 UTR，不能等同于 CDS 外显子数。

`exon_status=verified` 表示同版本、人类 RefSeq RNA 的 exon features 已通过结构校验；没有充分依据时 `exon_count=null`、状态 `unverified`。原始映射即使两套一致，也不会自动核验通过。完整的外显子应从转录本首位连续覆盖；末尾纯 A 序列仅在精确 `polyA_site` 位于同一末尾 A 连续段或边界时可作为转录后 poly(A) 尾排除，并留下 `verification_note`，不会多计一个外显子。无位点支持、内部缺口、模糊坐标、缺少 exon features 均保持待核验。`polyA_site` 的含义见 [ENA 官方定义](https://www.ebi.ac.uk/ena/WebFeat/polyA_site_s.html)。

少量旧版 RNA 的尾部纯 A 不满足上述位点条件，但原始记录明确用 `[WARNING] ... replaced by ...` 指向后续版本。脚本另作保守交叉核验：两者同一 base accession、同一非空 GeneID，旧版自身 exon features 自首位连续且只缺尾 A，后续版本本身已独立核验，外显子数量相同、每个内部外显子完整序列相同，首末外显子在剪接边界处最多100bp的序列锚点也一致。通过后确认的是**旧版本自身 features 的计数**，不拿新版计数替代旧版；`exon_support` 记录支持版本、旧/新序列 SHA、锚点长度与双方原始响应来源。缺 features、内部缺口或内部序列改变不会按此晋升，刚晋升的记录也不能链式互相证明。复用函数为 `resolve_supported_exon_counts(records)`。

例如 SRD5A2 `NM_000348.4` 的可信数为 5，hg19/hg38 原始映射分别为 6/5；两者分别保存，差异本身不自动证明生物学外显子结构改变。同版本不同位点的映射不丢弃、不简单取最大值。MANE 的 NM/ENST 完整版本关联独立保留；指定 `.999` 查不到就返回无匹配，无版本查询才列出候选。同一 GeneID 且 GenBank 明确给出旧符号为 `gene_synonym` 时才接受基因改名，原符号和官方别名继续保留。

ASXL1 `NM_015338.6` 与 ASXL2 `NM_018263.6` 的本次官方 GenBank features 都为13个外显子；旧本地 refGene 映射分别为 hg19/hg38 的12/13与12/12。3bp微外显子的不同对齐处理会改变分块数，因此完整转录本版本号相同，也不意味着不同注释来源及发布时间的基因组映射逐块相同。系统保留两类数及来源；特定表格选择保留历史映射值，不会反向覆盖官方转录本结构。原 hg38 文件第82208行 `NM_001164603.1` 只缺 score 列，导入器按其完整 gene/status/frame 结构识别，保存行号和 `missing_score_column` 格式告警，避免将后面的 `cmpl` 误读为基因名；其他不完整行仍阻止发布。

查询接口为 `/api/v1/knowledge/transcript-structures/?q=NM_000348.4`，支持基因、GeneID、完整版本编号及分页；返回可信计数、核验说明和两套映射。旧 `/knowledge/transcripts/` 编号映射接口继续兼容，不把其去版本的索引当成结构证据。网页入口为[基因与转录本](http://192.168.124.2:50011/knowledge/genes/)。

2026-10-28 11:54（北京时间）已发布 `a6858334a8c24121bec797afe0a61497`：78,652个完整版本转录本、170,216条基因组位点映射、19,437对 MANE 精确版本关联。本次累计取得303条 GenBank 记录，其中300条外显子计数通过核验；其余3条 `NM_017763.4`（RNF43）、`NM_006015.4`（ARID1A）、`NM_000314.4`（PTEN）没有 exon features，保持为空；另外78,349条尚未取得本轮转录本结构证据，也保持为空。300条中47条用明确 polyA_site 排除尾A，9条用上述声明替换版与内部序列/边界核验确认旧版 features。导入与验证回执在 `/appdata/02_resource/apps/biomed/operations/transcript-structures-20261028/`；后端25项针对性测试通过。

原始 GenBank 批响应、SHA-256、实际请求 URL 和获取时间在累计缓存 `/appdata/02_resource/apps/biomed/development/transcript-exons-cache/`。未知原始下载时间、ANNOVAR 注释发布版本保留为空；文件 mtime 不冒充获取时间或注释版本。重复请求复用已校验的缓存，失败保留断点。脚本只做网络采集和结构校验，不调用模型：

```bash
scripts/with_appdata.sh .venv/bin/python scripts/fetch_transcript_exons.py \
  --accessions /path/to/exact-transcript-versions.txt \
  --output /appdata/02_resource/apps/biomed/development/transcript-exons-cache \
  --batch-size 20 --max-records 100 --max-bytes 52428800 \
  --interval 2 --bytes-per-second 131072
```

输入要求 `NM_/NR_` 完整版本号，可用 `--mane data/knowledge-updates/releases/a86b866bdb9a424ab06e94e14cb43f86/raw/MANE.summary.txt.gz` 替代 `--accessions`。同一输出目录续跑会跳过已完成版本；`--max-records` 限制本次新增数量，触及预算会暂停，不能把预算暂停当全范围完成。只有合法返回、物种和精确版本集合一致且整批校验通过后才保存批响应。

导入命令 `manage.py import_transcript_structures --hg19 ... --hg38 ... --mane ... --genbank-dir ...` 从已有参考文件和**全部累计** GenBank 响应构建完整新快照，自动读取目录内 `sources.json`；可用 `--source-metadata` 增补已有参考文件的来源，`--root` 指定独立库。后续不能只传新增批次而漏掉以前的核验记录。生产用 UID 10001 的容器进程执行；参考文件不在资料 worker 挂载内时先将选定文件送入容器暂存目录，不扩大挂载或重启采集 worker。

完整校验后原子切换 `current.json`，保留旧 release。原始 GB 按内容哈希保存在 `blobs/<sha>.gb`，各发布版本的 `raw/` 仅建立硬链接，避免重复占空间；大 refGene 原文件保留路径和摘要，不在每个 release 复制。生产路径与备份范围见[部署说明](DEPLOY.md#transcript-structure-storage)。

癌种页面使用可展开、折叠的分类树，子类按需加载并缓存；支持全库中英文检索，并将搜索结果定位到树中的对应节点。药物页面提供“药理分类树”和“全部药物成分”，分类树按“EPC类别→RxNorm成分”两层展示，一个药物可以出现在多个类别中；当前快照没有类别间父子边，不凭类别名称构造更深的树。分类和成分各自分页，展开结果缓存，中英文检索可匹配类别或成员药物。癌种、药物的名称和整行不再打开弹窗，只有独立“详情”按钮触发详情。

浏览与详情沿用同一来源版本；药物树同时固定RxClass dataset与RxNorm release，避免后台更新造成页面混用版本。MONDO 仍保留多父级分类浏览，支持全部父级和全库检索，不强制改成单树。对于父级位于外部本体的概念，保留父级ID，并把它作为本库入口展示，不虚构外部节点。后端按不可变 release 缓存轻量关系索引，不把全量本体发送到浏览器。

## 来源选择

| 来源 | 官方获取与许可 | 本项目决定 |
| --- | --- | --- |
| **MONDO** | [下载](https://mondo.monarchinitiative.org/pages/download/)提供 OBO/OWL/JSON，CC BY 4.0 | 继续作通用疾病主体系。现有OBO解析未保留同义词的EXACT/BROAD等范围，后续精准实体归一须补齐，不能将所有别名视为等价 |
| **OncoTree** | [版本接口](https://oncotree.mskcc.org/api/versions)、[固定版完整树](https://oncotree.mskcc.org/api/tumorTypes/tree?version=oncotree_2025_10_03)、[CC BY 4.0](https://github.com/cBioPortal/oncotree/blob/master/LICENSE.md) | 本轮已收录。独立ONCOTREE命名空间，不取开发版，不覆盖MONDO |
| **NCIt** | [发布说明](https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/ReadMe.txt)核验为26.09d/2026-09-28；[CC BY 4.0](https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/ThesaurusTermsofUse.htm) | 下一批补OncoTree引用的NCIt概念及固定版肿瘤子集，保留署名、版本和衍生状态。本次未导入 |
| **ICD-O** | [IARC于2026-07-20发布ICD-O-4最终表](https://www.iarc.who.int/news-events/international-classification-of-diseases-for-oncology-fourth-edition-icd-o-4-final-tables-now-available/)，[在线及Excel入口](https://tumourclassification.iarc.who.int/icd-o-4/)；[IARC条款](https://publications.iarc.who.int/Terms-Of-Use)对商业整表再利用有限制 | 部位、形态、行为和分级编码独立建模。第4版形态编码扩展为五位，不能沿用3.2的解析假设。未确认独立开放许可前不收录并再分发整表 |
| **RxNorm / RxClass** | [RxNorm许可](https://www.nlm.nih.gov/research/umls/rxnorm/docs/termsofservice.html)、[RxNav条款](https://lhncbc.nlm.nih.gov/RxNav/TermsofService.html)、[关系语义](https://lhncbc.nlm.nih.gov/RxNav/applications/RxClassIntro.html) | 复用现有IN；FDASPL/EPC类别与直接成分关联已完成断点采集、完整校验及启用。不把SNOMED CT等所有RxClass来源视为同一许可 |
| **ChEMBL** | [下载](https://chembl.gitbook.io/chembl-interface-documentation/downloads)、[API](https://chembl.gitbook.io/chembl-interface-documentation/web-services/chembl-data-web-services)、[官方许可入口](https://www.ebi.ac.uk/chembldb/)，CC BY-SA 3.0 | 已导入37版机制及关联分子、靶点子集，不下载整个活性实验库；派生发布保留相应许可。完整适应症、批准状态和活性测定未随本轮全部导入 |
| **Wikidata中文标签** | [数据许可CC0](https://www.wikidata.org/wiki/Wikidata:Licensing)、[NCIt属性P1748](https://www.wikidata.org/wiki/Property:P1748)、[RxNorm属性P3345](https://www.wikidata.org/wiki/Property:P3345) | ID与英文标签/别名同时匹配才保留为reference；保留语言、快照时间及原始响应。来源中文仍可能错误，不视为官方中文或监管通用名 |
| **ChEBI** | [下载](https://www.ebi.ac.uk/chebi/downloads)、[当前许可CC BY 4.0](https://www.ebi.ac.uk/chebi/about)；本次月度档案rel255 | 补化学结构、别名和作用角色；is_a与has_role分开。当前不是CC0；LITE缺别名，不适合单独做文献实体归一 |
| **DrugCentral** | [下载](https://www.drugcentral.org/download)标2026-09-25；[许可入口](https://www.drugcentral.org/privacy)指向CC BY-SA 4.0 | 可补机构/日期、批准药物和机制。先用所需TSV子集，不为本轮新增PostgreSQL16+RDKit整库运行依赖；FDA/EMA/PMDA记录不等于NMPA状态 |

NCIt公开的Neoplasm Core CSV本次为1,395条，Last-Modified为2026-07-11，部分说明仍是旧文档。不能把它标作26.09d最新完整癌种库；后续应从固定NCIt版本取得C126659子集成员或有关概念。

MONDO维护者已于2026年将正式映射入口切换到 `mondo.sssom.tsv`，旧`equivalencies.*`弃用，见[官方说明](https://github.com/monarch-initiative/mondo/issues/9921)。跨库映射须保留关系谓词、来源、版本与核验状态；当前普通xref保持参考性质，不按名字相同或共享NCI引用自动合并。

## 药物关系与机制实现

保留现有`SourceRelease/Concept`成分和分类体系；新增五张表承载资源扩展，不把药物类别或机制塞进`Concept.parents`：

- `ResourceDataset`：来源、版本、许可、manifest摘要、统计与当前版本。
- `ResourceEntity`：类别、ChEMBL分子及靶点，保留来源属性。
- `ResourceAssertion`：直接成分类别关系或机制记录，保留原始记录ID和条件。
- `DrugMoleculeLink`：RxNorm成分与ChEMBL分子的名称匹配候选，保存方法、状态与出处，不自动宣称化学等价。
- `EntityTranslation`：按具体Concept版本保存中文展示别名，来源、语言、状态和匹配依据独立。

RxClass按类别获取`has_EPC + trans=1 + ttys=IN`的直接关联，返回关系必须明确为DIRECT；不逐个查询14,710个成分。见[接口说明](https://lhncbc.nlm.nih.gov/RxNav/APIs/api-RxClass.getClassMembers.html)。本次已在单路2秒间隔、256KiB/s下完成逐项缓存和断点恢复，版本前后核验通过后启用。2,394条关系涉及2,231个不同成分，均能在当前本地RxNorm版本匹配；缺少关联不能推断为该药物无药理类别。

ChEMBL机制必须同时保留分子、靶点、action_type、direct_interaction、分子/细胞机制标志及参考文献、comment和选择性/适用条件；不能只显示“药物—靶点”而丢失限定。分子母体和盐型不因名称接近自动合并；总体`max_phase=4`不证明每条适应症已批准。先前核对的奥希替尼实例中，一条NSCLC适应症`max_phase_for_ind=3`且来源是ClinicalTrials，与机制记录的阶段含义不同：[来源样本](https://www.ebi.ac.uk/chembl/api/data/drug_indication.json?molecule_chembl_id=CHEMBL3353410&limit=1)。这些是外部参考断言，未自动变成本项目审核通过的文献证据。

`/api/v1/curation/drug-resources/?identifier=RXNORM:1721560`提供药物关联资源；`/api/v1/curation/drug-tree/`提供分页类别及成员，支持`q`、`class_id`、`release`和`dataset`。原成分/分类接口增加`translations`，支持中文检索。页面保持英文原名、来源和版本，中文参考/草稿状态分别展示。新增资源不改变正式证据审核允许的实体命名空间。

## 中英文覆盖与边界

癌种中文已覆盖898/898个节点：119条Wikidata参考、779条项目翻译草稿（原57条加本轮722条）。修正版`biomed-oncotree-zh-supplement-2026-10-25-v2`已导入，原176条中文全部保留；v1草稿留存但未导入。

药物新增128条中文项目草稿已导入，当前覆盖2,779/14,710个成分：2,639条参考、140条项目草稿，11,931个成分仍待补充。RxClass类别另有762/762条中文项目草稿，与药物成分中文覆盖分别统计。类别译名只在来源ID与英文名精确一致时使用，不修改原分类关系。

页面中的“参考中文名”表示从可追溯外部来源取得并通过身份匹配的中文标签，不等于官方通用名或专家确认；“中文草稿”表示本项目翻译的展示名称，保留原英文、版本和依据，尚未提升为外部权威译名。“中文待补充”表示当前没有已收录且可用的中文映射，不表示模型不能翻译。剩余药物包含尚未整理中文名的成分、物种或材料身份待核验的变应原、研发代码、少见化合物、盐型和立体异构体等；直接音译或套用相近药名可能混淆不同成分，因此保留英文，并将缺口原因写入`remaining-gaps.json`。中文名称覆盖率与翻译准确率独立，满覆盖也不能宣称准确或临床适用。

癌种匹配同时要求OncoTree的NCIt引用与英文标签/别名一致；共享引用不自动构成概念等价。RxNorm匹配要求成分ID与英文名同时一致。语言保留`zh`、`zh-hans`、`zh-cn`、`zh-hant`等来源标签，不能只接受`zh-Hans`。有标签但只有拉丁名称/研发代码的条目不计中文覆盖。

原始来源标签与最终显示层分别冻结。抽查排除了17条癌种不恰当标签，例如PNET被译为另一类肿瘤、LPL被译为巨球蛋白血症；药物排除/替换16条非中文、品牌或不同译名。原候选与理由仍在`excluded-reference-labels.json`，必要时用明确的`project_draft`展示译名，不覆盖英文及原始出处。中文展示层不替换文献分类规则或冻结评测集。

## 快照、导入与恢复

资源都位于`/appdata/02_resource/apps/biomed/resources/`：

| 目录 | 内容与状态 |
| --- | --- |
| `rxclass-epc-20261025/` | 762类及2,394条直接关系的完整快照、原始响应、断点与导入回执，已启用 |
| `chembl-37-20261025/` | 完整机制子集manifest、分子、靶点及原始响应，已导入；采集约61.14MiB，磁盘约134.44MiB |
| `cancer-20261025/` | 稳定版本核验、完整树、Wikidata参考、中文显示层及排除理由 |
| `drug-translations-20261025/` | 冻结的RxNorm匹配输入、Wikidata响应、中文显示层及排除理由 |
| `translation-supplement-20261025/drugs/` | 新增128条成分中文草稿及11,931条剩余缺口，已导入 |
| `translation-supplement-20261025/cancers-v2/` | 722条癌种中文修正版草稿、显示manifest及缺口记录，已导入 |
| `translation-supplement-20261025/cancers/` | 首版癌种草稿留存，未导入；实际采用cancers-v2 |

RxClass类别中文保存在项目词表`apps/curation/vocabularies/rxclass-zh-20261025.json`，与冻结来源ID、英文名称、源版本及摘要绑定。`scripts/prepare_additional_translations.py`复用已有来源与冻结输入生成补全包，不重采文献或下载模型；药物与癌种补全按现有成分/节点版本匹配，已有中文记录不覆盖。

本轮导入后的只读逐条校验确认：原176条癌种及2,651条药物中文全部保留，新增850条记录（癌种722、药物128）与补全包精确一致、无重复。回执保存在`translation-supplement-20261025/import-verification.json`。

通用导入命令`import_reference_resources`先验证完整manifest和内容摘要，分批暂存，完整后切换当前dataset。失败不激活部分数据，重复导入按内容复用，旧dataset保留。双语导入必须选择`display-translations.json`，不能直接把未经显示层核对的原始`translations.json`全部发布。

例如在项目根目录导入已完成的ChEMBL快照，显式指定现用数据库与原始资料位置：

```bash
scripts/with_appdata.sh env \
  DATABASE_PATH=/appdata/02_resource/apps/biomed/production/var/django/db.sqlite3 \
  CURATION_STORAGE_DIR=/appdata/02_resource/apps/biomed/production/var/library \
  .venv/bin/python manage.py import_reference_resources \
  /appdata/02_resource/apps/biomed/resources/chembl-37-20261025
```

导入中文时使用同样的环境设置，将最后两行改为`.venv/bin/python manage.py import_reference_resources /appdata/02_resource/apps/biomed/resources/cancer-20261025/display-translations.json --translations ONCOTREE`；药物对应`drug-translations-20261025/display-translations.json --translations RXNORM`。RxClass仅在目录内manifest为completed、来源版本前后核对一致后使用同一导入命令。

`0009_reference_resources`仅新增上述五表，已应用，不迁移或删除原标准实体及文献数据。操作前标准库表快照为`/appdata/02_resource/apps/biomed/operations/reference-resources-20261025/standard-resources-before-0009.json`（21,435,472字节，3条SourceRelease、47,742条Concept），SHA和范围见同目录`backup.json`。它不是全业务数据库备份；在线全库备份因持续写入反复重启已停止，`before-0009.sqlite3.incomplete`不是可恢复来源。已有冻结备份继续保留。

此前资源适配器/导入42项测试及分类层级5项测试通过，TypeScript/Vite构建通过。该次真实浏览器验证奥希替尼中文检索、EGFR/T790M机制及限定条件、肺→非小细胞肺癌→肺腺癌中英文和来源；390px手机宽度无横向溢出，控制台0错误，菜单切换保持同一document。该次本地暖请求样本中，成分中文检索约65ms、药物扩展详情约23ms、癌种分类约13–23ms；这是单次样本，不是持续性能保证。文献原限速和配置摘要保持不变，不新增模型下载/训练，不改tumor_api。

此前癌种树视图的11项层级API测试及前端构建通过。Playwright在1440px桌面和390px手机宽度验证了肺→NSCLC→LUAD的展开与详情、全库搜索后树中定位；重复展开已加载分支新增请求为0，手机无横向溢出，控制台0错误、0警告。

本轮药物树、详情交互及中文补全相关15项后端测试、补全准备脚本6项测试、前端构建通过。真实浏览器验证药物中文及编号检索→激酶抑制剂→奥希替尼详情，名称点击不弹窗；激酶抑制剂50+21共71个成分分页无丢失，折叠重开新增请求0，全部药物成分页签检索有效。390px药物页面无横向溢出，控制台0错误、0警告。癌种手机端实测GCTSTM修正版中文显示、名称点击不弹窗、独立详情按钮有效，长名称无横向溢出，控制台0错误、0警告。药物树只读API实测首次约56ms，奥希替尼中文检索约62ms，均为单次样本。文献采集限速与配置保持不变，不改tumor_api。

## RxClass 后台续跑与完成记录（2026-10-25）

用户级服务 `biomed-rxclass-sync-20261025.service` 从原325类断点接管，05:00 UTC实查推进至334/762类、1,109条直接关联；这是当时完整数据尚未启用的历史快照。05:42 UTC来源manifest已完成，最终762类、2,394条直接关系、2,231个成分通过校验并自动导入，dataset已active。最终状态以资源目录的`manifest.json`及`sync-status.json`导入回执为准；后者counts仅在阶段更新时刷新。

运行入口为 `scripts/sync_rxclass.py`，显式绑定生产数据库与library目录；单路2秒间隔、256KiB/s不变。临时来源错误在原抓取重试之外按60/300/900秒有界续跑，只有新类别进展才重置预算；版本/结构/摘要错误立即暂停。导入遇数据库锁按15/60/300秒有界重试，只有命令确认active、版本及全部计数相符才标记完成。服务 `Restart=no`，不会通过无限重启绕过失败上限；flock防止重复采集。

```bash
systemctl --user status biomed-rxclass-sync-20261025.service
journalctl --user -u biomed-rxclass-sync-20261025.service -n 30 --no-pager
```

这是独立于聊天生命周期的一次性后台任务，并非开机定时器；宿主机重启或失败暂停后，先核对manifest与错误原因，再用同一目录运行下列命令续跑。不要删除断点或另建同版本副本。

```bash
DJANGO_DEBUG=1 scripts/with_appdata.sh .venv/bin/python scripts/sync_rxclass.py \
  --output /appdata/02_resource/apps/biomed/resources/rxclass-epc-20261025 \
  --database /appdata/02_resource/apps/biomed/production/var/django/db.sqlite3 \
  --library /appdata/02_resource/apps/biomed/production/var/library
```

新增后台续跑7项测试通过，覆盖临时错误恢复、完整性错误暂停、导入计数核验、重复调用及目标数据库绑定。页面将“完整快照尚未导入”与“已导入快照没有此成分关联”分开显示。

## 2026-10-24分类导入记录

OncoTree导入任务 `b67a7433-658d-46d6-91ab-962c47d90eed`，第二次连接尝试成功。原始树246,435字节，SHA-256为`613c84ebecce7b47fb6800a94efecf8c7edbc25120401d898591d3b478c36231`；内容按来源版本与原始blob保存，tissue/mainType/history等未建模属性仍保留在原文件。

实际数据库位于 `/appdata/02_resource/apps/biomed/production/var/django`，原始来源文件位于 `/appdata/02_resource/apps/biomed/production/var/library/blobs/61/613c84ebecce7b47fb6800a94efecf8c7edbc25120401d898591d3b478c36231`。898个节点中666个有NCI引用、692个有UMLS引用，仍不自动声明概念等价。入库校验唯一ID、真实根、父级闭合、层级、最小规模与下载前后固定版本一致，分批暂存后原子激活。

该次新增来源采用单路2秒间隔、256KiB/s读取上限；文献采集配置未改。10月24日分类导入本身无schema迁移，没有复制模型、覆盖旧版本或发布临床证据。

10月24日验证覆盖来源结构及中断恢复、多父级/外部父级、版本隔离、分页与既有入口；前端TypeScript/Vite构建通过。该次真实页面验证了肺→非小细胞肺癌→肺腺癌详情与NCI引用，以及MONDO的人类/非人类疾病分支；菜单切换保持同一文档，按页面加载数据。
