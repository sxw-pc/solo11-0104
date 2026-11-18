# 客流闸机采录台

离线读取虚构展馆的闸机事件、票种与入口映射，重建票据进出和分区在场数。仅使用本地合成材料，不联网。

`python -m pip install -e .` 安装，`python -m gateflow demo` 一条命令生成 SQLite 与 JSON/Markdown 报告，`python -m gateflow --help` 查看独立命令，`python -m pytest -q` 运行测试。

来源 Source 以原始字节 SHA-256 幂等保存；Event 以设备和序号去重，身份冲突进入 Issue。GateMap 按事件时间选版本；Ticket 保存票种；Occupancy 与 Decision 由不可覆盖事件重算。进、出、跨区移动要求票据状态相符，撤销必须引用存在的原事件。设备序号断档与心跳缺口降低结论可信度；容量超限单独报告。非法状态不会直接改变在场数。报告包含来源哈希、事件 ID、票种、分区时段与问题证据。业务状态为未入馆、在馆、离馆；结论为可信、超限、证据不足。错误码在 issue.code 中。

Run 记录每次重算状态，Evidence 逐事件连接原始来源哈希与载荷哈希；重算与运行记录在同一事务内写入。

## 灾损归档恢复（archive lab）

`archive-register` 冻结授权清单（revision、source_id、逐块 SHA-256、总字节数、整文件 SHA-256）与四块物理分片的观测哈希；任何恢复尝试都重新读盘并同时核对全部六项，不靠修补 JSON 文本猜测采录事实。

恢复只接受两种预先授权的完整性合同：

- `two_copy_quorum`：每个块必须在 A/B 两处副本都与同一清单摘要匹配（法定双份），任一块不足双份即阻断，CAPTURE-1 保持待恢复、导入 0 条。
- `verified_mosaic`：允许每块从任一摘要匹配的副本取回，优先选择完整副本；拼接后仍须逐字节核对长度、整文件 SHA-256，并先拼接再整体解 UTF-8（块边界可能落在汉字多字节序列中间，禁止逐块解码），再校验完整 JSONL 事件结构与事件数。CAPTURE-1 经 A0+B1 恢复出 338 字节、3 条事件。

分片异文、重复块号、块缺失/不可读、跨 revision 拼接、整文件摘要或长度不符、UTF-8 与事件结构非法时一律阻断，不写 `source`/`event` 行。发布采用崩溃可恢复协议：临时文件 fsync 后原子替换并 fsync 目录（先落盘后记账），`BEGIN IMMEDIATE` 串行化并发恢复者，来源登记与事件导入在同一事务提交；并发、写临时文件后崩溃、记账后文件丢失、迟到修复分片或清单换版都不会产生半份事件、双重来源身份或“账本可取而文件不存在”。同一原文重试幂等，不重复导入。

每次尝试（含阻断）都在 `archive_recovery_attempt`/`archive_chunk_decision`/`archive_publish` 账本中保存授权清单摘要、每块实际哈希、选取/见证/弃用/阻断及理由、副本与拼接顺序、完整源哈希、恢复文件路径、导入的 source/event ID 与原因；`archive-report` 与 `--report` 输出恢复证据报告，报告只含哈希与 ID，不含猜测事件。CLI：`python -m gateflow archive-register|archive-recover|archive-report ...`。
