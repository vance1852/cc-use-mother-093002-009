# 分配人工智能普惠支持协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

`digital_trade_foundation.inclusive` 在此之上实现**人工智能应用普惠支持资格、配额与成效跟踪服务**：把申请主体与受益关系、数字基础条件、地区优先级、服务商冲突、支持类别、分期额度、培训与部署里程碑、效果指标与退出责任纳入同一条可复核决策链。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - inclusive.py：普惠支持的资格筛查、冻结排序、限时预留、承诺分期、成效核验、稳定候补、预算缩减、特批会签与报告；
  - acceptance_inclusive.py：普惠支持端到端离线验收。
- tests/：基础规则、事务边界、接口路由、普惠服务规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance
    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance_inclusive

验收命令使用临时 SQLite 数据库。普惠验收覆盖关联去重、评审回避、冻结政策排序与原因码、材料与前置里程碑驱动的限时预留转承诺、分期兑付与回调重放防重、到期释放与稳定候补晋升、局部失败释放、特批独立会签与公平性快照、预算缩减保护已兑付资金、重启后时钟/候补/分期余额准确、按政策版本比较资金覆盖、地区分布与有效成果，以及全程审计链校验。成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## 普惠支持决策规则

1. **政策版本冻结**：权重、地区层级、类别名额、单主体上限、必备材料、里程碑、分期比例与效果目标整体哈希冻结，之后所有排序都引用不可变版本。
2. **关联去重与评审回避**：同一轮次同一类别下，一个关联集团（affiliation_group）只有最早一条申报进入候选；评审人与服务商同机构或登记了利益关系时，评分与核验一律拦截。
3. **可复核排序**：按地区优先级、数字鸿沟、受益需求和评审分加权，确定性兜底键保证可重算；每条结果保存分项因子、名次、决定（selected/waitlisted/held/locked/duplicate）和原因码及快照哈希。
4. **限时预留 → 正式承诺**：入选只产生带绝对截止时间的预留并占用预算；必备材料齐备、前置里程碑核验通过后才转正式承诺并生成与里程碑挂钩的分期。
5. **释放与稳定候补**：放弃、到期、局部失败、预算缩减都只释放未兑现部分（预留整笔或未兑付分期），沿冻结排序的稳定候补继续；已完成且核验通过、已有正式承诺的支持不参与重排。
6. **特批独立会签**：去重出局或被释放的申请可走特批，必须由发起人之外的审计员会签，落库会签前后的地区公平性快照，并受剩余预算约束。
7. **幂等与恢复**：申报、材料、里程碑回调、效果上报等都以 request_id 去重，重放先于任何业务校验返回原回执，绝不重复占款；截止时间、候补位置、分期余额全部持久化，重启后继续准确。
8. **退出责任**：到期、放弃、失败、预算缩减分别登记退出记录；已兑付金额作为责任金额保留。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。普惠接口统一挂在 /inclusive/* 下（如 POST /inclusive/policies、/inclusive/applications、/inclusive/ranking-runs、/inclusive/commitments、/inclusive/specials/countersign，GET /inclusive/reports/funds、/inclusive/reports/regions、/inclusive/reports/outcomes、/inclusive/reports/policy-comparison），各角色（admin/operator/reviewer/auditor）只能完成相互分离的步骤。
