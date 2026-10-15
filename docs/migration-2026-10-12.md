> 本文记录目录迁移完成时的状态。此后同日已完成单一前端与资料审核领域迭代；当前端口、服务及能力以 [统一产品验证记录](validation-2026-10-12.md) 和 [部署说明](DEPLOY.md) 为准。

# biomed 目录与数据迁移记录（2026-10-12）

## 迁移结果

唯一项目根目录为 `/data/06_project/biomed`，唯一有效 Git 根目录为本目录 `.git`，唯一部署入口为根目录 `docker-compose.yml`。两个原项目已归入同一仓库，当前业务仍由 Django/React 生信模块与 FastAPI 肿瘤教学模块分别提供；统一前端和资料审核业务仍待后续迭代。

| 原位置 | 当前位置 | 处置 |
| --- | --- | --- |
| `/data/05_workspace/online-biotool` | 项目根目录 | 同盘移动，保留全部 Git 元数据、未提交改动和本地工具状态 |
| `/data/05_workspace/Work/oncology-workbench` | `services/oncology` | 保留代码、测试、锁文件和历史文档；不创建嵌套 Git |
| `/data/02_resource/workspace/databases/online-biotool` | `var/reference` | 全部专属参考库、旧版本与下载历史一并移动 |
| 原生信根目录 `db.sqlite3` | `var/django/db.sqlite3` | 7 条成功任务保留，补充 Docker bind mount |
| 原肿瘤模块 `var` | `var/oncology` | 1 条问题记录保留；模块内 `var` 为相对链接 |

三个原目录已移出，不保留旧目录软链接；所有运行容器挂载及 Compose 标签都指向新根目录。`data/vep`、`data/annovar` 和历史缓存链接已改为指向新根目录内的相对路径，只有一份参考数据实体。旧实验代码仅留在 `legacy/` 供追溯，不再是部署或开发入口。

## 历史与备份

- 原分支：`refactor/architecture-django-drf`；HEAD 保持 `d0cafd2e34fc8005619f5ff2c150ec2bc439692c`。
- 远端：`git@github.com:qzhqzh/biomed.git`。迁移未新建提交、未推送、未合并远端变更；本地与远端此前存在进度差异，后续同步需单独处理。
- 迁移前 50 个已修改文件与 10 个未跟踪文件已保留；其中 Dockerfile、Compose、settings 在原改动之上补充迁移配置，另外 57 个文件内容逐一校验未变。
- 参考数据共 **37,166 个文件、121,702,917,114 字节（约 113.3 GiB，du 约 114G）**。同一文件系统的目录重命名保留每个原文件 inode、大小、mtime；代码与知识库关键文件另比对 SHA-256。
- 业务数据库执行 SQLite backup 并通过 integrity_check；迁移及容器重建后，所有表的行数与内容摘要与备份一致。界面/API 恢复可读 7 条任务与 1 条问题。

备份目录：`/data/06_project/biomed/var/backups/migration-20261012T033522Z`（权限限制，仅供恢复，不是另一份活动项目）。其中：

- `online-biotool.tar.gz`、`oncology-workbench.tar.gz`：迁移前文件归档，含原 .git、未提交/未跟踪源码、知识库、数据库及本地历史状态；不包含可重建的虚拟环境和 node_modules，不跟随大型参考库软链接。
- `repository.bundle`、`uncommitted-before.patch`、`git-status-before.txt`：Git 与未提交修改的单独恢复材料；bundle 已验证。
- `django.sqlite3`、`lessons.sqlite3`：一致性数据库快照。`container-db.sqlite3` 保存原容器的空数据库，未用它覆盖宿主机的 7 条任务。
- `file-inventory-before.json`、`migration.json`、`verification.json`：迁移清单与校验结果。
- `browser/`：Playwright 检查报告和桌面/手机截图。

约 114G 的参考库采用原位重命名，并未在压缩归档中另复制一份。不要将迁移归档误当成异盘灾备。恢复前先停止对应服务，备份当下新增数据；在临时目录解包检查后再恢复到新根目录，保持本记录的实体路径和容器用户权限，不在旧工作目录重新启动服务。

## 部署调整

