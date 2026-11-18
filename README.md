# 客流闸机采录台

离线读取虚构展馆的闸机事件、票种与入口映射，重建票据进出和分区在场数。仅使用本地合成材料，不联网。

`python -m pip install -e .` 安装，`python -m gateflow demo` 一条命令生成 SQLite 与 JSON/Markdown 报告，`python -m gateflow --help` 查看独立命令，`python -m pytest -q` 运行测试。

来源 Source 以原始字节 SHA-256 幂等保存；Event 以设备和序号去重，身份冲突进入 Issue。GateMap 按事件时间选版本；Ticket 保存票种；Occupancy 与 Decision 由不可覆盖事件重算。进、出、跨区移动要求票据状态相符，撤销必须引用存在的原事件。设备序号断档与心跳缺口降低结论可信度；容量超限单独报告。非法状态不会直接改变在场数。报告包含来源哈希、事件 ID、票种、分区时段与问题证据。业务状态为未入馆、在馆、离馆；结论为可信、超限、证据不足。错误码在 issue.code 中。

Run 记录每次重算状态，Evidence 逐事件连接原始来源哈希与载荷哈希；重算与运行记录在同一事务内写入。

## 闸机采集器固件运维

事件中的真实心跳设备与闸机身份（D1/D2→G1，D3→G2）由 `ingest` 留存，`register` 在写入
`gate_firmware_case` 时把每台设备的事件 ID、事件哈希与来源哈希冻结进
`gate_firmware_evidence`，并把两份获展馆运维（VENUE-OPS）授权的恢复合同一并冻结。缺少授权
合同或缺少设备事件都会拒绝注册；冻结后同一 case 的内容不可改写。

`gate_firmware.invoke` 是采集器端点模拟器：安装、回滚都带稳定操作 ID 与预期 revision；端点会
拒绝、失联，或在**真实提交后丢失应答**（`ack_lost`）。操作 ID 复用会返回 `replayed`，若同一 ID
指向不同设备则返回 `operation_id_conflict`——绝不盲目换新 ID 重试。

`run_rollout` 是可恢复的离线编排器：

- 每台设备、每个阶段（`install` / `rollback`）在 `gate_firmware_step` 中恒有稳定操作 ID、
  预期 revision、指令状态、端点应答、状态复核与实况版本；下发前先落盘 `intent`。
- 进程在任意步骤前后崩溃后重跑，会先核对端点操作流水与 `gate_firmware_device` 实况：已提交的
  直接确认，绝不重装；未知漂移转人工。重跑同一 case 不会重复安装或回滚。
- 回滚只作用于本轮确实修改过的版本，且核对端点 revision；若其他维护员已改版，停止自动覆盖并写
  `gate_firmware_manual` 人工处理记录，补偿义务在重跑后仍然保留。
- `atomic_all_gates` 要求三台最终同版；一台失败即按实况对已安装设备逆序安全回滚，未修改设备
  显式标记 `skipped`。`zone_isolated` 以闸机单元（gate+cell）为准，同一单元的 D1/D2 必须同版，
  半个单元不得宣称完成，必要时回滚该单元本轮修改。
- 相同的端点事实在两份合同下产生不同的真实版本：本仓库 fixture 中 D1 成功、D2 提交后丢应答、
  D3 拒绝，`atomic_all_gates` 最终 v1/v1/v1，`zone_isolated` 最终 v2/v2/v1。

CLI：`firmware-register`、`firmware-run`、`firmware-report`、`firmware-state`、
`firmware-operation`、`firmware-demo`。`firmware-report` 输出逐设备 JSON 版本证据，含闸机、
旧/目标/实际版本、事件与来源摘要、操作 ID、指令/应答、状态复核、revision、回滚与人工记录；
"实际版本"只来自 `gate_firmware_device`，计划版本从不作为安装证据，原闸机事件与分区客流结论
不被改写。
