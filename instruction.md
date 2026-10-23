# 供热分摊底稿重建

片区交接新系统上线时，物业导出了各房间热量表和三组二次侧环路总表。房间到环路的
归属没有随导出文件带出，但本季度租户账单需要在当晚重新核对。请修好
`/app/heat_ledger_reconcile.py`，让它从总表与房间表恢复归属并生成分摊单。

```text
本次交付到两层归属、分摊账单和原子发布完成为止，不扩展本页之外的随机场景。
```

## 输入口径

根对象不接收未声明字段。完整字段集合如下：

```text
site_id periods period_hours ambient_c pipe_loss_w_per_m_k
riser_pressure_coeff rate_per_kwh loops risers rooms
```

`site_id` 是 2–40 个字长的站区简称。计费段名放在 `periods` 里，
共 3 至 6 个，空串或重名都不算有效。`period_hours` 写明每个时段的整数小时数，
取值 1 至 24。布尔值不能代替
数值。`ambient_c` 落在 -40 到 60 摄氏度之间，`pipe_loss_w_per_m_k`、
`riser_pressure_coeff` 与 `rate_per_kwh` 都是正有限数，后三项上限为 1000000。

环路记录只接受下列字段：

```text
loop_id riser_count floor_area_m2 route_m supply_c meter_kwh
```

清单给 2–4 条记录，环路编号非空且互不重复。
`riser_count` 是 1 到 4 的整数，`floor_area_m2` 与 `route_m` 是正有限数。
`supply_c` 与 `meter_kwh` 的长度都等于 `periods`。`supply_c` 各项大于 `ambient_c`，
`meter_kwh` 各项不小于 0。

立管记录只接受下列字段：

```text
riser_id terminal_count floor_area_m2 pressure_drop_kpa
```

清单给 2–8 条记录，立管编号非空且互不重复。`terminal_count` 是 1 到 4
的整数，`floor_area_m2` 与 `pressure_drop_kpa` 是正有限数。

房间记录只接受下列字段：

```text
room_id tenant_id area_m2 terminal_kwh branch_length_m flow_lps
```

`rooms` 里有 8–16 个房间。房间编号与租户编号是非空字符串，房间编号
互不重复。`area_m2`、`branch_length_m` 与 `flow_lps` 是正有限数，`terminal_kwh` 与
`periods` 等长且每项不小于 0。

导出程序附加未声明成员时，复核请求被拒。导出程序遗漏任一顶层成员时，复核请求被拒。

每个立管接收 `terminal_count` 个房间，每个环路接收 `riser_count` 个立管。立管面积是
所辖房间 `area_m2` 之和，环路面积是所辖立管面积之和，两处都允许 `1e-7` 平方米的
绝对误差。房间到立管、立管到环路的归属都未在输入中给出。

立管压力损失按下列公式计算，允许 `1e-7` kPa 的绝对误差：

```text
pressure_drop_kpa = riser_pressure_coeff
                  * sum(branch_length_m * flow_lps * flow_lps)
```

二次侧热量总表还存在一个未标定的计量倍率；同一环路在全部计费段使用同一个倍率，
倍率范围为 0.8 到 1.2。

房间到立管、立管到环路的唯一两层划分与每个环路的倍率要共同满足终端数量、两处面积、
立管压力损失和全部计费段总表。

环路总表读数等于所辖房间终端热量之和乘该路计量倍率，再加管路损失。管路损失按每个
计费段计算：

```text
loss_kwh = route_m * pipe_loss_w_per_m_k
         * (supply_c - ambient_c) * period_hours / 1000
```

总表与房间热量、管路损失之和允许 `1e-7` kWh 的绝对误差。若找不到满足条件的划分，或
满足条件的划分不唯一，进程以退出码 1 结束并在标准错误写 `运行拒收: BAD_MODEL`。

## 分摊与账单

环路管路损失按该环路各房间全部计费段的终端热量占比分摊。某环路终端热量总和为零时，
损失由该环路房间均分。房间的 `billed_kwh` 是全部计费段终端热量之和乘环路计量倍率，
再加分摊损失。

租户账单把同一 `tenant_id` 下房间的 `billed_kwh` 相加。租户账单的 `cost` 等于该合计
电量乘 `rate_per_kwh`。各输出列表以对应编号递增排列。数值采用双精度计算，不中途
格式化。

## 调用与结果

复核单路径是第二个参数。租户结账前可以把追加型台账作为第三个参数传入：

```bash
python3 /app/heat_ledger_reconcile.py <输入> <复核单>
python3 /app/heat_ledger_reconcile.py <输入> <复核单> <台账>
```

复核单采用紧凑 JSON，并以单个 LF 结束。复核单顶层成员是 `site_id`、`status`、
`assignments`、`loop_totals`、`tenant_bills`。`status` 的值是 `reconciled`。

`assignments` 的成员是 `room_id`、`riser_id`、`loop_id`、`terminal_kwh`、
`pipe_loss_kwh`、`billed_kwh`。`loop_totals` 的成员是 `loop_id`、`riser_count`、
`scale`、`terminal_kwh`、`pipe_loss_kwh`、`billed_kwh`。`tenant_bills` 的成员是
`tenant_id`、`billed_kwh`、`cost`、`room_count`。

成功时不写标准输出和标准错误。同一输入再次调用时，复核单字节一致。复核单通过同目录
暂存文件发布。

## 台账

台账是 UTF-8 JSONL，采用紧凑 JSON，每一行以单个 LF 结束。台账行成员是 `site_id`、
`input_sha256`、`result_sha256`、`billed_kwh`。两个摘要按相应文件的原始 UTF-8 字节
计算。`billed_kwh` 等于复核单中所有房间 `billed_kwh` 之和。

同一输入再次成功复核时，台账字节一致。不同输入产生不同复核单摘要时，台账按成功顺序
追加一行。台账无法解码、行成员集合不符、末行缺少 LF、已有结果摘要对应不同内容，或
台账路径与复核单路径相同时，进程以退出码 1 结束并在标准错误写
`运行拒收: BAD_LEDGER`。

## 失败边界

输入 JSON 无法解码、成员不合法或数组口径不符时，进程以退出码 1 结束并在标准错误写
`运行拒收: BAD_INPUT`。复核单无法完整发布时，进程以退出码 1 结束并在标准错误写
`运行拒收: IO_FAILURE`。

复核请求失败时，复核单与台账保留旧字节，暂存文件会被清除。终端不会出现 Python
`traceback`。
