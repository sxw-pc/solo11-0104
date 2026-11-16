# 客流闸机采录台

离线读取虚构展馆的闸机事件、票种与入口映射，重建票据进出和分区在场数。仅使用本地合成材料，不联网。

`python -m pip install -e .` 安装，`python -m gateflow demo` 一条命令生成 SQLite 与 JSON/Markdown 报告，`python -m gateflow --help` 查看独立命令，`python -m pytest -q` 运行测试。

来源 Source 以原始字节 SHA-256 幂等保存；Event 以设备和序号去重，身份冲突进入 Issue。GateMap 按事件时间选版本（换版时用事件时刻关联原 gate）；Ticket 保存票种；Occupancy、Decision 与 OverCapacity 由不可覆盖事件重算。进、出、跨区移动要求票据状态相符，撤销必须引用存在的原事件，撤销与缺少票据/引用的记录在采录阶段即拒绝。设备序号断档与心跳缺口降低结论可信度；容量超限按连续超容区间单独报告。非法状态不会直接改变在场数。报告包含来源哈希、事件 ID、票种、分区时段、超容区间与问题证据。业务状态为未入馆、在馆、离馆；结论为可信、超限、待观察、需疏散、证据不足。错误码在 issue.code 中。

超容区间（over_capacity）按事件时间重建每个分区连续超容的起止、峰值、涉及票据，以及触发/解除事件 ID（观测窗口结束仍未解除时解除证据为空，不虚构解除）。同秒事件产生零时长区间，序号缺口、心跳缺口、重复撤销与跨区 move 均不会制造虚假持续时间。两种告警契约：即时契约（immediate）只要可信占用超过容量即记为超限；持续契约（sustained）仅当超容持续达到配置秒数（setting.sustain_seconds，默认 300）才升级为需疏散，短暂超容仍记录为待观察。证据不可信的区间一律标为证据不足，两种契约都不得报告安全结论。契约可由 config 的 setting 段预置，或用 evaluate --contract/--sustain-seconds 覆盖。

Run 记录每次重算状态，Evidence 逐事件连接原始来源哈希与载荷哈希；重算与运行记录在同一事务内写入。
