# 客流闸机采录台

离线读取虚构展馆的闸机事件、票种与入口映射，重建票据进出和分区在场数。仅使用本地合成材料，不联网。

`python -m pip install -e .` 安装，`python -m gateflow demo` 一条命令生成 SQLite 与 JSON/Markdown 报告，`python -m gateflow --help` 查看独立命令，`python -m pytest -q` 运行测试。

来源 Source 以原始字节 SHA-256 幂等保存；Event 以设备和序号去重，身份冲突进入 Issue。GateMap 按事件时间选版本；Ticket 保存票种；Occupancy 与 Decision 由不可覆盖事件重算。进、出、跨区移动要求票据状态相符，撤销必须引用存在的原事件。设备序号断档与心跳缺口降低结论可信度；容量超限单独报告。非法状态不会直接改变在场数。报告包含来源哈希、事件 ID、票种、分区时段与问题证据。业务状态为未入馆、在馆、离馆；结论为可信、超限、证据不足。错误码在 issue.code 中。

Run 记录每次重算状态，Evidence 逐事件连接原始来源哈希与载荷哈希；重算与运行记录在同一事务内写入。

## 布展物资采购

开馆前的展区物料需求与供应商 CSV 报价先冻结为 `venue_procurement_case`（需求+报价原文 SHA-256，不预设供应商、价格或到货日），再由精确整数穷举签发两份合同：

- `venue-buy-register --db ... --case-id ... --case <case.json>`：冻结分区需求与报价；展区身份无效等问题留证并在规划期统一诊断。
- `venue-buy-plan --db ... --case-id ...`：对最多 8 种 SKU、3 家供应商的本地案例穷举全部整数分配，叶节点联合核对可供量、跨 SKU 合计起订额与各展区到货日，按 `min_total_cost`（先总价后最大正逾期）与 `min_max_lateness`（先最大正逾期后总价）各取字典序最优。同 SKU 可整数拆分到多家供应商；缺项、重复矛盾报价、非正价格、容量不足、起订额不满足、展区身份无效或无完整可行解时整单 `待核`，绝不只买一部分却宣称两区可布展。
- `venue-buy-report --db ... --case-id ... --out x.json`：导出 JSON 订单与布展准备报告（同名 `.md` 摘要），含逐供应商 SKU/数量、整数分行价与总价、报价原文 SHA、分区需求身份、承诺到货日、区域延误天数、帕累托前沿、全部可行订单与无更优订单说明。

采购表 `purchase_plan/purchase_contract/purchase_order_line/purchase_vendor_total/purchase_requirement/contract_supersession` 由 schema 幂等迁移到旧库。同一冻结案例重复规划稳定且不覆写旧单；后续补价或需求修订以新案例登记，旧有效合同显式 `取消` 并记录 `替代/补购` 差额，同一需求不叠加收费。采购流程不改写闸机事件、门票与已确认的展品搬运证据。
