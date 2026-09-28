# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访。

### 接触者每日随访与解除规则

- 每日随访独立存储（`followups` 表），一人一天只允许一份记录；重复提交同一天返回 `409`。
- 填错可通过 `amend_followup` 订正：当前值被更新，旧值写入 `followup_revisions` 与审计日志，全程留痕。
- 判定异常：填报了任一症状（发热、咳嗽等），或体温 ≥ 37.3℃。
- 解除观察（`complete_followup`）：以 **max(最后接触日, 最近一次异常随访日)** 为锚点，
  锚点次日起连续 14 天每天都有一份无症状（且体温正常）的有效记录才放行；
  观察期未满、或窗口内有日期缺记录都会被拒绝（`400`），错误信息说明具体原因、日期。
- 最后接触日取 `exposure_end`，缺省回退 `exposure_start`；`begin_followup` 时服务端
  自动据此计算 `due_at`（最后接触日 + 14 天），忽略客户端传入值。
- 病例 `lab_positive` 确诊后，所有关联的、未解除接触者按其最后接触日重新计算观察窗，
  并写入 `case_confirmed_recalculate` 审计记录。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/<id>/timeline`：接触者随访时间线（每日记录、订正历史、解除评估 `release`），可用`?as_of=YYYY-MM-DD`指定评估日。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录（随访录入、订正旧值、确诊重算都会记录）。

接触者动作：

- `begin_followup`：`data: {"followup_start": "YYYY-MM-DD"}`，开始随访并自动计算观察窗。
- `record_followup`：`data: {"followup_date": "...", "symptoms": ["fever"], "temperature": 37.4}`，症状留空/空数组表示无症状；未来日期会被拒绝。
- `amend_followup`：同 `record_followup` 字段并必填 `reason`，订正已存在的某天记录，旧值留痕。
- `complete_followup`：`data: {"outcome": "..."}`，通过 14 天规则检查后才转为 `completed`。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
