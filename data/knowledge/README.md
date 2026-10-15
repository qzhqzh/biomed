# 注释对照知识库

用于 VEP（Ensembl / ENST）与 ANNOVAR（RefSeq / NM）结果对比。

## 初始化与日常更新

推荐使用门户菜单「数据更新」：`/tools/data-updates/`。

- **检查更新**：下载固定 NCBI 官方来源，校验 gzip、字段、主键和数据量，比较解压后 SHA-256；不替换本地数据。
- **检查并更新**：检查后仅在内容、MANE 版本变化或本地文件缺失时发布新快照。下载失败、数据异常不发布。
- Gene 文件名固定为 `Homo_sapiens.gene_info.gz`，不能依靠文件名判断新旧；上游时间戳仅作参考。
- MANE 从 `current/README_versions.txt` 发现版本，再下载匹配的 summary；不再锁定 v1.5。
- 本地成功更新时间、最近成功检查时间和上游文件时间分别显示；无变化不刷新本地更新时间。
- 初始库保持只读。更新写入 `KNOWLEDGE_UPDATES_DIR`（默认 `data/knowledge-updates`）的 `releases/<id>/`，完整校验后原子切换 `current.json`；旧快照保留，不自动清理。
- 每个服务进程自动识别新快照；运行中的注释固定使用原快照，并在结果中记录 `knowledge_version`。
- 仅更新 Gene/MANE 知识库，**不更新 VEP/ANNOVAR 数据库**。来源按官方人类文件范围保留，其中包含少量尼安德特人/丹尼索瓦人记录；总条数不是蛋白编码基因数。

Compose 首次部署时，新增 bind mount 必须可供容器用户（UID 10001）写入。只为该目录初始化权限，不修改原始知识库或其他数据目录：

```bash
docker compose run --rm --no-deps --user root --entrypoint sh web -c 'mkdir -p /data/knowledge-updates && chown 10001:10001 /data/knowledge-updates'
```

无初始库时，同一个更新命令可以完成初始化。命令也可接入宿主机 cron/systemd；**本项目未自动安装定时任务**：

```bash
docker compose exec -T web python manage.py update_knowledge --check-only
docker compose exec -T web python manage.py update_knowledge
```

命令与页面共用文件锁，不允许并发更新。服务重启会中断后台下载，页面会提示重新检查；已发布快照不受影响。
每次完整检查需要网络下载和临时磁盘空间；保留的版本会占用额外磁盘。备份时同时保存初始库和整个更新目录。
数据量下降超过 20% 会拒绝自动发布，需人工核对上游变更。文件内容指纹用于变化检测，不是 NCBI 数字签名。

### 旧版导入脚本（仅离线初始化兼容）

以下脚本仍保留，但会直接改写初始目录、缓存已有下载，不提供在线原子更新；不要在运行中的服务上用它替代新更新命令。

```bash
python scripts/import_gene_knowledge.py
# 或仅用已下载 raw：
python scripts/import_gene_knowledge.py --skip-download
# 强制重下 MANE：
python scripts/import_gene_knowledge.py --force-mane
```

生成：

| 文件 | 说明 | 是否进 Git |
|------|------|------------|
| `meta.json` | 版本、条数、MANE 说明 | 是 |
| `genes.jsonl` | NCBI 人类基因（约 19 万行） | 否（本地生成） |
| `transcript-map.json` | MANE ENST↔NM | 否（本地生成） |
| `raw/` | 原始 gz | 否 |

## MANE 版本要点

| 维度 | 说明 |
|------|------|
| 组装 | 基于 **GRCh38** 定义；官方另提供 GRCh37 等组装映射，需要核对序列差异，本库未导入这些坐标映射 |
| 集合 | MANE Select（每基因一条常用）/ MANE Plus Clinical（临床额外） |
| 发布版 | 如 v1.5；随 NCBI `current/` 与 `README_versions.txt` 更新 |

门户「基因知识库」页顶部有同样说明与外链。

## API

- `GET /api/v1/knowledge/meta/`
- `GET /api/v1/knowledge/updates/`
- `POST /api/v1/knowledge/updates/`，JSON `{"action":"check"}` 或 `{"action":"update"}`；需 CSRF token，若配置 API Key 则也需提供。202 表示已受理，409 表示已有任务，轮询 GET 查看结果。
- `GET /api/v1/knowledge/genes/?q=&type=protein-coding&limit=50`
- `GET /api/v1/knowledge/transcripts/?q=&mane_only=1`

## 权威来源

- https://www.ncbi.nlm.nih.gov/refseq/MANE/
- https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/current/
- https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/Homo_sapiens.gene_info.gz
- https://www.ensembl.org/info/genome/genebuild/mane.html
