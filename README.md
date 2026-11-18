# 客流闸机采录台

离线读取虚构展馆的闸机事件、票种与入口映射，重建票据进出和分区在场数。仅使用本地合成材料，不联网。

`python -m pip install -e .` 安装，`python -m gateflow demo` 一条命令生成 SQLite 与 JSON/Markdown 报告，`python -m gateflow --help` 查看独立命令，`python -m pytest -q` 运行测试。

来源 Source 以原始字节 SHA-256 幂等保存；Event 以设备和序号去重，身份冲突进入 Issue。GateMap 按事件时间选版本；Ticket 保存票种；Occupancy 与 Decision 由不可覆盖事件重算。进、出、跨区移动要求票据状态相符，撤销必须引用存在的原事件。设备序号断档与心跳缺口降低结论可信度；容量超限单独报告。非法状态不会直接改变在场数。报告包含来源哈希、事件 ID、票种、分区时段与问题证据。业务状态为未入馆、在馆、离馆；结论为可信、超限、证据不足。错误码在 issue.code 中。

Run 记录每次重算状态，Evidence 逐事件连接原始来源哈希与载荷哈希；重算与运行记录在同一事务内写入。

## 展馆保存环境控制

`gateflow.climate_control` 在同一 SQLite 上冻结展馆气候证据并求解有限班次空调启停。原闸机客流与分区超容结论不受影响。

```
python -m gateflow climate-demo --workdir out
python -m gateflow climate-register --db out/climate.db --case-id CLIMATE-1 --fixture fixtures/climate-case.json
python -m gateflow climate-plan --db out/climate.db --case-id CLIMATE-1 --all-contracts --out out/plan.json
python -m gateflow climate-execute --db out/climate.db --case-id CLIMATE-1 --contract min_energy --slots 0
python -m gateflow climate-feedback --db out/climate.db --case-id CLIMATE-1 --contract min_energy --feedback feedback.csv
python -m gateflow climate-report --db out/climate.db --case-id CLIMATE-1 --out out/climate-evidence.json
```

**动力学**：每时段由前一时段真实/预测温度 + 该区增温 - 本时段冷却量推进。单次动作使所选区降温 `cooling_c_per_action`，共享设备每时段最多服务一个区。展品热暴露是每时段结束温度超过该区上限的正温差之和（不是最高读数，也不把缺测当作零）。

**联立最优**：所有时段与分区在一个精确动态规划中求解，状态为各区起始温度，动作域为每时段全部允许分区子集；只剪除在电费与分区暴露上逐分量不劣的状态，因此结果是冻结目标下的全局精确最优，可用独立的穷举见证复核（`optimality.brute_force_matches`）。不做逐区阈值报警，也不按当前最热区贪心开机。

**目标合同**（同一证据与设备，仅目标顺序不同）：
- `min_energy`：先最小化用电整数分，再最小化最大分区累计暴露；
- `min_max_zone_exposure`：先最小化各区累计暴露最大值，再最小化用电。

样例（A/B 均 20°C，A 每时段 +3°C、B 每时段 +2°C，单次冷却 -2°C，上限 22/21°C，电价 100/1000 分，暴露上限 6）：
- `min_energy` 仅在第 0 时段冷却 A，100 分，累计暴露 A=2/B=4、最大 4；
- `min_max_zone_exposure` 先 A 再 B，1100 分，累计暴露 A=2/B=2、最大 2。

**整份待核**：传感器身份冲突、时区错位、增温缺项、设备降温能力非物理、计划容量不足、读数缺项都会使整份控制建议进入 `待核`，不发布任何部分命令。

**执行与迟到反馈**：`climate_execution` 记录已执行命令且不可回写；迟到真实温度只校正剩余时段起点并生成新版可行计划与偏差（`deviation`），已造成暴露超过合同上限时报告 `无法追回`，不用未来负暴露抵消。

表：`climate_case`（冻结证据）、`climate_plan`/`climate_slot_plan`（每时段源读数、动作、共享设备占用、分区暴露、电价、最优性依据）、`climate_execution`、`climate_feedback`、`climate_issue`。相同输入重算得到相同摘要。
