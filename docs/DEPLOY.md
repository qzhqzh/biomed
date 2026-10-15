# 部署、备份与恢复

唯一项目目录 `/data/06_project/biomed`；根目录 Docker Compose 是唯一部署入口。只有 web 的 50011 对外监听。两类 worker 不发布端口，oncology 仅在项目私有网络提供教学 API，18765 已关闭。

## 日常运行

```bash
cd /data/06_project/biomed
docker compose up -d --no-build
docker compose ps
curl --fail http://127.0.0.1:50011/health/ready
curl --fail http://127.0.0.1:50011/api/v1/curation/overview/
```

- 局域网：http://192.168.124.2:50011/；本机：http://localhost:50011/。
- `web` 启动先运行向前兼容 migrations，再运行 Gunicorn。`worker` 与 `annotation-worker` 等 web 健康后启动，使用同一数据库；前者处理资料工作流，后者处理 AnnotationJob。
- 默认专属网段 `10.254.220.0/28`，不要清理其他项目网络或参考缓存。
- 原始资料存储挂载为 `var/library:/app/library`；业务数据库挂载 `var/django:/app/var`；历史问题库为 `var/oncology`。新目录需由容器 UID 10001 可写。
- 两类 worker 当前各保持单实例。SQLite 写事务采用 IMMEDIATE，批量导入分批暂存，最终原子切换；扩容前迁移 PostgreSQL 并测试认领并发。

## 构建与代码更新

Python 使用 `requirements.lock` 固定版本，前端使用 npm lockfile。修改前端后先构建，再 collectstatic，最后重启 web；页面根据 `.vite/manifest.json` 加载带内容摘要的 JS/CSS，不能给入口额外添加不同的版本查询串。

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

默认 Compose 不挂载 Docker socket；需要 Docker 模式的引擎时，显式使用 `deploy/docker-compose.engines.yml`。本机原配置使用 `ANNOVAR_MODE=docker` 且保留其运行镜像，因此本机 `.env` 设置：

```dotenv
COMPOSE_FILE=docker-compose.yml:deploy/docker-compose.engines.yml
ANNOVAR_MODE=docker
```

这保留既有 Docker 引擎运行条件，不开启 ANNOVAR 授权开关；`ANNOVAR_PUBLIC_ENABLED` 仍为 0。新安装采用默认 local 模式时，需自行提供合法可用的 ANNOVAR 程序。VEP 继续使用镜像内本地程序及原参考库。

## 身份与配置

- 当前审阅账号：`biomed-reviewer`；初始凭据在 `var/private/reviewer.txt`，权限 600，目录权限 700。不要将内容复制进文档或 Git。
- 该账号只有 `curation.change_evidence` 权限，可以在“临床证据”页登录、人工确认或撤回，无管理员权限。
- 如需其他审阅者，运行 `manage.py create_reviewer --username <name> --credentials-file var/private/<name>.txt`。命令拒绝覆盖已存在账号或凭据；重置密码使用 Django 的 `changepassword`，保留原审核身份。
- 非调试启动必须提供 `DJANGO_SECRET_KEY`，缺失会直接失败；允许主机默认限定 localhost、127.0.0.1 和当前局域网 IP。`.env` 中保存独立 `DJANGO_SECRET_KEY`；不要使用公开默认密钥。浏览器写入校验 CSRF，登录会话 8 小时，密码不写入日志。
- 目前查询、来源导入和资料上传仍面向受控局域网；人工抽查有账号权限。没有宣称已经实现多租户、资料隔离或机构级访问管理。

| 配置 | 用途 |
| --- | --- |
| `BIOMED_IMAGE` | web / worker 的同一运行镜像 |
| `CURATION_MODEL_BACKEND` | 知识模型适配器类路径；默认空，无外部文本发送 |
| `DATABASE_PATH` | Django 数据库路径，容器内 `/app/var/db.sqlite3`；本地命令兼容旧 `DJANGO_DB_PATH`，两者同时设置时前者优先 |
| `CURATION_STORAGE_DIR` | 不可变原文件目录，容器内 `/app/library` |
| `BIOTOOLS_API_KEYS` / `BIOTOOLS_REQUIRE_API_KEY` | 原生信 API 密钥策略，沿用已有语义 |
| `ANNOVAR_PUBLIC_ENABLED` | 默认 0；核实授权后才启用 |

模型适配器参数和发送边界见 [产品架构](product-architecture.md)。配置模型后可以重试 waiting_configuration 任务；不得修改旧审核的模型或提示词版本冒充重新审核。

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

- `var/django` 与 `var/oncology` 的一致数据库快照。
- `var/library` 的所有原文件与来源快照。
- `data/knowledge`、`data/knowledge-updates` 的完整版本及 current 指针。
- `var/reference` 大型参考库或可恢复的原始包/清单。
- `.env`、`var/private` 需单独保密备份，权限不放宽。

对资料与数据库的组合备份，先暂停 web / worker 写入，或使用已有不可变文件加一致性快照方案。数据库使用 SQLite backup API 或停止服务后复制完整目录；不能仅复制活动数据库主文件而遗漏 WAL。恢复必须同时核对版本记录引用的文件 SHA-256。

既有迁移备份位于 `var/backups/migration-20261012T033522Z`，本轮前端合并前备份位于 `var/backups/unified-ui-20261012T051024Z`，新领域表迁移前备份位于 `var/backups/curation-20261012T060136Z`。备份不是第二个开发项目；原 05_workspace 两个项目目录及旧参考库目录已经移除。

回滚业务代码时保留新增表、原文文件和审核记录，不反向删除 migrations 或覆盖运行库。需要完整恢复旧数据库时先停写，并保存当前版本作为新的备份。

## 日志与已知限制

访问日志记录路径、状态码和耗时，不记录文献全文、病例或密码。模型处理失败不会把外部响应和凭据写入用户可见错误。

VEP 缓存探测与成功计算是两件事。此前 GRCh37 示例计算出现 300 秒超时；本轮保留原计算链路，不把界面合并、缓存 ready 或知识库测试描述为 VEP 实算已修复。ANNOVAR 仍默认关闭。
