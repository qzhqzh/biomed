# 部署、备份与恢复

唯一项目目录 `/data/06_project/biomed`；根目录 Docker Compose 是唯一部署入口。只有 web 的 50011 对外监听。两类 worker 不发布端口，oncology 仅在项目私有网络提供教学 API，18765 已关闭。

## 日常运行

```bash
cd /data/06_project/biomed
docker compose up -d --no-build
docker compose ps
curl --fail http://127.0.0.1:50011/health/ready
curl -I http://127.0.0.1:50011/
```

- 公网：https://biomed.qzhqzh.com/；局域网：http://192.168.124.2:50011/；本机：http://localhost:50011/。当前部署均需登录，日常优先使用 HTTPS。
- `web` 启动先运行向前兼容 migrations，再运行 Gunicorn。`worker` 与 `annotation-worker` 等 web 健康后启动，使用同一数据库；前者处理资料工作流，后者处理 AnnotationJob。
- 默认专属网段 `10.254.220.0/28`，不要清理其他项目网络或参考缓存。
- 生产资料与数据库分别由 `LIBRARY_HOST_DIR:/app/library`、`DJANGO_HOST_DIR:/app/var` 挂载，当前宿主机路径见下文；默认新安装仍可用项目 `var/library`、`var/django`。历史问题库为项目 `var/oncology`。目录需预先创建并由容器 UID 10001 可写；Compose 不自动创建缺失的生产 bind mount。
- 两类 worker 当前各保持单实例。SQLite 写事务采用 IMMEDIATE，批量导入分批暂存，最终原子切换；扩容前迁移 PostgreSQL 并测试认领并发。

## 构建与代码更新

Python 使用 `requirements.lock` 固定版本，前端使用 npm lockfile。修改前端后先构建，再 collectstatic，最后重启 web；页面根据 `.vite/manifest.json` 加载带内容摘要的 JS/CSS，不能给入口额外添加不同的版本查询串。

WhiteNoise 在 Web 进程启动时建立静态文件索引；只构建并执行 `collectstatic` 会使 HTML 引用新 hash，而现有进程仍对新 JS 返回 404，导致页面空白。只改静态资源时可用 `docker compose kill -s HUP web` 平滑重载 Gunicorn 子进程刷新索引，无需重启容器或采集 worker，也不会运行启动命令里的迁移。更新后必须验证新入口 JS 返回 200，并实际打开页面；不能把 `collectstatic` 成功当作发布完成。

```bash
npm --prefix frontend ci
npm --prefix frontend run build
DJANGO_DEBUG=1 .venv/bin/python manage.py collectstatic --noinput
docker compose restart web
```

当前 Compose 只读挂载唯一项目的源码与生成静态资源。正常新服务器可用根 Dockerfile 完整构建。此服务器 Docker 29.3.1 内置 BuildKit 曾因 history filter panic 重启共享 Docker；不要在此服务器直接触发默认 BuildKit 或重启共享引擎。

已验证的本机依赖层构建使用原有 VEP 运行时 `biomed-web:runtime-20261012`，增加锁定依赖 pypdf 和 defusedxml，保持既有 Django/DRF/Gunicorn 版本。2026-10-12 构建结果为 `biomed-web:curation-20261012`，web 与 worker 复用同一镜像。可重复命令：

```bash
cd /data/06_project/biomed
biomed_build_context="$(mktemp -d)"
cp deploy/Dockerfile.runtime "$biomed_build_context/Dockerfile"
cp requirements.lock "$biomed_build_context/requirements.lock"
DOCKER_BUILDKIT=0 docker build -t biomed-web:curation-20261012 "$biomed_build_context"
rm -rf "$biomed_build_context"
docker compose up -d --no-build
```

这只是已知宿主机构建故障的部署兼容路径，不是第二份代码来源。干净构建仍由根 Dockerfile 和同一份 requirements.lock 负责。

