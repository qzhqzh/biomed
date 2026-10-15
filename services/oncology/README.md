# biomed 教学验证模块

本模块保留虚构病例的字段提取、条件匹配、出处展示及问题记录，作为 biomed 的教学验证工具。正式标准实体、真实资料和证据审核由根项目 `apps/curation` 负责。

唯一界面：[局域网教学验证](http://192.168.124.2:50011/tools/teaching/)，[本机教学验证](http://localhost:50011/tools/teaching/)。原 18765 前端已经关闭，原生静态页面已由统一 React 界面取代；模块只提供内部 JSON API。

当前仍是 7 条自建教学资料、3 个虚构病例。没有把这些样例改成真实指南、适应症或招募试验。有限词表、匹配结果和未知字段不能证明临床适用。病例原文与字段只在前端内存中保留，不写入数据库；问题与经验保存在根目录 `var/oncology/lessons.sqlite3`，模块内 `var` 是指向它的相对链接。

## 运行与检查

唯一部署入口在根目录：

```bash
cd /data/06_project/biomed
docker compose up -d --no-build
curl --fail http://127.0.0.1:50011/api/v1/evidence/health/
```

本模块无主机映射端口；Django 的 `/api/v1/evidence/*` 同源网关转发内部 API。正式证据使用 `/api/v1/curation/evidence/`，两个目录不能混用。

独立本地 API 开发与测试：

```bash
cd /data/06_project/biomed/services/oncology
uv sync --locked --group dev
uv run uvicorn oncology_workbench.main:app --app-dir src --host 127.0.0.1 --port 18765
uv run ruff check src tests
uv run ruff format --check src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --locked pytest -q
```

此开发命令只开本机 API，用于网关联调；并不恢复第二个产品前端。

## 验证流程

1. 在“教学验证”切换到条件检索，选择虚构病例并提取字段。
2. 人工修改/确认后查询三类资料，核对出处、未知信息和条件冲突。
3. 改动病例或字段，确认旧确认状态和结果失效。
4. 将分期清空，确认它保持未知；查询超出样例覆盖的癌种时显示无教学结果。
5. 保存问题，在“问题与经验”更新状态、填写处理记录并完整导出。

历史需求和验证保留在 `docs/`。当前部署、路由与产品核心以[根 README](../../README.md) 和[产品架构](../../docs/product-architecture.md)为准。
