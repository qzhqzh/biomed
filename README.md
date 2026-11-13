# biomed

以真实标准实体、可追溯临床证据和资料处理工作流为核心的生信与医学知识库。唯一项目目录为 `/data/06_project/biomed`，沿用原 online-biotool 的 Git 历史，远端为 https://github.com/qzhqzh/biomed。

统一入口：[公网 HTTPS https://biomed.qzhqzh.com/](https://biomed.qzhqzh.com/)、[局域网 http://192.168.124.2:50011/](http://192.168.124.2:50011/)、[服务器本机 http://localhost:50011/](http://localhost:50011/)。当前部署均需登录；账号共用全部工作台功能，不设置用户组。公网账号凭据保存在 `var/private/public-biomed.txt`，不提交仓库。18765 已停止对外监听，原教学流程进入统一界面的“教学验证”。

## 整合与分支状态（2026-10-15）

- 统一工作台已通过 [PR #9](https://github.com/qzhqzh/biomed/pull/9) 合入 `main`，合并提交为 `d7f204e`；分支推送、PR 和合并后的主线 CI 均通过。
- 临时整合分支 `codex/biomed-consolidation-20261015` 和原重构分支 `refactor/architecture-django-drf` 的本地、远端引用均已清理。原重构分支内容已通过 [PR #1](https://github.com/qzhqzh/biomed/pull/1) 完整 squash 合入主线。
- 保留本地快照分支 `codex/biomed-snapshot-20261015`（`2b020ce`）。源码、Git 历史、数据库与本机配置备份位于 `/data/06_project/biomed/var/backups/git-consolidation-20261015T125759Z`，该目录不进入 Git。
- `repository-after.bundle` 包含快照及原重构分支的完整历史；`github-integration-result.json`、`refactor-branch-cleanup.json` 保存后续合并和清理核对结果。详见[整合记录](docs/consolidation-2026-10-15.md)。

实体瘤采集、分类与知识提取等功能在上述整合提交之后持续迭代；研究用原始资料、数据库及备份保留在本机。

## 菜单大纲

知识库依次为基因（含转录本）、疾病、药物。证据库包括临床证据、文本资料（文献管理、指南管理）。工作流位于管理板块；生信工具保留独立分组。

指南管理：[局域网入口](http://192.168.124.2:50011/evidence/sources/guidelines/)。旧页面地址继续可用，原文件、实体和任务数据不迁移、不复制。

## 已实现

当前文献知识工作流以**符合tumor_api要求的可用知识产出**为主目标；保存量、结构合格量另计，不代表目标可用。当前基线及验收口径见[目标利用率与验收](docs/knowledge-processing.md#以目标可用产出为验收标准2026-11-06)。

[030 · 新版后台首批实测](docs/research/030-continuous-clinical.md)：2篇尝试、1篇提取，保存17条，目标兼容1条；同实际输出旧适配0、新适配1。该条复现已有SS18诊断知识，净新增0条。随后按用户要求放开选文门槛，将现有50篇待处理全文纳入[夜间扩大测试](docs/knowledge-processing.md#夜间扩大测试2026-11-07)，每批5篇自动接续；进一步把累计全文尝试预算由100扩大至2,000，新的合法全文解析后自动补入知识处理队列。

- **常驻知识处理**：独立宿主机服务自动领取已有及新解析的JATS全文，复用4B分类、9B段落／实体、Bonsai关系与本地归一，每批5篇、单GPU串行，资源空闲时持续处理，不设每日篇数或时长上限；支持断点、有限重试及邮件告警，网络限速保持不变。[工作流](http://192.168.124.2:50011/management/workflows/)展示真实批次与独立抽查，计划、边界及操作见[常驻处理说明](docs/knowledge-processing.md)。上游全文采集按独立预算及PMC允许时段运行；只写biomed，不写tumor_api生产。
- **知识兼容修复**：两篇固定问题论文验证形成3条核心知识、2条通过目标离线兼容，同输入旧方案为1条。修复及真实产物见[028研究记录](docs/research/028-target-optimization.md)，不与原常驻批次统计混算。
- **五篇目标产出对照及接入**：同5篇、6条关系，独立兼容知识2→4，净新增2条诊断证据；关系推理76.45→82.53秒。[029研究](docs/research/029-target-yield.md)保留当时结果；2026-11-07已将6条候选保存至biomed，并把诊断抽取/适配与分子临床选文接入后台，补齐同9B的诊断段落筛选。[当前运行边界](docs/knowledge-processing.md#当前边界)明确不写tumor_api、旧知识不重复计新增。
- **标准库**：NCBI Gene / MANE 的基因、转录本及标识查询；MONDO 疾病与 RxNorm 成分（IN）真实数据，来源、版本、同义词和更新任务可查。
- **转录本结构**：独立保存完整版本转录本的可信外显子总数、核验依据及各基因组位点的原始映射，支持批量获取、断点复用和来源追溯；未核验计数保持为空。见[字段语义与批量补齐](docs/standard-resources.md#transcript-structures)。
- **生信原始文件管理**：在 `/appdata/02_resource/databases` 按基因组和发布版本统一管理 RefSeq、参考 FASTA、MANE、ClinVar，记录来源与校验值，重复获取优先复用；实际已收录文件以 manifest 为准。见[目录、版本与下载约定](docs/reference-databases.md)。
- **疾病与癌种分类**：MONDO 多父级分类浏览；新增 OncoTree 固定版898个节点，覆盖组织分组及实体瘤、血液肿瘤亚型，保留原始来源与交叉引用。见[资源盘点、官方来源和药物扩展方案](docs/standard-resources.md)。
- **药物扩展与双语**：ChEMBL 37机制子集已收录7,561条机制、6,816个分子和1,518个靶点，保留条件、来源与版本；与RxNorm的名称关联仍标记候选。RxClass FDASPL/EPC已完整启用762类、2,394条直接关系，关联2,231个本地成分；药物页面提供真实的“类别→成分”两层树与全部成分检索。762类均有中文项目草稿；药物中文已覆盖2,779/14,710个成分。癌种新增722条v2译名已导入，中文覆盖898/898个节点（119条参考、779条草稿），原176条中文保留。癌种和药物仅点“详情”才打开弹窗；页面说明参考中文与项目草稿的来源差异。名称覆盖不代表翻译准确率或官方确认，详见[资源状态与覆盖](docs/standard-resources.md)。
- **证据库 / 文本资料**：文献管理、指南管理分别提供入口，其他资料继续按类型归档。指南目前仅保存原文，详情提供解析方案草案、当前版本状态与处理记录；知识结构确定后再安排解析。PubMed 检索与 PMID 导入，自动尝试获取 PMC 允许复用的全文；上传论文、指南等合法 PDF，保存不可覆盖的原文件版本及 SHA-256。
- **文本解析**：文本 PDF 按页定位、PMC JATS 按段定位。未取得全文和扫描 PDF 不会伪装成成功提取。
- **实体瘤批量采集**：按发表日期估算候选规模，再启动有预算的批次；日期分片、固定 PMID 清单、断点续跑、跨批次去重和版本化多标签规则分类。默认每批 50 篇元数据、5 次开放全文尝试，单路请求、2 秒间隔、256 KiB/s 读取上限；采集完成自动暂停供评估。见[使用与实测记录](docs/literature-collection.md)。
- **工作流**：独立 worker 执行持久任务，临时采集错误按 1 / 5 / 15 / 30 / 60 分钟等待重试，连续失败 6 次后暂停并保留断点；异常暂停发站内告警和邮件，本次范围扫描完成另发完成邮件。执行中断按租约恢复，手动暂停不会自动续跑或发故障邮件。标准库分批暂存，完整校验后切换当前版本。见[恢复与通知](docs/literature-collection.md#recovery-and-notifications)。
- **证据与审核机制**：候选必须精确引用原文，标准实体 ID 须得到确认。AI 通过即可发布；人工抽查需登录，记录账号与原因，发现问题可撤回，历史保留。
- **提取知识库**：[知识入口](http://192.168.124.2:50011/knowledge/candidates/)支持癌种＋实际干预＋分子条件的同条关系AND检索，并复用已确认的本地标准名、别名和主癌种。文章版本、原词和精确段落出处保留，提取、结构分流、目标兼容与独立质量状态分开。[024单篇闭环](docs/research/024-minimal-closure.md)已保存26个入选片段，23条形成医学关系、3条待处理；50处癌种／药物参与引用已归一，1,194处定位可回查。原始、修订及历史研究版本均保留，不能把版本记录数当作唯一知识数；不自动写入生产项目。
- **本地知识批次流程与导出**：`scripts/run_minimal_pipeline.py run`串联已筛选段落、9B实体、Bonsai关系、脚本定位/组装/去重和目标适配包；`--pmid`限定文章，`--import-to-local`接入本地保存及实体归一。已有输入和同批导入可重放，`reassemble`可从原模型输出追加脚本修订版本，不重跑模型。质量结果仍独立通过`--quality-only`保存。见[024实际规则与边界](docs/research/024-minimal-closure.md)和[离线导出约束](docs/tumor-api-export.md)。目标分子触发条件等覆盖缺口独立保留，适配包不等于tumor_api导入就绪。
- **PubTator独立参考**：新知识提取批次默认缓存入选文章标注，9B实体和Bonsai关系照常运行。外部原词、ID候选和出处单独保存，不覆盖本地结果；单路、2秒间隔、256 KiB/s，故障不阻断原流程。9篇实测首次2次请求，缓存复用0次请求。见[规则、缓存路径与使用方式](docs/pubtator-integration.md)。
- **十篇批量闭环**：`scripts/run_literature_batch.py`接入4B分类、9B段落／实体、Bonsai关系、本地归一及离线导出，支持断点恢复。[025实际结果](docs/research/025-ten-paper-pipeline.md)：主批10篇中9篇进入提取，182条记录中160条结构合格；两篇独立对照另计，合计210条已保存到biomed。1条通过目标离线结构检查，已发现的数值人群问题独立展示；未做全批语义评分、不导入生产。[研究核对页](http://192.168.124.2:50011/management/research/?experiment=ten-paper-pipeline-20261102)保留逐条状态、目标字段和文章／段落出处。
- **目标兼容核对**：[022核对页](http://192.168.124.2:50011/management/research/?experiment=target-compatibility-20261101)逐条展示30条知识、真实目标ID、映射提议和原文。89个参与引用中16个唯一原名匹配、31个主癌种对应提议；整体为26条当前目标不能表达、3条条件/实体待明确、1条目标草稿字段冲突，0条导入就绪。仅生成独立核对包，tumor_api源码和生产数据未修改。
- **本地实体库**：[药物、癌种／疾病及待审规则](http://192.168.124.2:50011/knowledge/entities/)以一次性只读快照的501条药物、752条癌种／疾病为底库，随后在biomed内按文献扩展。原ID、来源、组合条目、同名冲突和未定层级保留；脑胶质瘤为主癌种，弥漫性中线胶质瘤为亚型，部位和分子限定另存。已确认规则可复用，原始知识与出处不改写；稳定运行不再拉取tumor_api。方法及本轮模型对照见[023 · 实体归一](docs/research/023-entity-resolution.md)。
- **已确认药物自动补齐目标库**：缺少目标条目时自动生成查重/新增操作，保存返回ID并绑定知识，不设逐药人工审批。[隔离验证](docs/tumor-api-export.md#已确认药物自动新增2026-11-06)已自动新增7种药物、回填94处引用；重跑新增0、复用7。原文及其他知识缺口保留，生产写入连接尚未接入，未修改tumor_api。
- **每批自动实体归一**：新名称统一经过已有规则、标准字典及9B判断，自动生成本地实体和可复用别名；完整批次入口已接通，单个不确定项不阻断其他名称。下一批读取当前biomed实体库，沿用原文和两层出处，见[通用流程与运行方式](docs/entity-normalization.md)。
- **实体归一评价口径**：疾病以主癌种是否对上为主要正确性指标，亚型及其他限定另记信息完整度，药物身份单独评价。[023业务口径复评](http://192.168.124.2:50011/management/research/?experiment=entity-resolution-business-20261101-v2)保留历史评分；后续受版本保护的修复批次已将原18处待定引用落到实际规则，原知识、出处及人工审核不覆盖。
- **已发布文献知识**：[知识检索](http://192.168.124.2:50011/knowledge/search/)按核验过的癌种、药物、标志物做AND查询；[018](docs/research/018-knowledge-publication.md)同15组新增Astra核验，11条已发布、4条未发布。AI通过即可发布、人工抽查独立，英文原文与文章/段落出处保留；尚未接入全库自动推理或tumor_api报告。
- **既有能力**：变异注释、任务历史、氨基酸映射、教学病例核对以及问题记录保留在同一前端。

**旧版Evidence自动worker的模型后端尚未配置，因此该正式证据库目前为空。** 新常驻知识服务已复用真实4B、9B及Bonsai处理已有全文，与旧worker的“待配置模型”状态分开；按资源空闲情况持续处理，尚未接入tumor_api生产。文献分类实验不替代采集的规则默认分类。传统AI通过发布机制保持兼容，新提取知识不以语义核验通过作为保存和检索门槛。扫描件OCR、复杂表格恢复、定时文献订阅、临床指南自动下载授权尚未接入。

当前入库的 MANE 开放论文（PMID 35388217）用于验证真实来源链路，不是治疗建议或已发布临床证据。用户提供的 NCCN Breast Cancer Version 6.2026（278 页）作为指南示例仅归档，未启动解析或 AI 抽取；见 [指南入库与后续解析](docs/guideline-ingestion.md)。教学区继续保留 7 条明确标记的样例与 3 个虚构病例。

本机多文件存储：生产数据库与文献、前端依赖、开发Python环境、实验目录及新工具缓存已使用 `/appdata/02_resource/apps/biomed/`，项目内保留兼容路径；源码仍以本目录为唯一来源，共享模型保持 `/data/models`。实际路径及开发命令见[多文件存储约定](docs/DEPLOY.md#开发与研究的多文件存储2026-10-23)。

## 启动

```bash
cd /data/06_project/biomed
docker compose up -d --no-build
docker compose ps
curl --fail http://127.0.0.1:50011/health/ready
curl -I http://127.0.0.1:50011/
```

根 Compose 管理 `web`、资料 `worker`、`annotation-worker` 与 `oncology`。变异注释任务由独立进程从原任务表认领，Web 进程重启后仍可恢复。只有 web 发布端口；专用网络仍为 `10.254.220.0/28`。当前服务器的完整 BuildKit 构建问题与已验证的镜像依赖层构建方式见 [部署说明](docs/DEPLOY.md)，不要重启共享 Docker 或清理其他项目网络。

当前工作台账号为 `biomed`，凭据仅保存在 `var/private/public-biomed.txt`（文件权限 600）；原 `biomed-reviewer` 账号仍可使用。用户登录后可使用全部工作台功能，均没有 Django 管理员权限。新增个人账号和 HTTPS 配置见[部署说明](docs/DEPLOY.md#身份与配置)。会话签名密钥保存在忽略提交的 `.env` 中。

## 目录

```text
biomed/
├── apps/
│   ├── curation/              标准实体、资料、原文定位、证据审核、工作流
│   ├── annotations/           既有生信计算与 NCBI/MANE 数据查询
│   ├── portal/                Django 页面入口与 Vite 清单加载
│   └── evidence/              教学 API 的同源兼容网关
├── frontend/                  唯一 React + TypeScript 界面
├── config/                    Django 配置与 API 路由
├── services/oncology/          内部教学 API，无第二套前端
├── deploy/                    当前服务器可重复的运行时依赖层构建
├── docs/                      产品架构、部署、验证、经验与迁移记录
├── requirements.txt           后端依赖范围
├── requirements.lock          后端固定版本安装清单（uv 生成）
├── data/knowledge*/           基因/MANE 初始及不可变更新版本
├── data/vep → ../var/reference/vep
├── data/annovar → ../var/reference/annovar
├── legacy/                    只读历史背景，不部署
├── var/
│   ├── oncology/lessons.sqlite3 历史问题与经验
│   ├── reference/             既有 VEP/ANNOVAR 大型参考缓存
│   ├── private/               私有账号凭据，不进入 Git
│   └── backups/               迁移及迭代前备份，不进入 Git
└── docker-compose.yml         唯一部署入口
```

## 生产数据与采集恢复（2026-10-22）

2026-10-22 17:36（北京时间）核对时，采集已从 133,207 篇恢复至 **133,707 篇元数据**，后台继续运行，连续失败 0 次。该数字是运行中快照，完整记录见[采集说明](docs/literature-collection.md)。

`/data` 虽有空闲容量，但 inode 已接近耗尽。生产 Django 数据库和原始资料改用 `/appdata/02_resource/apps/biomed/production/var/django`、`/appdata/02_resource/apps/biomed/production/var/library`；由本机 `.env` 的 `DJANGO_HOST_DIR` / `LIBRARY_HOST_DIR` 明确指定，不建立旧路径软链接。源码、教学数据库、私有配置、参考库和备份仍在本项目原位置。`/appdata` 使用 `/data` 上的磁盘镜像，仍须同时监控底层物理剩余容量，不能将它当作独立备份。

本次修复缩短采集写事务、增加 DOI / PMCID 查询索引，并让空闲注释 worker 避免占写锁。临时数据库错误等待后重试，整页失败保留断点。独立主机监控每 2 分钟检查存储、心跳及进度，按限定次数恢复已知故障；尊重来源冷却、手动暂停和既有限速。不能安全恢复时停止并邮件通知 `已配置的收件邮箱`，完成也发邮件。部署与排障见[部署说明](docs/DEPLOY.md#collection-watch)。

修复前原库和原始资料保留在 `var/backups/literature-db-repair-20261022T084748Z/`，包含数据核对、代码、配置和测试记录；原快照分支与既有备份继续保留。

## 开发和验证

```bash
cd /data/06_project/biomed
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.lock -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build
# 页面测试读取真实 Vite manifest，因此先构建前端。
.venv/bin/python manage.py test apps.curation apps.annotations apps.evidence apps.portal
DJANGO_DEBUG=1 DATABASE_PATH=/tmp/biomed-dev.sqlite3 CURATION_STORAGE_DIR=/tmp/biomed-dev-library .venv/bin/python manage.py migrate
DJANGO_DEBUG=1 DATABASE_PATH=/tmp/biomed-dev.sqlite3 CURATION_STORAGE_DIR=/tmp/biomed-dev-library .venv/bin/python manage.py runserver 127.0.0.1:8000
# 另一个终端，使用同一组开发数据库/存储环境变量：
DJANGO_DEBUG=1 DATABASE_PATH=/tmp/biomed-dev.sqlite3 CURATION_STORAGE_DIR=/tmp/biomed-dev-library .venv/bin/python manage.py curation_worker
# 再开一个终端，使用同一组环境变量：
DJANGO_DEBUG=1 DATABASE_PATH=/tmp/biomed-dev.sqlite3 .venv/bin/python manage.py run_annotation_worker
```

教学模块继续使用自身 `uv.lock`，见[模块说明](services/oncology/README.md)。旧 [架构草案](ARCHITECTURE_PLAN.md) 仅是原生信项目背景；当前设计以 [PRODUCT.md](PRODUCT.md) 和 [产品架构](docs/product-architecture.md) 为准。

本轮交付、真实来源校验、浏览器路径与已知限制见 [本轮验证记录](docs/validation-2026-10-12.md)。

## 下一阶段

用户已确定优先覆盖**全部实体瘤，先完成近五年文献采集与分类，并保留历史回填**，再推进有出处的结构化知识提取和垂直小模型实验。2026-10-16（北京时间）11:09:58 官方复查得到 **1,135,415 条候选**，当时与本库 PMID 的交集为 5,000 条，尚余 1,130,415 条不重复候选。按用户后续要求，11:29:16 保存全量请求，**11:39:19 从 6,300 条断点恢复，目标改为采集本次日期范围内全部候选**，不再停在 10 万条。元数据快照已完成，后续增量同步尚未执行；全文累计尝试上限已由 100 提高至 2,000，达到上限后暂停。当前数量以[文献管理实时页面](http://192.168.124.2:50011/evidence/sources/papers/)为准；采集限速保持单路、2 秒间隔、256 KiB/s。查询范围、计数时间、全量启动时间和后续增量回查起点均已保存。详见[全量采集与时间记录](docs/literature-collection.md#full-scope-20261016)。

已在原有开发样本中固定 20 篇，用本机 Qwen3-1.7B 对照两版提示词，结果不足以替代规则，因此仅保留实验候选；详见[并行续采与模型试跑](docs/literature-model-pilot.md)。本轮新增元数据同时保留原批次规则和 v3 规则，未扩大模型试验。原有 **100 篇助手暂定评测集（60 开发 / 40 原保留）**仍保留，不是人工金标准；后续改规则或调提示词需另设未使用的保留集，见[分类评测记录](docs/literature-evaluation.md)。原 PubMed 搜索仍提供前 10 条预览；定时增量订阅、模型训练及向量检索尚未实现。

分阶段安排、设备核对、知识库架构建议及近期研究依据见[产品架构中的下一阶段讨论稿](docs/product-architecture.md#solid-tumor-roadmap)。该方案区分已确定的优先级和待实验的技术选择，不代表已启动训练或完成数据库迁移。

已补充[文献多环节分类设计](docs/literature-classification-pipeline.md)，细分资料状态、发表状态、相关性、癌种、研究对象、研究设计、主题与后续分流。分类推理命令、原文引用校验与实验版本追加已实现，训练未启动；各环节仍需独立评测。

2026-10-22 新增[文献知识预提取试验](docs/evidence-extraction-pilot.md)：统一使用包含实体瘤材料的 **30 篇开发测试集**。离线脚本完成来源冻结、断点恢复、规则段落定位，以及本地模型与 Astra 独立参考的分类对照；模型参考不是人工金标准。本轮未训练、未生成正式证据，未修改 tumor_api 或采集限速。

研究经验已持久保存到[研究历程页面](http://192.168.124.2:50011/management/research/)，登记与复用见[研究记录说明](docs/research-history.md)。[001 · 三分类基线](docs/research/001-three-class.md)已在统一 30 篇测试集实测：Qwen3-1.7B 与 Astra 确定参考一致 23/29，另 1 篇参考未确定；少数类不足，尚不能自动筛选。页面展示规则 v3 的实际正则、排除条件和逐篇命中。后续研究按独立文档保存关键结论，不覆盖历史实验。

2026-10-29：[015 · 概括辅助抽取](docs/research/015-summary-assist.md)未显示足以默认加入的收益，当前研究流程直接从原文抽实体和关系，取消独立概括与自由statement。输入准备使用`scripts/prepare_knowledge_workflow.py`，原文与两层出处保留；此入口不运行模型或写入生产知识。

2026-10-30：[016 · 原文抽取简单调整](docs/research/016-source-refinement.md)采用两条有原文依据的实体类型规则，核心13→15/15，约0.06秒、零模型调用；关系提示调整未稳定改善，继续原无概括9B提示。类型整理模块为`apps/curation/source_entity_types.py`，研究入口为`scripts/refine_source_entity_types.py`，不改原文或自动写入生产。

[002 · 模型选择与规模对比](docs/research/002-model-comparison.md)已完成相同30篇、未微调的五模型实测：GLiNER 25/29、1.7B 23/29、4B 27/29、8B 26/29、9B 27/29。4B与9B总分相同，显存峰值分别为9.28与18.34GiB；网页保存逐类结果、耗时和逐篇得失。下一轮以4B为主候选，先补少数类与留出集，再评估规则协同。

[003 · 100篇三分类与资源对照](docs/research/003-model-comparison-100.md)已完成同五模型扩展测试：GLiNER 55/95、1.7B 63/95、4B 88/95、8B 87/95、9B 92/95；另5篇Astra参考未决。9B比4B多对4篇，实体瘤识别52/54对47/54，显存18.62对9.42GiB。新增70篇单列，未微调；当前保留9B效果候选与4B资源候选。

模型权重、tokenizer、配置与模型下载缓存统一使用 `/data/models`，下载前核对并复用已有版本；模型下载**不限速**（2026-10-30 用户更新），不改变文献采集限速。持久约定见 `/data/01_profile/MODEL_STORAGE.md`，本机迁移与服务恢复记录见 `/data/models/MIGRATION-20261022.md`，私有配置备份位于 `/data/01_profile/model-migration-20261022/`。实验输入、原始输出与运行环境分别保存，不复制模型权重到项目目录。
