# biomed 整合、主线合并与清理记录（2026-10-15）

## 当前结果

整合成果已通过 [PR #9](https://github.com/qzhqzh/biomed/pull/9) 合入 `main`，合并提交为 `d7f204e6b11c6006814a197d44ee79d4ee8608ab`。合并后本地 main 已与 origin/main 同步；本次记录描述已完成的整合和清理，不把历史开发分支作为当前主线。

| 项目 | 最终状态 |
| --- | --- |
| `main` | 已包含统一工作台及原重构成果 |
| `codex/biomed-consolidation-20261015` | 本地和远端均已删除，原头提交 `15dc56e` 已通过 PR #9 合并 |
| `refactor/architecture-django-drf` | 本地和远端均已删除，原头提交 `cc3d2de` 的内容已通过 PR #1 squash 合并 |
| `codex/biomed-snapshot-20261015` | 保留本地快照，提交 `2b020ce98278e4864fb4360c0ab776d0f5aebf76`；未推送该快照分支 |
| 备份 | `/data/06_project/biomed/var/backups/git-consolidation-20261015T125759Z`，已有文件全部保留，不进入 Git |

## 来源与保存

唯一活动项目仍为 `/data/06_project/biomed`。原 `online-biotool`、`oncology-workbench` 和旧参考库目录已于 10 月 12 日移出，不重新建立旧目录或部署入口。

- 保存前本地 HEAD 为 `d0cafd2`，有 59 个已修改文件和 100 个未跟踪文件。
- 保存提交为 `2b020ce`，分支 `codex/biomed-snapshot-20261015`。业务数据、原始资料、参考库、私有凭据和本地代理状态均不进入提交。
- 备份在 `var/backups/git-consolidation-20261015T125759Z`：源码归档、二进制 diff、已验证 Git bundle、两个一致性 SQLite 快照和受保护的本机配置备份。
- 两个数据库备份均通过 quick_check。Django 共 21 张业务/框架表；教学问题库 1 张表、1 条历史问题。未删除迁移、参考数据、原文件或已有备份。

## 保存时的远端差异与整合

整合前 fetch 确认：当时 `origin/main` 为 `76683dd`，`origin/refactor/architecture-django-drf` 为 `cc3d2de`。远端重构分支比保存前的本地分支多 18 个提交；这些内容已 squash 合入 main，两个远端分支当时的代码树相同。

整合分支 `codex/biomed-consolidation-20261015` 直接基于最新 main，并应用当前统一工作台成果。保留以下远端行为，同时沿用 biomed 领域和持久路径：

- AnnotationJob 由独立 worker 认领、执行和恢复，不依赖 Web 内的后台线程。
- 知识库检索索引、数据和版本一起原子发布；任务固定同一快照，执行期间更新不会改写结果中的知识库版本。
- accession 主编号查询保留“版本号未核验”的语义。
- 分页参数边界、轮询重试、提交冷却、VEP 非法输出处理与下载 PID 清理修复保留。
- 非调试启动要求密钥；主机白名单和代理信任显式配置。现用 `DATABASE_PATH` 优先，兼容旧 `DJANGO_DB_PATH`。
- CI 先生成前端 manifest，再检查所有 Django 模块；肿瘤模块沿用 uv.lock 的独立测试。

整合提交为 `b7a31c9`，随后由 `15dc56e` 补充运行和清理记录。最初完成本地保存后，按后续授权推送整合分支、创建 PR #9，并在检查通过后合入 main；整合临时分支已清理。

## GitHub 合并与旧分支清理

- 分支推送 CI [38745261738](https://github.com/qzhqzh/biomed/actions/runs/38745261738)、PR CI [38745408386](https://github.com/qzhqzh/biomed/actions/runs/38745408386) 和合并后主线 CI [38745682707](https://github.com/qzhqzh/biomed/actions/runs/38745682707) 均通过。PR #9 采用 merge commit，已验证合并后的代码树与通过检查的整合分支一致。
- 删除原重构分支前重新 fetch，并核对 [PR #1](https://github.com/qzhqzh/biomed/pull/1) 已合并。原头 `cc3d2deab6718cd44967430e90199811bc8d0b01` 与 squash 提交 `76683dd87feced80b009a67cbf09221853675165` 的完整代码树均为 `0e045122604741d570fe0645a7964b5609ffbb37`；后者是当前 main 的祖先。分支没有后续新增内容或未关闭 PR。
- 由于 squash 合并不会保留原分支提交的祖先关系，清理依据是已合并 PR、完整代码树相等和后续主线继承关系，不能仅用 `git branch --merged` 判断。删除的仅是本地和远端分支引用。
- `repository-after.bundle` 已通过验证，并保有原重构分支及快照分支的完整 Git 历史。快照分支提交和清理前已有的 30 个备份文件均未改变；数据库、原文和参考库未参与分支清理。
- 同一备份目录中的 `github-integration-result.json`、`refactor-branch-cleanup.json` 保存精确提交、CI、分支核对与清理结果。清理完成时仅保留本地 main 和快照分支，工作区干净；后续文档或功能开发继续使用新的短期分支。

## 运行兼容

Compose 保留 web、资料 worker 和内部 oncology API，新增独立 annotation-worker；对外仍只有 50011。现有 SQLite、资料、知识库及参考缓存全部沿用原 bind mount，不创建替代数据库或 Docker 数据卷。

本机镜像不含本地 ANNOVAR 程序。为保留既有依赖，本机 `.env` 显式选择 `ANNOVAR_MODE=docker` 和 `deploy/docker-compose.engines.yml`；后者只为需要引擎访问的进程挂载既有 Docker socket。授权开关仍为 0。默认新安装配置不自动授予 Docker socket。

复用现有 `biomed-web:curation-20261012`、`biomed-web:runtime-20261012`、`biomed-oncology:local` 及现有 ANNOVAR 引擎镜像，不触发已知有故障的宿主机完整 BuildKit 构建。

## 验证

- 保存前：78 项 Django 测试、15 项肿瘤模块测试及前端构建通过。
- 同步后：94 项 Django 测试、前端构建、Python 定向静态检查、下载脚本语法、默认及引擎 override Compose 配置验证通过。
- 新增回归覆盖：更新时固定别名索引、注释结果版本、数据库配置优先级、缺失密钥拒绝启动。原 ANNOVAR 单元测试改为显式提供 MANE 夹具，避免依赖运行机私有下载目录。
- Browser plugin not available，复用已安装的 Playwright/Chromium。临时环境仅绑定 127.0.0.1，使用独立数据库；EGFR/MANE、accession 版本提示、虚构示例提取→确认→出处、修改条件清除确认均通过。
- 1440×960 与 390×844 页面正常，未发现相关控制台错误、框架错误覆盖层或横向溢出。指南管理、工作流、任务历史页面均非空并返回 200。
- 不把上述检查当作 VEP 实算问题已修复，也不启用 ANNOVAR、模型或真实患者数据处理。

## 清理和最终运行状态

- 删除空闲构建器 `online-biotool-proxy`，使用 `docker buildx rm --keep-state` 保留 `buildx_buildkit_online-biotool-proxy0_state` 缓存卷。清理前确认 7 条构建记录均已结束，保存了构建器配置和历史；未清理其他构建器。
- 删除旧标签 `online-biotool-web:latest`。它与 `biomed-web:runtime-20261012` 指向同一镜像，后者及其镜像 ID 保留。
- 删除没有容器引用的旧镜像 `oncology-workbench:0.1.0`。现用 `biomed-oncology:local` 是不同镜像，保持不变。
- 现用 curation/runtime、oncology 和 ANNOVAR 四个镜像 ID 均与清理前一致；所有已有 Docker volume 均保留，没有执行全局 prune。数据、备份、专属参考库及 `legacy/` 继续保留。
- 本地服务复用已有镜像，加载整合后的代码和前端静态文件。web、资料 worker、oncology 均为 healthy；独立 annotation-worker 正常运行，不使用 Web HTTP 健康探针。
- 局域网 `http://192.168.124.2:50011/` 与本机 `http://localhost:50011/` 的首页、`/health/ready` 均返回 200；旧端口 18765 关闭。相同桌面和手机浏览器流程已在局域网实际入口再次通过。
- 最终两个数据库 quick_check 均为 ok；Django 21 张表共 47,052 条记录、教学问题库 1 条记录，各表数量与操作前一致。
- Docker 清理结果、最终服务和数据库状态保存在上述备份目录；`qa/` 保存局域网浏览器结果及三张截图。临时验证服务已停止，不保留第二套活动工作目录。