1. 两个服务现在属于 Compose 项目 `biomed`，容器为 `biomed-web` 与 `biomed-oncology`。监听端口/范围与迁移前一致。
2. Django 增加 `DATABASE_PATH`，容器使用 `/app/var/db.sqlite3`，宿主机 bind mount 为 `var/django`；启动先运行已有 migrations，避免容器重建后回到空库。
3. 肿瘤模块使用根目录 `var/oncology`，继续只读容器与原网络限制；专用网段保留 `10.254.220.0/28`。
4. 原肿瘤模块独立 Compose 文件已从活动源码移除，原文件保留在迁移归档中；启动说明统一指向根 Compose。
5. 修复迁移后虚拟环境脚本的绝对路径；增加 Docker build context 排除项，避免把数据库、备份、参考库和本地工具状态发送给构建器。
6. 原专用构建器引用已失效的 `127.0.0.1:19080` 代理。默认构建器完成肿瘤镜像及生信前端编译后，Docker 29.3.1 的内置 BuildKit 在 history filter 中 panic；systemd 自动重启 Docker。待引擎恢复后，legacy builder 仍停在镜像元数据提交，已取消该构建。最终复用迁移前运行时镜像并标记为 `biomed-web:runtime-20261012`，以根目录 config、apps、manage.py、导入脚本、参考清单和 staticfiles 的只读挂载运行当前源码；命令和数据库位置由根 Compose 统一控制。前端重新本地编译，collectstatic 生成 162 个静态文件。未重启共享 Docker，也未修改其他项目网络或卷。宿主机构建器问题未在本次项目迁移中修复。

## 验证

| 检查 | 结果 |
| --- | --- |
| Django 现有测试 | 35 项通过；使用隔离数据库 |
| 肿瘤现有测试 | 14 项通过；使用临时数据库 |
| 运行环境与 Compose 配置 | 肿瘤镜像构建通过；生信复用现有运行时，当前源码与新前端静态产物只读挂载；Compose 配置通过 |
| 容器运行与健康检查 | 两容器均 healthy，工作目录均为新根目录 |
| 数据与容器重建 | 7 条任务、1 条问题；数据库全部表内容与迁移快照一致 |
| 基因知识库版本 | `a86b866bdb9a424ab06e94e14cb43f86`；193,887 条基因记录，77,610 个转录本索引键，MANE 1.5 |
| 参考引擎 | VEP GRCh37/GRCh38 缓存就绪，ANNOVAR 继续按原授权开关关闭；就绪不代表实算已通过 |
| VEP 实算限制 | 公共示例 `17:43092951 G>A` 在 GRCh37 下达到 300 秒超时并返回 HTTP 504；GRCh38 未继续实算。引擎源码和参考文件与迁移前一致，超时根因尚未确定，未调整算法或参考版本 |
| HTTP | 两模块 localhost 与 192.168.124.2 均 200 |
| 浏览器页面身份/非空/覆盖错误/控制台 | 通过；无相关控制台错误 |
| 桌面与手机 | 1440×960、390×844 检查并截图 |
| 生信关键路径 | 输入 EGFR，得到基因结果和 MANE `NM_005228` 转录本 |
| 肿瘤关键路径 | 虚构示例→提取→人工确认→检索→出处；改字段失效；未知条件展示；历史问题与完整导出 |
| 旧来源清理 | 三个原目录不存在，运行挂载无原路径，单一 Git 根 |

浏览器工具：Browser plugin not available，因此复用服务器已安装的 Playwright/Chromium；只使用虚构示例和公共基因名，未改写历史记录。未做真实临床资料适用性验证，也未启用 ANNOVAR。

## 工程经验

- 健康接口成功不足以证明数据库已持久化：原生信容器健康，但容器内数据库为空且无挂载，宿主机另有 7 条任务。迁移时先核对所有数据库来源，再选取并挂载完整历史库。
- 目录体积不代表全部数据量：两个代码目录只约 618M，软链接背后另有约 114G 参考库。迁移应检查容器挂载和链接终点，并验证原位移动。
- 构建器配置可能保留历史代理，Docker 内置 BuildKit 崩溃还可能中断共享引擎。本次等待 Docker 自动恢复，以现成运行环境和新根目录代码的只读挂载完成迁移；当前重启应使用 `docker compose up -d --no-build`。宿主机构建器修复或可用独立构建器准备好后再从根 Dockerfile 全量构建。Python 依赖变化需要重建运行环境；前端改动需 `npm run build` 后运行 `manage.py collectstatic --noinput`。
- npm ci 在现有 lockfile 上报告 20 条依赖安全公告（6 moderate、13 high、1 critical）。本次未更换依赖或执行自动 audit fix，需另行评估依赖更新。
- VEP 缓存就绪探测只检查目录与版本，不能替代实际注释。本次公共示例实算返回 504，已记录在备份目录 `annotation-check.json`；当前参数使用 cache 与 hgvs，但未强制 offline，仍需排查运行或外部依赖。迁移保留了原始引擎参数与全部数据，不把 readiness 通过记成计算验收通过。
