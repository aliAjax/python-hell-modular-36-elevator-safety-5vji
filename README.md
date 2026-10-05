# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/offline-records`：批量回传手机离线记录，请求体为`{"records":[...]}`。

## 离线记录合并

巡检员在井道等无信号区域先把巡检、维保、困人报警记录在手机上，回到值班室后整批上传。每条记录格式：

```json
{
  "source_id": "phone-a",
  "record_id": "a-0001",
  "equipment_id": "<设备id，或用asset_no代替>",
  "category": "inspection | maintenance | alarm",
  "field": "brake_check",
  "value": "ok",
  "recorded_at": "2026-10-05T08:00:00Z"
}
```

合并语义：

- 记录按`(source_id, record_id)`幂等：上传失败后整批重传，已合并的记录自动跳过，不会重复写入；同一标识内容不一致时返回冲突错误。
- 字段合并到对应设备：同一设备同一字段多台手机各记过一次时，先到的写入字段值，后到的不覆盖；所有来源、手机上`recorded_at`的写入时间和回传时间保留在设备`data.offline_entries[字段]`列表里。
- `category`为`inspection`且`value`为`passed`/`failed`的记录会登记为正式检验台账（标记`source: "offline"`）；整改未关闭时，即使离线检验合格，恢复许可仍会被拒绝。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制，离线回传的合格检验也不能绕过未关闭整改。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
