# 实现云赏月互动内容治理服务基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域模块可以在这些稳定边界之上增加自己的状态、规则和接口，而不必重复实现身份、站点与审计能力。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/moon_governance/`：云赏月互动内容治理服务，覆盖投稿版本、审核租约、复议、规则升级与专题冻结；
- `tests/`：基础规则、事务边界、接口路由、治理规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m festival_foundation.acceptance
PYTHONPATH=src python3 -m moon_governance.acceptance
```

基础层验收会在临时 SQLite 数据库中登记组织、操作者、站点和参考资料，核对幂等回执与审计链。治理服务验收会完整走一遍投稿、审核、复议、专题发布、修订、撤回和规则升级链路，中途关闭并重新打开数据库模拟服务重启，确认待审与待复议队列继续保留。两条命令成功时都输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m moon_governance.api --database moon_governance.sqlite3 --host 127.0.0.1 --port 8081
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 云赏月互动内容治理服务

`moon_governance` 在基础层之上实现线上云赏月活动的内容治理。运营人员只依据文本与媒体摘要（`media_summaries` 只登记媒体编号、类型和校验摘要，不接触图片或视频本体）完成审核、引用和撤回。

### 核心规则

- **投稿与版本**：投稿分诗词接龙、家乡介绍、祝福留言三类，每个版本记录文本、媒体摘要、作者声明、来源引用、可见范围和审核结论；作者修改产生新版本，旧版本保留，已引用旧版本的专题不自动漂移。
- **审核租约**：审核任务必须先认领（`POST /review-claims`）再决定，租约持有人在有效期内才能作出唯一有效决定；租约过期后其他审核者可重新认领。
- **复议**：被驳回的当前版本可申请复议（`POST /submissions/{id}/appeals`），复议任务禁止原审核者认领，由另一名审核者作出维持或推翻。
- **规则升级**：`POST /rule-upgrades` 递增规则版本，只对未终结投稿（待审、已通过）重新排队复核，已驳回、已撤回、已处置的投稿保持原状；复核完成前内容保持受限。
- **专题**：专题编辑只能引用已通过且授权范围覆盖专题受众的确定版本；发布时冻结成员清单并逐一校验所有引用版本，任一失效即整体拒绝；发布后成员清单不可再改。
- **撤回与合规处置**：撤回（`POST /submissions/{id}/withdraw`）或合规处置（`POST /submissions/{id}/compliance`）后，未来读取只返回审计占位，专题中的失效成员同样以占位替代，审计链保留完整轨迹。
- **幂等**：所有写接口要求 `request_id`，相同请求安全重放并返回原响应，不同载荷复用编号返回 409 冲突。

### 主要接口

- `POST /submissions`、`POST /submissions/{id}/revisions`：投稿与修订；
- `POST /review-claims`、`POST /review-tasks/{id}/decisions`：认领与审核决定；
- `GET /review-queue`、`GET /appeal-queue`：待审与待复议队列（重启后继续保留）；
- `POST /topics`、`POST /topics/{id}/members`、`POST /topics/{id}/publish`、`GET /topics/{id}`：专题编辑、发布与读取；
- `GET /submissions/{id}/content`：面向未来的读取路径，可见时返回内容，否则返回占位；
- `GET /submissions/{id}/explain`、`GET /versions/{id}/explain`：解释内容当前为何可见、受限、被替代或被阻止。
