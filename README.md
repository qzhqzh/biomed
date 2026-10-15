# biomed

以真实标准实体、可追溯临床证据和资料处理工作流为核心的生信与医学知识库。唯一项目目录为 `/data/06_project/biomed`，沿用原 online-biotool 的 Git 历史，远端为 https://github.com/qzhqzh/biomed。

统一入口：[局域网 http://192.168.124.2:50011/](http://192.168.124.2:50011/)，[服务器本机 http://localhost:50011/](http://localhost:50011/)。18765 已停止对外监听，原教学流程进入统一界面的“教学验证”。

## 菜单大纲

知识库依次为基因（含转录本）、疾病、药物。证据库包括临床证据、文本资料（文献管理、指南管理）。工作流位于管理板块；生信工具保留独立分组。

指南管理：[局域网入口](http://192.168.124.2:50011/evidence/sources/guidelines/)。旧页面地址继续可用，原文件、实体和任务数据不迁移、不复制。

## 已实现

- **标准库**：NCBI Gene / MANE 的基因、转录本及标识查询；MONDO 疾病与 RxNorm 成分（IN）真实数据，来源、版本、同义词和更新任务可查。
- **证据库 / 文本资料**：文献管理、指南管理分别提供入口，其他资料继续按类型归档。指南目前仅保存原文，详情提供解析方案草案、当前版本状态与处理记录；知识结构确定后再安排解析。PubMed 检索与 PMID 导入，自动尝试获取 PMC 允许复用的全文；上传论文、指南等合法 PDF，保存不可覆盖的原文件版本及 SHA-256。
- **文本解析**：文本 PDF 按页定位、PMC JATS 按段定位。未取得全文和扫描 PDF 不会伪装成成功提取。
- **工作流**：独立 worker 执行持久任务，下载失败可重试，执行中断按租约恢复，保留每次尝试。标准库分批暂存，完整校验后切换当前版本。
- **证据与审核机制**：候选必须精确引用原文，标准实体 ID 须得到确认。AI 通过即可发布；人工抽查需登录，记录账号与原因，发现问题可撤回，历史保留。
- **既有能力**：变异注释、任务历史、氨基酸映射、教学病例核对以及问题记录保留在同一前端。

**模型尚未配置，因此正式证据库目前为空。** 文献下载和文本解析已经可执行，后续抽取会停在“待配置模型”。AI 发布规则已实现并经隔离测试验证，没有声称真实模型已经完成审核。扫描件 OCR、复杂表格恢复、定时文献订阅、临床指南自动下载授权尚未接入。

当前入库的 MANE 开放论文（PMID 35388217）用于验证真实来源链路，不是治疗建议或已发布临床证据。用户提供的 NCCN Breast Cancer Version 6.2026（278 页）作为指南示例仅归档，未启动解析或 AI 抽取；见 [指南入库与后续解析](docs/guideline-ingestion.md)。教学区继续保留 7 条明确标记的样例与 3 个虚构病例。

## 启动

```bash
cd /data/06_project/biomed
docker compose up -d --no-build
docker compose ps
curl --fail http://127.0.0.1:50011/health/ready
curl --fail http://127.0.0.1:50011/api/v1/curation/overview/
```

根 Compose 管理 `web`、资料 `worker`、`annotation-worker` 与 `oncology`。变异注释任务由独立进程从原任务表认领，Web 进程重启后仍可恢复。只有 web 发布端口；专用网络仍为 `10.254.220.0/28`。当前服务器的完整 BuildKit 构建问题与已验证的镜像依赖层构建方式见 [部署说明](docs/DEPLOY.md)，不要重启共享 Docker 或清理其他项目网络。

人工审阅账号为 `biomed-reviewer`，初始密码仅保存在 `var/private/reviewer.txt`（文件权限 600）。登录入口位于“临床证据”页；账号仅授予证据抽查权限，没有 Django 管理员权限。会话签名密钥保存在忽略提交的 `.env` 中。

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
│   ├── django/db.sqlite3      任务、实体版本、资料、证据及审核身份
│   ├── oncology/lessons.sqlite3 历史问题与经验
│   ├── library/blobs/         按 SHA-256 保存的资料和来源快照
│   ├── reference/             既有 VEP/ANNOVAR 大型参考缓存
│   ├── private/               私有账号凭据，不进入 Git
│   └── backups/               迁移及迭代前备份，不进入 Git
└── docker-compose.yml         唯一部署入口
```

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

2026-10-15 已保存目录整合成果，并以远端 main 合入持久注释任务、安全配置和检索修复；处理记录见 [整合与清理](docs/consolidation-2026-10-15.md).
