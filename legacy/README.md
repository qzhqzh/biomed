# Legacy sandboxes（已非权威入口）

本目录保留迁移前的实验代码，**请勿作为新开发或部署入口**。当前代码来源是 biomed 根仓库；参考缓存实体已统一到 `var/reference/`，这里的缓存路径只是相对链接。本次迁移保留全部历史数据，未执行清理删除。

| 路径 | 原角色 | 处置 |
|------|--------|------|
| `online-tool/online-vep/` | FastAPI + VEP 116 API | 逻辑已迁入根目录 Django；`116_GRCh37` 链接到根目录 `var/reference/vep/` |
| `online-vep/` | VEP 115 CLI / GRCh38 | 用例迁完且 116 GRCh38 就绪后可删除（含重复 tar.gz） |
| `online-annovar/` | ANNOVAR 手工 Docker | 服务经 Django `engines/annovar.py`；权威 humandb 在 `data/annovar/humandb/` |

权威启动方式见仓库根 [README.md](../README.md)。