## 引擎兼容配置

本地文献分类实验另用短时 CLI 进程，不是 Compose 长驻服务，也没有新增对外端口。当前临时推理解释器为 `var/runtime/decision-venv/bin/python`，通过 `.pth` 只读复用已有 CUDA 12 运行库；正式持续运行前需固化独立依赖，不能直接清理被引用环境。模型与原始试验文件、重跑命令和资源边界见[模型试跑记录](literature-model-pilot.md)。文献分类配置与正式证据抽取的 `CURATION_MODEL_BACKEND` 分开，后者仍未配置。

默认 Compose 不挂载 Docker socket；需要 Docker 模式的引擎时，显式使用 `deploy/docker-compose.engines.yml`。本机原配置使用 `ANNOVAR_MODE=docker` 且保留其运行镜像，因此本机 `.env` 设置：

```dotenv
COMPOSE_FILE=docker-compose.yml:deploy/docker-compose.engines.yml
ANNOVAR_MODE=docker
```

这保留既有 Docker 引擎运行条件，不开启 ANNOVAR 授权开关；`ANNOVAR_PUBLIC_ENABLED` 仍为 0。新安装采用默认 local 模式时，需自行提供合法可用的 ANNOVAR 程序。VEP 继续使用镜像内本地程序及原参考库。

## 身份与配置

- 2026-11-09 起当前部署启用全站登录，公网、局域网页面及 API 均受会话保护；内部健康检查保留。账号 `biomed` 的凭据在 `var/private/public-biomed.txt`，原 `biomed-reviewer` 凭据在 `var/private/reviewer.txt`，文件权限 600。不要将凭据内容复制进文档或 Git。
- 登录用户可使用全部工作台功能及证据抽查，无用户组，也无 Django 管理员权限。没有自助注册；建议为每位使用者创建个人账号以保留操作身份。当前不提供多租户或资料隔离，登录用户能访问全部工作台资料。
- 使用宿主机运行环境创建个人账号：

  ```bash
  cd /data/06_project/biomed
  DJANGO_DEBUG=1 DATABASE_PATH=/appdata/02_resource/apps/biomed/production/var/django/db.sqlite3 \
    CURATION_STORAGE_DIR=/appdata/02_resource/apps/biomed/production/var/library \
    .venv/bin/python -B manage.py create_reviewer --username <name> --credentials-file var/private/<name>.txt
  ```

  命令拒绝覆盖已存在账号或凭据；重置密码使用 Django 的 `changepassword`，保留原审核身份。文件通过私有渠道交付，不在聊天或日志中发送密码。
- 本机 `.env` 使用 `BIOMED_REQUIRE_LOGIN=1`、`BIOMED_ALL_AUTHENTICATED_CAN_REVIEW=1`，并在 `DJANGO_ALLOWED_HOSTS` 和 `DJANGO_CSRF_TRUSTED_ORIGINS` 中加入公网域名。新部署默认为关闭，须显式配置。现有共享网关的 `/home/zhuqin/star/gateway/nginx/conf.d/biomed.conf` 来自 `deploy/biomed-public.nginx.conf`，只匹配该域名；校验完整 Nginx 配置后平滑重载。公网通过 HTTPS 提供安全 Cookie，公开 `/health/` 被网关屏蔽。直接调用 API 的原局域网客户端也需要登录会话。
- 非调试启动必须提供 `DJANGO_SECRET_KEY`，缺失会直接失败；允许主机默认限定 localhost、127.0.0.1 和当前局域网 IP。`.env` 中保存独立 `DJANGO_SECRET_KEY`；不要使用公开默认密钥。浏览器写入校验 CSRF，登录会话 8 小时，密码不写入日志。

