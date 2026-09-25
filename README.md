# 实现云赏月互动内容治理服务基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域模块可以在这些稳定边界之上增加自己的状态、规则和接口，而不必重复实现身份、站点与审计能力。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/moon_governance/`：云赏月互动内容治理服务，覆盖投稿版本、审核租约、复议、规则升级、专题发布与可解释读取；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

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

验收命令会在临时 SQLite 数据库中完成一次端到端流程，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。治理服务的验收覆盖投稿、审核租约、复议、版本漂移、专题发布、撤回占位、规则升级，并在中途重启服务验证待审与待复议队列继续保留。

## 云赏月内容治理服务

`moon_governance` 面向线上云赏月活动（诗词接龙、家乡介绍、祝福留言），运营人员仅依据文本与媒体摘要完成审核、引用和撤回，不接触图片或视频本体。

### 治理规则

- **投稿版本**：每次投稿与作者修改都产生不可变版本，记录文本、媒体摘要（类型、SHA-256、尺寸、时长、说明，拒绝媒体本体字段）、作者声明（原创性、授权范围）、来源引用（非原创必填）、可见范围（`public` / `event` / `restricted`）与审核结论；
- **审核租约**：投稿进入待审队列，审核员先租用任务再提交结论，租约保证多人审核时唯一有效决定，租约过期可被他人在到期后接管；
- **复议**：未通过版本可申请复议一次，必须由另一名审核者处理（租用与提交两处强制）；
- **规则升级**：`POST /rules/upgrade` 只重新评估未终结投稿（打开中的任务重排为新规则版本并撤销在途租约），已终结结论不受影响；
- **专题**：编辑只能引用已通过且作者授权范围匹配专题用途的确定版本；发布时冻结成员清单并整体校验，任一引用失效即整体拒绝；发布后成员版本不随作者修改漂移；
- **撤回与合规处置**：投稿进入终结状态后阻止未来读取，专题与详情接口返回审计占位（状态、时间、内容哈希），不返回正文；
- **幂等**：所有写接口携带 `request_id`，相同请求安全重放，不同载荷复用编号返回 `409` 冲突；
- **可解释**：`GET /submissions/{id}/explain` 说明内容当前为何可见、受限或被替代，并列出引用它的专题与发布快照。

### 角色分工

- `operator`：登记投稿与作者修改、申请复议、编辑与发布专题、登记作者撤回；
- `reviewer`：租用审核任务、提交审核与复议结论、执行合规处置；
- `admin`：登记操作者、升级审核规则，并可执行上述全部动作（审核结论除外）；
- `auditor`：只读查询与审计链核验。

### 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /submissions` | 登记投稿（首版本进入待审队列） |
| `POST /submissions/{id}/versions` | 登记作者修改，产生新版本 |
| `POST /submissions/{id}/withdraw` / `takedown` | 作者撤回 / 合规处置 |
| `GET /submissions/{id}` | 当前可见视图（终结后为审计占位） |
| `GET /submissions/{id}/versions/{n}` | 指定版本详情与审核记录 |
| `GET /submissions/{id}/explain` | 解释可见、受限或被替代的原因 |
| `GET /review-queue?kind=initial\|appeal` | 待审 / 待复议队列（重启后保留） |
| `GET /review-tasks/{id}` | 审核任务详情（含待审文本与媒体摘要） |
| `POST /review-tasks/{id}/lease` / `release` | 租用 / 释放审核租约 |
| `POST /review-tasks/{id}/decision` | 提交审核结论（需持有有效租约） |
| `POST /submissions/{id}/appeals` | 对未通过版本申请复议 |
| `POST /rules/upgrade` | 升级审核规则，重排未终结任务 |
| `POST /collections` | 创建专题（声明用途范围） |
| `POST /collections/{id}/items` | 引用确定版本（校验审核结论与授权范围） |
| `POST /collections/{id}/publish` | 冻结成员清单并整体校验后发布 |
| `GET /collections/{id}` / `publication` | 专题草稿视图 / 已发布视图（失效成员为占位） |

基础层接口（`/organizations`、`/actors`、`/sites`、`/audit-events`、`/health`）由同一服务进程提供。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m moon_governance.api --database moon_governance.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态、待审与待复议队列和审计链继续保留。
