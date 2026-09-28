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

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/<id>/timeline`：接触者时间线（每日随访、订正史、解除评估、审计）。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 接触者每日随访与解除规则

- 观察窗口：从**最后接触日**（`exposure_end`，缺省取`exposure_start`）次日起算 14 天；`begin_followup` 时按该规则自动计算 `followup_start`/`due_at`，调用方传入的日期不生效。
- 每日随访：`record_followup`，入参 `followup_date`、`symptoms`（空数组/空字符串表示无症状）、可选 `note`。按日期独立存储，**一人一天只收一份**，重复登记返回冲突。
- 订正：`revise_followup`，入参同上并必须提供 `reason`；当前值被更新，旧症状、备注、订正人与时间保存在该日记录的 `revisions` 中，并写审计。
- 解除：`complete_followup`（可传 `as_of` 指定评估日期，默认今天）。最近一次出现发热、咳嗽等症状的随访为异常，解除日期从异常日起重新计算 14 天；必须连续 14 天每天有无症状有效记录且观察期已满。观察期未满或缺记录均拒绝，错误信息列出具体原因和缺失日期。
- 确诊联动：病例执行 `lab_positive` 确诊后，所有关联接触者按最后接触日重新计算窗口并记录 `recalculate_window` 审计；已解除的接触者重新打开为随访中，历史随访全部保留。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