| 配置 | 用途 |
| --- | --- |
| `BIOMED_IMAGE` | web / worker 的同一运行镜像 |
| `CURATION_MODEL_BACKEND` | 知识模型适配器类路径；默认空，无外部文本发送 |
| `DATABASE_PATH` | Django 数据库路径，容器内 `/app/var/db.sqlite3`；本地命令兼容旧 `DJANGO_DB_PATH`，两者同时设置时前者优先 |
| `CURATION_STORAGE_DIR` | 不可变原文件目录，容器内 `/app/library` |
| `TRANSCRIPT_STRUCTURE_DIR` | 转录本结构独立发布库；默认 `CURATION_STORAGE_DIR/reference/transcript-structures`，不连接 Django 业务数据库 |
| `DJANGO_HOST_DIR` / `LIBRARY_HOST_DIR` | Compose 宿主机数据库 / 原文件目录，不等同于容器路径 |
| `BIOMED_STORAGE_MARKER` | 生产目录身份标记；数据库父目录和原文件目录均须匹配，否则拒绝启动 |
| `BIOTOOLS_API_KEYS` / `BIOTOOLS_REQUIRE_API_KEY` | 原生信 API 密钥策略，沿用已有语义 |
| `ANNOVAR_PUBLIC_ENABLED` | 默认 0；核实授权后才启用 |

模型适配器参数和发送边界见 [产品架构](product-architecture.md)。配置模型后可以重试 waiting_configuration 任务；不得修改旧审核的模型或提示词版本冒充重新审核。

<a id="transcript-structure-storage"></a>

### 转录本结构独立库

生产宿主机路径为 `/appdata/02_resource/apps/biomed/production/var/library/reference/transcript-structures/`，容器内对应 `/app/library/reference/transcript-structures/`，沿用现有 library 持久挂载。`releases/<id>/structures.sqlite3` 和 `manifest.json` 保存不可变发布版本，`current.json` 原子指向当前版本；`blobs/` 保存按 SHA 去重的原始 GenBank 响应，release 的 `raw/` 是硬链接。备份须同时包含当前指针、发布目录和 blobs，不能只备份 Django `db.sqlite3`。旧发布版本不在日常清理范围。

