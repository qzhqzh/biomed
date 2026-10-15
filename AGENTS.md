# biomed 项目约定

- 唯一项目来源是 `/data/06_project/biomed`；沿用 https://github.com/qzhqzh/biomed 的历史与分支。不得在旧工作目录重建第二个项目。
- 开始前读 README 与当前任务相关文档；保留用户未提交修改。普通代码修改不自动授权提交、推送或发布。
- 根目录 Docker Compose 管理 web、资料 worker、annotation-worker 与内部 oncology API，只有 50011 对外提供统一 React 界面。保留既有 API 和持久数据兼容。
- 业务数据库在 `var/django` 和 `var/oncology`，原始资料在 `var/library`，私有账号在 `var/private`，专属参考库在 `var/reference`；初始/更新知识库在 `data/knowledge*`。备份在 `var/backups`，均不得提交。
- 不删除数据库、migration、历史数据、参考缓存或 Docker volume。部署/迁移按当次授权处理；新依赖与组件由实际需求决定。
- 先做生信、资料与审核流程；英文论文 PDF 为首批目标，模型方式未定。真实资料来源、权利、版本、适用条件和更新责任需记录。
- 肿瘤模块仍只用虚构病例和教学资料；不能自动证明临床适用或入组资格，不能上传真实患者资料。模块约定见 `services/oncology/AGENTS.md`。
- Python 优先 uv 并沿用现有依赖文件；前端沿用 npm lockfile。完成后运行最小有效检查，可见流程需真实浏览器验证。
- 中文简洁说明变更、验证与未解决限制；新工程问题记入相关经验文档。

- 产品核心是标准实体、真实资料、证据及持久工作流，见 PRODUCT.md 和 docs/product-architecture.md。AI 通过即可发布，人工抽查状态独立；不能自行改成人工必审后发布。
- 未配置模型时保留 waiting_configuration，不生成假 AI 结果。新解析、模型或提示词版本不能覆盖旧证据的出处与审核记录。