发布由容器 UID 10001 执行，完整构建、外键与完整性校验成功后才切换；接口按请求固定一个 release 只读查询。导入失败不改当前指针，现有文献数据库、限速与资料 worker 不受影响。累计下载缓存位于 `/appdata/02_resource/apps/biomed/development/transcript-exons-cache/`，采集预算、来源时间语义及导入命令见[转录本外显子结构](standard-resources.md#transcript-structures)。

## 持久任务运维

工作流页展示任务及每次尝试；容器健康检查验证持久心跳。资料 worker 每 10 秒更新心跳，90 秒租约超时可恢复任务，单项子进程最多 10 分钟。网络暂时失败指数退避并最多自动尝试三次；人工重试不删除尝试记录。

人工重启 worker 时未完成任务保留，恢复后按租约和幂等键继续处理。不要通过手改数据库把失败任务标成成功。原始资料按 SHA-256 校验，重复下载/解析不会创建重复版本；标准来源的未完成批次不会成为当前版本。

发现原文关联错误时，使用领域命令隔离具体版本，保留文件及原因，停止关联下游处理并撤回关联已发布证据：

```bash
docker compose exec web python manage.py quarantine_source --version <version-uuid> --reason '填写核对到的具体来源问题'
```

隔离不是删除，界面仍可查该版本的警示和历史。后续应修正来源获取规则、运行回归测试并导入正确版本，不直接把隔离状态改回有效。

变异任务由 `annotation-worker` 单独执行，默认 1 秒轮询，1800 秒后可恢复被中断的 RUNNING 任务；其配置与 web 使用相同引擎、数据库和参考目录。该容器不使用 Web 镜像自带的 HTTP 健康检查，进程异常退出由 restart 策略恢复；运行状态与任务结果需分别核对。

## 备份与恢复

一次完整备份至少包含：

- `DJANGO_HOST_DIR` 与项目 `var/oncology` 的一致数据库快照。
- `LIBRARY_HOST_DIR` 的所有原文件与来源快照。
- `data/knowledge`、`data/knowledge-updates` 的完整版本及 current 指针。
- `var/reference` 大型参考库或可恢复的原始包/清单。
- `.env`、`var/private` 需单独保密备份，权限不放宽。

对资料与数据库的组合备份，先暂停 web / worker 写入，或使用已有不可变文件加一致性快照方案。数据库使用 SQLite backup API 或停止服务后复制完整目录；不能仅复制活动数据库主文件而遗漏 WAL。恢复必须同时核对版本记录引用的文件 SHA-256。

既有迁移备份位于 `var/backups/migration-20261012T033522Z`，本轮前端合并前备份位于 `var/backups/unified-ui-20261012T051024Z`，新领域表迁移前备份位于 `var/backups/curation-20261012T060136Z`。备份不是第二个开发项目；原 05_workspace 两个项目目录及旧参考库目录已经移除。

回滚业务代码时保留新增表、原文文件和审核记录，不反向删除 migrations 或覆盖运行库。需要完整恢复旧数据库时先停写，并保存当前版本作为新的备份。

### 本地实体库增量部署（2026-11-01）

`0012_entity_registry`只增加五张实体登记表。现用大SQLite库可执行`docker compose exec -T web python - < scripts/apply_entity_registry_schema.py`：脚本只允许这一个向前迁移，检查旧schema不变，并把该次迁移的外键检查限定在新表，避免扫描全量文献库；不改变后续连接的约束检查。不重启资料worker，不反向删除新表。

冻结导入文件位于`var/experiments/entity-resolution-20261101/`（实际存储在appdata）。`bootstrap-v2.json`是一次性基础底库，`literature-registry.json`是本批文献扩展；通过`docker compose exec -T web python manage.py import_entity_registry --input /dev/stdin < <文件路径>`导入，可先加`--dry-run`。同批同内容重复执行复用；基础库封存后拒绝另一批bootstrap，后续只接受本地literature扩展和有版本的审核。没有运行时目标库凭据、拉取或生产导入步骤。静态文件更新后按上文collectstatic和HUP验证页面；人工审核使用现有审阅账号，不改变账号权限。

业务口径v2的受保护修复包为`operational-registry-v2.json`，已实际导入并验证重放。规则更新和候选激活要求旧内容哈希及revision，拒绝覆盖人工审核；无新增迁移。`import_knowledge_candidates`成功或重放后自动登记参与实体并应用本地规则，注册失败时重放相同导入即可继续，不重新推理。旧知识使用`docker compose exec -T web python manage.py register_knowledge_entities --chunk-size 25`分批回填，可用`--source-run`限定；只追加派生关联，不重写fact、出处或审核。

## 生产数据位置与锁竞争修复（2026-10-22）

当前本机 `.env` 固定以下非秘密配置，其他凭据保持原样：

```dotenv
DJANGO_HOST_DIR=/appdata/02_resource/apps/biomed/production/var/django
LIBRARY_HOST_DIR=/appdata/02_resource/apps/biomed/production/var/library
BIOMED_STORAGE_MARKER=biomed-production-20261022
```

`/appdata` 必须是 UUID `bab3ba24-622e-4023-b867-5c4ed1755aa3` 的读写挂载，两目录必须各有同值 `.biomed-storage` 文件。新目录 UID 10001 可写；不通过 chmod 放宽私有配置或旧文件权限。容器内路径保持不变，优先通过 `docker compose exec -T web python manage.py ...` 运维；宿主机命令必须显式指定相应 `DATABASE_PATH` / `CURATION_STORAGE_DIR`，不能使用旧目录默认值。

修复前停止三个 Django 写入进程；旧库 `quick_check=ok`，新库迁移前与旧库 SHA-256 一致，原文件逐个验证内容哈希。原 `var/django` 和 `var/library` 原地改名保留到 `var/backups/literature-db-repair-20261022T084748Z/django-original` 与 `library-original`。备份还包含 `baseline.json`、`source-before.tar.gz`、私有 `env-before`、复放与迁移核对记录。恢复时必须先停止写入、另存当前库和新增文件，再调整挂载，不能用旧库直接覆盖运行库。

本轮迁移 `0007` 只增加标识查询索引。SQLite 仍使用原 IMMEDIATE / delete journal；原文件落盘、规则计算移出写事务，空闲 AnnotationJob 轮询不发起写事务。没有启用旧 SQLite 运行时的 WAL；SQLite 官方记录了旧版本 WAL-reset 风险，后续如切换必须先升级并验证。[SQLite WAL 官方说明](https://www.sqlite.org/wal.html)

`/appdata` 镜像的物理文件仍在 `/data/disk-images/smallfiles-8t.img`。它解决小文件 inode 容量问题，不提供独立磁盘冗余；主机监控同时检查 `/data` 和 `/appdata` 的剩余容量和 inode，不删除旧数据腾空间。

### 空闲心跳和状态文件减写（2026-11-14）

新版资料 worker 在确认数据库可读后，更新数据库目录内的 `.curation-worker-heartbeat` 文件时间；文件不可写时沿用原有数据库心跳。Web、健康检查与采集监控优先使用新鲜文件，文件缺失或过期时兼容读取旧数据库心跳。任务和租约仍保存在数据库中。`.curation-worker-heartbeat.lock` 使用 `flock` 限制同一数据库目录只有一个资料 worker；持锁状态由内核管理，不应通过删除锁文件来解除。

部署先更新并重载 Web，再重启资料 worker；回滚时先恢复旧 worker，确认数据库心跳更新后再回滚 Web。知识处理服务的 `status.json` 是可重建状态：运行中最多每 20 秒原子更新一次，不强制同步落盘；队列、处理进度和结果仍按各自的持久化方式保存。该服务必须重新启动才会使用新代码，不应为此中断正在处理的批次。

本次资料 worker 重启时 Docker 对其容器的回收超时；经用户同意，临时在 Web 容器启动一个使用同一把锁的资料 worker，原容器恢复并等候该锁后停止临时进程。现已确认原 `biomed-worker` 健康且独占锁，临时 PID 标记已归档，日志保留在生产数据库目录的 `curation-worker-recovery.log`。采集监控已恢复；本次故障用尽的重启记录经核验归档后重新启用原有有限重试，完整状态备份在 `/home/zhuqin/.local/state/biomed/collection-watch.pre-worker-recovery-20261113T170807Z.json` 与 `collection-watch.pre-budget-rearm-20261113T171106Z.json`。监控在临时 worker 持锁时可通过 Web 容器查询状态；只在原容器使用同一数据库目录、原进程等候同一把锁且临时进程无子任务时才切回。

恢复后原 worker 的 35 秒小样本中，33 次主键只读查询全部成功，未采到资料数据库排他锁；测试期间心跳持续更新。这不代表高并发场景已消除全部锁竞争。知识处理服务保持运行，新的状态写入代码待其下次正常启动后生效。不改变数据库 journal 模式，也不重启共享 Docker。文献采集范围本来已扫描完成并暂停，增量补扫尚未执行；原始隔离测试与锁采样见 `/appdata/02_resource/apps/biomed/operations/io-diagnosis-20261113T171221Z/`。

2026-10-23，`/data` inode 紧张时，将本项目现用前端依赖迁到 `/appdata/02_resource/apps/biomed/development/frontend-node-modules/node_modules`，`frontend/node_modules` 保留同路径符号链接。迁移前确认无前端构建进程或容器挂载依赖，复制后逐文件核对 SHA-256、权限与符号链接，再通过 TypeScript 全项目检查及 Vite 依赖加载后移除原副本；未安装或升级依赖；当时仅迁移前端依赖，后续Python环境迁移见下文。模型、资料和备份保持原位置。目标目录必须保留 `node_modules` 名称，以兼容 Node 的真实路径依赖解析；后续构建前核对 `/appdata` 挂载及链接有效。清单与验证记录位于 `/appdata/02_resource/apps/biomed/development/frontend-node-modules-migration-20261023.json`。

<a id="collection-watch"></a>

## 独立采集监控

`deploy/biomed-collection-watch.service` / `.timer` 安装到用户 systemd，每 2 分钟运行 `scripts/watch_literature.py`。用户 linger 已开启，退出终端后继续运行。它只管理指定批次和本项目 worker，不重启共享 Docker，不改变限速、预算、日期或增量时间锚点。

- 固定批次：`09688095-fad7-4980-b56b-76357d9f79f8`；配置为项目 `var/private/collection-watch.json`（600）。
- 状态为 `/home/zhuqin/.local/state/biomed/collection-watch.json`（600），包含检查时间、进度、恢复次数和邮件重试，不含 SMTP 密码。独立进程锁防止并发监控。
- 心跳超过 180 秒，或排除冷却后 20 分钟无元数据进展时恢复 worker；连续 3 次状态探测失败也尝试恢复。已知暂时错误耗尽内部重试后，冷却结束可再续跑。重启与续跑各最多 3 次 / 24 小时。
- 额度耗尽后持久记录 `halted`，暂停批次；数据库不能操作时停止资料 worker，并持续尝试邮件告警。不会在第二天自动清除该停止状态。排障后备份状态文件、确认问题已修复，再明确移除 `halted` 并人工续跑。
- 挂载身份、标记、读写状态不能核验，或底层空间少于 2 GiB / inode 少于 4096 时停止资料 worker，并独立发信。存储恢复后重新核验，再走有限恢复；不会将存储错误误当作进程错误反复重启。
- 来源身份冲突、预算耗尽及未知暂停不自动绕过。手动暂停即使发生在故障暂停期间也阻止恢复；监控发现限速 / 预算变更时停止自动操作，避免覆盖新配置。
- 邮件继续用私有 Claw SMTP 配置，收件人 `已配置的收件邮箱`。异常须介入时发信；完成优先用应用持久通知，若失败或完成后超过 5 分钟仍未送出，独立通道补发。SMTP 失败持久等待重试；只报告服务器接受，不保证收件箱送达。正常运行不发送周期邮件。
- 2026-10-23 起，监控用固定 `biomed-worker` 容器的 `docker exec` / `restart` / `stop`，不通过 Compose 解析其他服务，采集恢复也不再顺带重启 annotation-worker。仅当 systemd 明确显示 Docker 为 `activating` 时等待，不消耗资料 worker 恢复额度；启动超过 30 分钟按本次 boot / InvocationID 去重邮件告警，继续等待，不重启共享 Docker。服务就绪后仍重新核对存储、限速、暂停来源与冷却，未知状态保留原有有界处理。

2026-10-23 本次 inode 告警、Compose 探针超时及主机重启后的排障，状态备份、修复前代码与恢复审计位于 `/appdata/02_resource/apps/biomed/operations/collection-watch-repair-20261023T145918Z/`。监控误耗额度只能在问题修复、实际 worker / guard 核验后由运维审计清除，旧状态和邮件记录保留；以该目录 `recovery-result.json` 的实际结果判断恢复完成，不能把脚本启动当作采集恢复。

本次有界恢复由独立用户 oneshot `biomed-collection-repair-20261023.service` 执行该目录 `repair.py`，专用 `repair.lock` 防止重复运行，不依赖聊天会话。脚本最多主动处理 55 分钟，systemd 硬上限 60 分钟；成功或异常退出交回原监控 timer，并以 `ExecStopPost=/usr/bin/systemctl --user start biomed-collection-watch.timer` 覆盖进程被强制终止的情况。等待期间保留原 `halted`，仅在存储、容器身份、限速、暂停来源和冷却核验后审计解除；最终仍须看到实际采集数增长。

本次已于北京时间 2026-10-23 23:46 验证恢复：采集从 391,160 增至 391,210，随后原监控观察到 391,260；连续错误归零，保持 2 秒间隔、256 KiB/s，来源退避完整等待后续跑。恢复观察窗口须排除已知退避时间，同时保持首次任务的总截止不顺延；修订前观察结果及后续成功结果均保留在上述审计目录。监控代码的 18 项针对性测试通过。

```bash
systemctl --user status biomed-collection-watch.timer
journalctl --user -u biomed-collection-watch.service -n 30 --no-pager
python3 scripts/watch_literature.py --config var/private/collection-watch.json --dry-run
docker compose exec -T worker python manage.py literature_guard 09688095-fad7-4980-b56b-76357d9f79f8
```

这些自动规则用于恢复可识别的故障；数据库损坏、磁盘故障、凭据失效或来源规则变化仍可能需要人工处理。不能把自动重启等同于任何故障都能自行修复。


## 开发与研究的多文件存储（2026-10-23）

本机 `/data` 的 inode 容量远小于 `/appdata`。即使还有数 TiB 字节容量，inode 耗尽仍会使新文件失败。用户已确认将依赖、缓存和实验写入优先放到 `/appdata`。源码仍以 `/data/06_project/biomed` 为唯一入口；共享权重与模型下载缓存继续使用 `/data/models`，不建立另一份模型或项目。

生产数据库和文献已经位于 `/appdata/02_resource/apps/biomed/production/var/{django,library}`。前端依赖也已迁移，见前文迁移记录。开发虚拟环境 `.venv` 已迁到 `/appdata/02_resource/apps/biomed/development/python-env`，保留同路径符号链接，8,490个条目经过内容/权限/链接校验并验证Django、DRF导入后清理旧副本。迁移审计在 `development/relocations/venv-20261023T150945Z.json`。当前9B运行环境是 `/appdata/02_resource/apps/biomed/research/runtime`；本轮原始输出在 `research/experiments/knowledge-candidates-20261023/qwen35-9b-joint`，项目实验目录保留符号链接。旧 `var/runtime/evidence-pilot-venv` 仍被研究环境的 `.pth` 引用；迁移须保留入口并验证导入，不能直接删除。

实验目录 `var/experiments` 已迁到 `/appdata/02_resource/apps/biomed/research/artifacts`，1,890个条目完成内容、权限及链接校验，原路径保留符号链接。当前9B三条断点及全部历史试验仍可从原入口读取；旧复制已清理，审计在 `development/relocations/var-experiments-20261023T151654Z.json`。

新宿主机开发命令使用：

```bash
scripts/with_appdata.sh .venv/bin/python manage.py check
scripts/with_appdata.sh npm --prefix frontend run build
scripts/with_appdata.sh uv ...
```

包装命令只改变缓存目录，不自动替任意工具选择输出路径。新建运行环境和批量产物须显式放在 `/appdata/02_resource/apps/biomed/`（或使用上述兼容入口）。包装命令先检查 `/appdata` 已挂载，再设置项目专属 uv/pip/npm/Ruff/Python字节码缓存到 `/appdata/02_resource/apps/biomed/development/cache/`，不修改全局用户配置或生产容器配置。原有备份、参考数据和冻结来源继续保留。目录迁移须复制校验后切换入口；处于运行中的模型或写入任务先结束/暂停并保留断点。

## 日志与已知限制

2026-11-09 公网接入时，知识候选列表在磁盘 I/O 压力下曾占满两个 web 进程。查询计划显示问题是对全部候选及关联片段先聚合，再分页；文献表按主键关联，并非对百万篇文献全表扫描。现改为按候选主键计数、先分页再仅统计本页片段，保留列表、详情及筛选计数语义。上线后两次知识列表请求服务端耗时约 1.04 秒和 0.25 秒；磁盘压力仍会影响响应，不能把这两次测量视为性能保证。

### 常驻知识处理（2026-11-06）

`deploy/biomed-knowledge-worker.service`安装到`~/.config/systemd/user/`，使用本项目`.venv`监督已有模型脚本。状态与断点位于`/appdata/02_resource/apps/biomed/operations/knowledge-processing/`，Compose只读挂载到`/app/knowledge-processing`供状态API读取；生产资料库不新增调度表。安装后执行`systemctl --user daemon-reload`及`systemctl --user enable --now biomed-knowledge-worker.service`。预算、暂停恢复、核读及目录见[常驻处理说明](knowledge-processing.md)。

静态文件在宿主机依次执行`npm --prefix frontend run build`、`DJANGO_DEBUG=1 .venv/bin/python -B manage.py collectstatic --noinput`；容器的staticfiles挂载只读，不在容器内collectstatic。代码／静态文件更新用`docker kill --signal=HUP biomed-web`热加载；只有挂载或启动配置变更才需重建容器。

本次新增状态挂载重建web时，遇到共享containerd清理超时；中止本次Compose客户端、等待旧容器退出后启动并恢复新web原名，网站已恢复200。没有重启共享Docker或删除数据。模型任务临时停止后使用同批断点继续。

2026-10-30候选库增量迁移：`0010_knowledge_candidates`仅新增三张候选表。遇到大SQLite库时，Django默认schema editor退出会执行全库`PRAGMA foreign_key_check`，可能延长IMMEDIATE写事务；不要并行重跑迁移。专用`scripts/apply_knowledge_schema.py`仅接受0010这一个原子前进计划，限定CreateModel/AddConstraint作用于三张新表，并逐表检查外键、比对原schema；其他计划拒绝，不能作为通用迁移快捷方式或使用`--fake`。

本机执行记录保存在`/appdata/02_resource/apps/biomed/operations/knowledge-intake-20261030/`：原schema、涉及5篇文章的来源记录、第一次迁移回滚核验及最终成功回执。宿主启动期间I/O拥塞，中止了额外整库备份；`before.incomplete.sqlite3`明确不可用于恢复，详情见`backup-status.json`，不把它当作完整备份。已核对原schema不变，最终仅新增三表；文献、正式Evidence/Review及采集参数未改。候选数据随现用Django数据库备份。

访问日志记录路径、状态码和耗时，不记录文献全文、病例或密码。模型处理失败不会把外部响应和凭据写入用户可见错误。

VEP 缓存探测与成功计算是两件事。此前 GRCh37 示例计算出现 300 秒超时；本轮保留原计算链路，不把界面合并、缓存 ready 或知识库测试描述为 VEP 实算已修复。ANNOVAR 仍默认关闭。

## 018 知识发布迁移（2026-10-30）

`0011_knowledge_publication`新增审核、检索词两表及候选状态，使用`scripts/apply_knowledge_publication_schema.py`；仅接受该单步原子迁移，FK检查限于新两表、候选和锚点4个小表。与0010工具分开，不能绕过全库其他迁移。隔离验证覆盖失败回滚、数据保持、重复运行和非法计划拒绝。实际迁移约0.95秒，无关schema及原15组数据不变。

定向备份`/appdata/02_resource/apps/biomed/operations/knowledge-release-20261030/small-tables-before.json`包含3张旧知识小表、schema和迁移记录，不是整库备份；`migration-result.json`保存回执。只重启web加载接口/页面，不重启采集worker。真实审核文件先`review_knowledge_candidates --dry-run`验证再导入，重复导入幂等；不要用旧研究评分伪造AI通过。详见[018](research/018-knowledge-publication.md)。
