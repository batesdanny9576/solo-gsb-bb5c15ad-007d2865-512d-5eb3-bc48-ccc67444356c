# 发票税额计算 API（Invoice Tax Calculator）

基于 **FastAPI + SQLite + Decimal** 的发票税额计算服务，支持 Docker Compose 一键启动。

- **全程 `Decimal` 高精度运算**：金额入参/出参一律为十进制字符串，绝不使用 `float`；计算在按本次输入量级调整的局部精度（`localcontext`）内进行，不依赖线程的全局 `Decimal` 设置，超大金额（如 30 位整数净额）也能精确校验、保存与复算。
- **统一的收支精度策略**：收款登记、分次冲销、来款分配与整批撤销、当前/历史收款余额等所有金额累计与量化，与开票共用同一套按金额量级自动放大的局部精度——超大金额发票登记 `0.01` 后未收余额不丢分位，登记/冲销大额款项也不会因默认精度抛出异常，收款响应、查询与快照的余额始终一致。
- **收款余额读取分段取数**：按发票 ID 查收款、按收款 ID 查冲销的 `IN (...)` 查询统一按固定批量（500 个 ID；连同 UTC 截止时点参数，单条语句绑定参数始终低于 SQLite 上限）分段读取，空集合不查询；各段取回的行合并后仍在同一局部精度下用 `Decimal` 逐笔累计（不走 SQL 浮点求和），收款与冲销各自保持 UTC 截止秒闭区间口径。逾期列表、应收账龄汇总、收款概览与历史快照在超大 ID 集合下也可查询，结果与小批量完全一致。
- **按币种最小单位舍入**：`ROUND_HALF_UP`（四舍五入）。USD/CNY 等按 0.01，JPY/KRW 按 1，KWD/BHD 按 0.001。
- **币种精度按发票固定**：创建发票时把原币与目标币小数位一并持久化；之后修改
  `CURRENCY_DIGITS` 不影响已保存发票——读取、复算与残差分配始终沿用各发票入库时
  的精度（旧的两位 USD 发票在 USD 改为零位后仍可正常 `GET`，不会复算报 `422`），
  历史记录的目标币精度从已持久化换算金额恢复；只有新建发票按当前配置校验。
- **残差固定分配且可复算**：发票总额与「逐行舍入后税额之和」的差（残差）按**最大余数法**确定性地分配回具体行，任何时候用同一请求复算结果完全一致（响应中带 `recompute_matched` 标志）。
- **跨币种换算**：按请求汇率换算，原币金额完整保留，目标币独立做最小单位舍入与残差分配。
- **收款登记**：按发票登记收款（发票币种字符串、必须大于 0），随时查询收款记录与未收余额；累计收款不得超过含税总额，校验与写入在同一 SQLite 事务中完成，并发不会超收。
- **收款冲销**：`POST /api/v1/payments/{payment_id}/reversals` 对已登记的收款流水冲销（可同一笔分次冲销，累计不得超过原收款）；冲销记录单独持久化、不删除原收款，未收余额、逾期查询与后续来款分配一律按**净入账额**（收款 − 冲销）计算，累计校验与写入在同一 SQLite 写事务内完成，并发冲销不会超额。
- **来款分配**：传入币种、金额字符串与待结发票 ID 列表，把一笔来款按**到期日升序（未设置到期日排最后）、同日按 ID 升序**逐张抵扣；只处理列表内发票，不做跨币种换算。**负额冲红 / 已结清发票（未收余额 ≤ 0）不参与抵扣**：分配 `0`、不产生收款流水，不消耗也不增加剩余来款，可分配余额只计入正未收额，成功时各笔收款合计恒等于来款金额。**每张发票以其保存时的 `currency_digits` 为准逐张校验实际分配额**（币种精度配置变更后，同币种旧发票可保留两位、新发票为零位，接口不按某一张发票的位数统一校验整笔来款）：整笔能按各发票精度精确分配才成功，任一实际分配额落不到对应发票的最小单位（如 `0.01` 落到零位发票）即 `422` 整笔拒绝；列表重复、发票不存在、币种不符或正未收额合计不足时同样**整笔拒绝、不留下部分收款**。分配成功在同一事务内保存**分配批次**（批次标识 `batch_id` 随响应返回）及本次实际生成的收款关联；每张被抵扣的发票沿用同一条 `payments` 流水，单张收款查询与逾期查询均可见。
- **按批次整批撤销**：`POST /api/v1/payment-allocation-batches/{batch_id}/reversals` 按批次一次撤销整笔来款分配——在**同一个 SQLite 写事务**内逐笔计算「收款 − 已有冲销」的**净入账额**，**仅对正余额追加等额冲销**（含手工已冲部分只冲剩余净余额），返回各笔撤销金额、撤销后净入账额与批次合计。接口**幂等**：重复请求不新增任何冲销，返回内容恒定（首次写入返回 `201`，重复请求或批次收款此前已全部手工冲完返回 `200`）；批次不存在返回 `404`。撤销生成的冲销与单张收款的手工冲销同表同口径，撤销后未收余额回升、可再次分配。旧的无批次收款不归属任何批次，仍可通过原冲销接口查询与逐笔冲销。
- **到期日与逾期查询**：创建发票时可选填 ISO 日期 `due_date`；`GET /api/v1/invoices/overdue` 按截止日（可选 `as_of`，缺省取 UTC 当前日期）分页返回**已到期且未收余额大于零**的发票，含币种与精确（Decimal）未收余额。
- **历史收款快照**：`GET /api/v1/invoices/{id}/payment-snapshot?at=...` 返回单张发票在指定 UTC 时点（必填、秒精度 `YYYY-MM-DDTHH:MM:SSZ`，截止秒为闭区间、同秒流水全部计入）的收款总额、冲销总额、净已收与未收余额；只统计该时点之前及当秒的流水，截止后的流水不改变历史结果，金额全程按发票币种最小单位用 `Decimal` 计算。
- **分期回款计划**：`POST /api/v1/invoices/{id}/installment-plan` 为单张发票创建**唯一一份**分期计划，提交按到期日升序排列的期次与金额（每期为币种最小单位的正数，期次合计须等于含税总额，校验与写入在同一 SQLite 事务内完成）；`GET .../installment-plan?as_of=...` 查询时把当前**净已收额（收款 − 冲销）按期次顺序逐期 FIFO 抵扣**，返回每期已覆盖额、未覆盖额，仅到期日严格早于查询日（缺省 UTC 当日）的未覆盖额计为逾期额。冲销后再查自动反映重新打开的期次，**原计划永不改写**。
- **供应商发票号码批次查重**：`POST /api/v1/invoice-number-check` 一次接收 1~500 条「供应商编号 + 供应商发票号码」，服务端对每个字段做 **Unicode NFKC 规范化 + 去首尾空白 + 大小写折叠**，在**同一供应商内**识别规范化后重复的号码，按输入首次出现顺序返回重复组（含全部输入位置与原始号码）；无状态、不落库。
- **原发票贷项凭证**：`POST /api/v1/invoices/{id}/credit-memos` 对已保存发票的**正净额行**提交一张可含多行的部分抵扣凭证，每行给出 `line_no` 与本次抵扣净额；拒绝重复行号、非币种最小单位金额以及不存在或非正的原行，同一原行历次抵扣净额不得超过原净额，检查与写入在**同一 SQLite 写事务**内完成。每行本次抵扣税额按「原行已确认税额 × 抵扣后累计净额 ÷ 原净额」按币种最小单位 HALF_UP，再减去历次已记税额得出，**完全抵扣时恰好抵完原行税额**。`GET .../credit-memos` 按原发票查询全部凭证，返回各行净额、税额、含税额及累计抵扣。凭证只追加，**原发票与收款流水（余额、逾期、分期计划）全部保持原样**。
- **应收账龄汇总**：`GET /api/v1/receivables/aging?as_of=YYYY-MM-DD` 按查询日 UTC 结束时点（`23:59:59Z`，含当秒）汇总应收账龄：只统计该时点前已创建、含税总额为正且仍有未收额的发票，未收额按截至该秒的收款 − 冲销计算；每张发票的全部未收额只进入一个账龄桶（无到期日单列；未到期含到期当日；逾期 1~30 / 31~60 / 61~90 / 91 天及以上），按发票币种分别汇总、不跨币种相加，各桶返回发票数与未收合计。**混合保存精度**：每张发票仍按自己入库时的 `currency_digits` 计算未收额，同币种账龄桶按落入该桶的参与发票**最高入库小数位**、币种合计与回显的 `currency_digits` 按该币种全部参与发票的最高入库小数位格式化（空桶沿用币种位数），合计 `1.55` 不会被首条 0 位发票舍成 `2`，结果与插入顺序无关；各桶未收额（Decimal 求和）恒等于币种总未收额。
- **期间税额归集**：`GET /api/v1/tax-ledger?start=YYYY-MM-DD&end=YYYY-MM-DD` 按 **UTC 自然日闭区间**（含首尾日期，`start 00:00:00Z` ~ `end 23:59:59Z`）归集税额台账：发票行按**发票保存时间**、贷项凭证行按**凭证创建时间**分别落入期间——**贷项凭证即使对应更早期开票，也计入本期抵减**。按**原币种 + 原发票行税率**分组（税率按数值规范化，`0.13` 与 `0.130` 同组；不同币种绝不混合），用 `Decimal` 分别累计开票（`invoiced`）、抵减（`credited`）与净额（`net = invoiced − credited`）的未税/税/含税金额，**只取原币列、不使用目标币换算列**。**混合保存精度**：同币种同税率的组按该组**实际参与记录（发票行与贷项凭证行）中最高的入库小数位**格式化并回显 `currency_digits`——贷项凭证行也参与组精度判定（0 位开票行配 2 位抵减凭证时整组按 2 位展示，抵减 `0.01` 不会被抹零），组精度取参与记录的 `max`、与插入顺序无关，开票减抵减严格等于净额。分组固定按币种代码升序、税率数值升序，空期间返回空分组，日期非法或倒置返回 `422`。
- **SQLite 持久化**（WAL 模式），提供创建、按 id 查询、分页列表、健康检查。
- 内置 OpenAPI 交互文档（Swagger UI / ReDoc）。

---

## 1. 快速开始（Docker Compose）

前置条件：已安装 Docker（含 Compose v2：`docker compose`）。

```bash
# 构建镜像并后台启动（首次启动自动建表、初始化依赖，单条命令即可）
docker compose up -d --build

# 或前台启动（日志直接输出到终端，Ctrl+C 停止）
docker compose up --build

# 查看状态 / 日志
docker compose ps
docker compose logs -f

# 停止
docker compose down

# 停止并同时删除数据卷（清空所有发票数据）
docker compose down -v
```

启动后服务监听 **`http://localhost:8000`**（宿主机浏览器/curl 直接访问该地址）：

| 地址 | 说明 |
| --- | --- |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/docs> | Swagger UI（可在线发请求） |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/openapi.json> | OpenAPI 3.1 Schema |

> 宿主机端口默认 `8000`，被占用时可用 `API_PORT=8080 docker compose up -d` 改到 8080。

### 快速冒烟

```bash
curl -s http://localhost:8000/health
# {"status":"ok","database":"up"}

curl -s -X POST http://localhost:8000/api/v1/invoices \
  -H 'Content-Type: application/json' \
  -d '{
    "currency": "USD",
    "due_date": "2026-09-30",
    "lines": [
      {"description": "笔记本", "net": "10.05", "tax_rate": "0.10"},
      {"description": "签字笔", "net": "20.05", "tax_rate": "0.10"}
    ]
  }'

# 登记一笔收款并查看未收余额（假设上一步返回的发票 id 为 1）
curl -s -X POST http://localhost:8000/api/v1/invoices/1/payments \
  -H 'Content-Type: application/json' \
  -d '{"amount": "20.00"}'

curl -s http://localhost:8000/api/v1/invoices/1/payments

# 冲销该笔收款的一部分（假设上一步返回的收款流水 id 为 1），可分次冲销
curl -s -X POST http://localhost:8000/api/v1/payments/1/reversals \
  -H 'Content-Type: application/json' \
  -d '{"amount": "5.00"}'

# 一笔来款分配到多张待结发票（按到期日升序、同日 ID 升序、无到期日排最后）；
# 成功响应含批次标识 batch_id，可用于整批撤销
curl -s -X POST http://localhost:8000/api/v1/payment-allocations \
  -H 'Content-Type: application/json' \
  -d '{"currency": "USD", "amount": "50.00", "invoice_ids": [1, 2, 3]}'

# 按批次整笔撤销上一步的来款分配（幂等：重复请求不新增冲销；首次 201、重复 200）
curl -s -X POST http://localhost:8000/api/v1/payment-allocation-batches/1/reversals

# 查询已逾期且仍有未收余额的发票（as_of 缺省取 UTC 当前日期）
curl -s 'http://localhost:8000/api/v1/invoices/overdue?page=1&page_size=20&as_of=2026-10-15'

# 查询发票 1 在指定 UTC 时点的历史收款快照（at 必填，秒精度，截止秒含同秒流水）
curl -s 'http://localhost:8000/api/v1/invoices/1/payment-snapshot?at=2026-10-15T12:00:00Z'

# 为发票 1 创建唯一一份分期回款计划（期次按到期日升序，合计须等于含税总额 33.11）
curl -s -X POST http://localhost:8000/api/v1/invoices/1/installment-plan \
  -H 'Content-Type: application/json' \
  -d '{"installments": [
    {"due_date": "2026-09-01", "amount": "10.00"},
    {"due_date": "2026-10-01", "amount": "13.11"},
    {"due_date": "2026-11-01", "amount": "10.00"}
  ]}'

# 查询分期计划（as_of 缺省取 UTC 当前日期；仅到期日严格早于 as_of 的未覆盖额计逾期）
curl -s 'http://localhost:8000/api/v1/invoices/1/installment-plan?as_of=2026-10-15'

# 供应商发票号码批次查重（全角/大小写/首尾空白规范化后同供应商内判重）
curl -s -X POST http://localhost:8000/api/v1/invoice-number-check \
  -H 'Content-Type: application/json' \
  -d '{"items": [
    {"supplier_code": "SUP-A", "invoice_number": "INV-001"},
    {"supplier_code": "sup-a", "invoice_number": " inv-001 "},
    {"supplier_code": "SUP-B", "invoice_number": "INV-001"}
  ]}'

# 对发票 1 的原行提交一张可含多行的部分抵扣贷项凭证（line_no + 本次抵扣净额）
curl -s -X POST http://localhost:8000/api/v1/invoices/1/credit-memos \
  -H 'Content-Type: application/json' \
  -d '{"lines": [
    {"line_no": 1, "net": "5.00"},
    {"line_no": 2, "net": "10.00"}
  ]}'

# 按原发票查询全部贷项凭证（各行净额/税额/含税额 + 累计抵扣）
curl -s http://localhost:8000/api/v1/invoices/1/credit-memos

# 应收账龄汇总（as_of 缺省取 UTC 当前日期；按币种分桶、不跨币种相加）
curl -s 'http://localhost:8000/api/v1/receivables/aging?as_of=2026-10-15'

# 期间税额归集（UTC 自然日闭区间，含首尾日期；原币种 + 原发票行税率分组）
curl -s 'http://localhost:8000/api/v1/tax-ledger?start=2026-10-01&end=2026-10-31'
```

> 以上命令要求服务已启动：在仓库根目录执行 `docker compose up -d --build`（见下一节）。

该请求每行精确税额为 1.005 / 2.005（逐行 HALF_UP 得 1.01 / 2.01），但总税额
3.01（30.10×10%），行和 3.02，产生 **-0.01 残差**，按最大余数法从零头最小的
第 1 行扣减 1 个最小单位：

```json
{
  "id": 1,
  "rule_id": "half_up_plus_largest_remainder_v1",
  "currency": "USD",
  "currency_digits": 2,
  "lines": [
    {"line_no": 1, "description": "笔记本", "tax_rate": "0.10", "currency": "USD",
     "net": "10.05", "exact_tax": "1.005", "tax": "1.00", "gross": "11.05"},
    {"line_no": 2, "description": "签字笔", "tax_rate": "0.10", "currency": "USD",
     "net": "20.05", "exact_tax": "2.005", "tax": "2.01", "gross": "22.06"}
  ],
  "totals": {"currency": "USD", "total_net": "30.10", "total_tax": "3.01", "total_gross": "33.11"},
  "rounding": {
    "mode": "ROUND_HALF_UP",
    "minor_unit": "0.01",
    "residual_units": -1,
    "residual_amount": "-0.01",
    "method": "largest_remainder",
    "allocations": [{"line_no": 1, "delta_units": -1, "remainder": "0.005", "rank": 1}]
  },
  "due_date": "2026-09-30",
  "created_at": "2026-09-23T01:40:00Z",
  "recompute_matched": true
}
```

不传 `due_date` 时该字段返回 `null`，行为与旧版一致。

注意不变量：**逐行 `tax` 之和恒等于 `totals.total_tax`**（1.00 + 2.01 = 3.01）。

---

## 2. 计算规则（固定，可复算）

规则版本号 `rule_id = half_up_plus_largest_remainder_v1`：

1. 每行 **精确税额 = net × tax_rate**，全程 Decimal 高精度、无 float；所有十进制
   运算在按本次输入量级计算的局部精度（`localcontext`，常规金额 50 位、超大金额
   自动放大）内进行，不依赖线程全局上下文（FastAPI 工作线程的默认 28 位精度
   不影响结果）。
2. 每行税额按**币种最小单位**做 `ROUND_HALF_UP`。
3. 发票总税额 = Σ 各行精确税额后再做一次 `ROUND_HALF_UP`；它与逐行舍入税额之和
   的差称为**残差**（单位为最小单位的整数倍，`|残差| ≤ 行数`）。
4. 残差按 **最大余数法（largest remainder / Hare quota）** 分配：
   - **正残差**（总额更大，需补分）：按「精确税额相对其最小单位向下取整的零头」
     **从大到小**选行，每行 **+1 个最小单位**，分完为止；
   - **负残差**（需扣减）：按零头**从小到大**选行，每行 **-1 个最小单位**；
   - **零头相同按行号（请求中的先后顺序）决胜负**，因此结果完全确定、可复算。
   - 分配明细（哪一行、±几个最小单位、零头、名次）在响应的
     `rounding.allocations` 中可见、可审计。
5. **跨币种**：`target_currency` 与 `currency` 不同时必须提供 `exchange_rate`，
   标价方向为 `1 发票币种 = exchange_rate 目标币种`。原币行金额与总额原样保留
   （`lines[].net/tax/gross`、`totals`）；换算结果放在每行的 `converted` 与顶层
   `fx` 段，目标币独立执行一次相同的舍入 + 残差分配，同样有
   `fx.allocations` 审计明细。
6. 每次 `GET` 单张发票时，服务会用库里保存的原始请求重新计算并与存储结果比对，
   `recompute_matched=true` 即表示结果可被同一规则完整复现。
7. **币种小数位按发票固定（配置变更不回溯）**：创建发票时把**原币与目标币**的
   币种小数位一并固定入库（`invoices.currency_digits` 与 `invoices.target_digits`）。
   此后即使经 `CURRENCY_DIGITS` 修改了同一币种的精度，读取与复算该发票也**始终
   沿用其入库时的精度**：例如以 USD 两位精度保存净额 `0.01` 的发票，再把 USD
   配成零位，`GET /api/v1/invoices/{id}` 仍返回两位精度的行、合计与残差分配，
   不会按新精度复算而报 `422`，`recompute_matched` 仍为 `true`。**新建发票仍按
   当前配置校验**（零位 USD 下提交 `0.01` 净额返回 `422`）；同一币种因此可共存
   不同精度的新旧发票（来款分配已按每张发票保存的 `currency_digits` 逐张校验，
   应收账龄汇总与期间税额归集则按各同币种组实际参与记录的**最高入库小数位**
   统一格式化——见 3.11 / 3.12 节）。
   升级前保存的历史发票头表没有目标币精度列，读取时**从已持久化的换算金额**
   （定点、已按目标币最小单位量化）恢复目标币小数位，行为与新发票一致。

负数净额（冲红/贷记凭证）同样支持，规则不变。

### 支持的币种小数位

- 0 位：JPY、KRW、VND、XAF、XOF、ISK、CLP 等；
- 2 位：USD、EUR、CNY、GBP、HKD、AUD、CAD、SGD 等 ISO 4217 常见币种；
- 3 位：KWD、BHD、JOD、OMR、TND 等；
- 4 位：CLF、UYW。

不在内置 ISO 4217 表内的币种会返回 `422`；可用环境变量 `CURRENCY_DIGITS` 扩展
（见下文配置）。

---

## 3. API 一览

Base URL：`http://localhost:8000`（服务在容器内监听 `8000`；Docker Compose 默认
把宿主机 **`8000`** 端口映射到容器 `8000`，故浏览器/curl 直接访问
`http://localhost:8000`，被占用时可用 `API_PORT=8080 docker compose up -d` 改到
宿主机 8080。本地先用 Docker Compose 启动：在仓库根目录执行
`docker compose up -d --build`，停止用 `docker compose down`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查（含数据库探活） |
| `POST` | `/api/v1/invoices` | 创建并计算发票（可选 `due_date`，持久化后返回完整结果），`201` |
| `GET` | `/api/v1/invoices/{id}` | 按 id 查询单张发票（含逐行明细与复算标志） |
| `GET` | `/api/v1/invoices?page=1&page_size=20` | 分页列表（按 id 倒序），`page_size` 范围 1~100 |
| `GET` | `/api/v1/invoices/overdue` | 逾期发票分页查询（按 id 倒序），支持 `page`、`page_size`、可选 `as_of` |
| `POST` | `/api/v1/invoices/{id}/payments` | 登记一笔收款（发票币种字符串、必须大于 0），`201` |
| `GET` | `/api/v1/invoices/{id}/payments` | 查询该发票的收款记录（含各流水已冲销金额与净入账额）与未收余额 |
| `GET` | `/api/v1/invoices/{id}/payment-snapshot?at=...` | 单张发票在指定 UTC 时点（必填、秒精度）的历史收款快照：收款/冲销总额、净已收与未收余额 |
| `POST` | `/api/v1/payments/{payment_id}/reversals` | 冲销一笔收款（可分次，累计不超过原收款），`201` |
| `POST` | `/api/v1/payment-allocations` | 一笔来款在多张待结发票间按到期日顺序分配，保存分配批次并在响应返回 `batch_id`，`201` |
| `POST` | `/api/v1/payment-allocation-batches/{batch_id}/reversals` | 按批次整笔撤销来款分配（同事务按净入账额逐笔追加等额冲销，幂等）：首次写入 `201`，重复请求/无新增 `200`，批次不存在 `404` |
| `POST` | `/api/v1/invoices/{id}/installment-plan` | 为单张发票创建唯一一份分期回款计划（期次按到期日升序、合计等于含税总额），`201`；已存在计划 `409` |
| `GET` | `/api/v1/invoices/{id}/installment-plan?as_of=...` | 查询分期计划：净已收额逐期 FIFO 抵扣，返回每期已覆盖/未覆盖/逾期额；无计划 `404` |
| `POST` | `/api/v1/invoice-number-check` | 供应商发票号码批次查重（1~500 条，NFKC 规范化后同供应商内判重），返回重复组 |
| `POST` | `/api/v1/invoices/{id}/credit-memos` | 对已保存发票的正净额行创建一张可含多行的部分抵扣贷项凭证（累计净额比例摊算税额、完全抵扣恰好抵完原行税额），`201` |
| `GET` | `/api/v1/invoices/{id}/credit-memos` | 按原发票查询全部贷项凭证：逐行净额/税额/含税额与抵扣后累计抵扣、逐原行累计汇总与剩余净额；无凭证时为空 |
| `GET` | `/api/v1/receivables/aging?as_of=YYYY-MM-DD` | 应收账龄汇总：查询日 UTC 结束时点已创建、含税总额为正且仍有未收额的发票，按币种分桶（无到期日 / 未到期 / 逾期 1~30 / 31~60 / 61~90 / 91+），不跨币种相加 |
| `GET` | `/api/v1/tax-ledger?start=YYYY-MM-DD&end=YYYY-MM-DD` | 期间税额归集：UTC 自然日闭区间（含首尾），发票按保存时间、贷项凭证按创建时间选取，原币种 + 规范化税率分组累计开票/抵减/净额（不使用换算列） |
| `GET` | `/docs` `/redoc` `/openapi.json` | 交互式 API 文档 |

### 3.1 创建发票 `POST /api/v1/invoices`

请求体（**所有金额/税率/汇率都用字符串**）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `currency` | string | 是 | 发票币种 ISO 4217 代码，如 `USD` |
| `lines[].net` | string | 是 | 不含税净额，必须是该币种最小单位的整数倍（如 USD 下 `100.00`，JPY 下 `100`）；不得为 `0`，负数表示冲红 |
| `lines[].tax_rate` | string | 是 | 税率，`0`~`1` 小数，`0.13` 表示 13% |
| `lines[].description` | string | 否 | 行备注，最长 500 字 |
| `target_currency` | string | 否 | 换算目标币种；与 `currency` 不同时必填 `exchange_rate` |
| `exchange_rate` | string | 条件必填 | 汇率：`1 currency = exchange_rate target_currency`，必须为正数 |
| `due_date` | string | 否 | 到期日，ISO 8601 **日期** `YYYY-MM-DD`（如 `2026-10-01`）；仅接受日期形状（带时间如 `2026-10-01T00:00:00Z`、不存在的日期均返回 `422`），缺省为 `null` 且不纳入逾期统计 |

跨币种示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/invoices \
  -H 'Content-Type: application/json' \
  -d '{
    "currency": "USD",
    "target_currency": "CNY",
    "exchange_rate": "7.123456",
    "lines": [
      {"description": "咨询服务", "net": "100.00", "tax_rate": "0.13"},
      {"description": "差旅",     "net": "50.00",  "tax_rate": "0.06"}
    ]
  }'
```

要点：原币 `totals.total_tax = 16.00`；换算到 CNY 后逐行税额 92.60/21.37，
精确合计 113.975296 → 113.98，产生 +0.01 残差，分给零头更大的第 1 行
（`fx.allocations`），最终 CNY 税额 92.61 + 21.37 = **113.98**。

### 3.2 查询与列表

```bash
# 单张
curl -s http://localhost:8000/api/v1/invoices/1

# 分页列表
curl -s 'http://localhost:8000/api/v1/invoices?page=1&page_size=20'
```

分页响应：

```json
{
  "items": [
    {"id": 2, "currency": "USD", "target_currency": "CNY",
     "total_net": "150.00", "total_tax": "16.00", "total_gross": "166.00",
     "line_count": 2, "created_at": "2026-09-23T01:41:00Z"}
  ],
  "page": 1, "page_size": 20, "total": 2, "pages": 1
}
```

### 3.3 收款登记与未收余额

**登记收款** `POST /api/v1/invoices/{id}/payments`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `amount` | string | 是 | 收款金额，**发票币种**的十进制字符串；必须 **大于 0**，且为币种最小单位的整数倍（USD 下 `20.00`，JPY 下 `2000`） |

规则：

- 每张发票的**累计收款不得超过含税总额** `total_gross`；超额请求返回 `422`，
  事务回滚，**不留下任何收款记录**。
- 余额校验与收款写入在**同一个 SQLite 事务**中完成（`BEGIN IMMEDIATE` 串行化
  并发写入），并发登记不会超收。
- 最小单位校验、量化与余额计算都在按金额量级自动放大的局部精度内进行：
  超大金额发票（如 30 位整数净额）登记 `0.01` 后未收余额仍精确到分位，
  登记大额收款同样精确，收款响应与后续查询、快照的余额口径一致。
- 每笔收款保存登记时间 `created_at`（UTC，ISO 8601）。

```bash
# 登记 20.00 USD（发票 total_gross = 33.11）
curl -s -X POST http://localhost:8000/api/v1/invoices/1/payments \
  -H 'Content-Type: application/json' \
  -d '{"amount": "20.00"}'
```

响应 `201`（收款记录 + 最新余额；`paid_total`/`outstanding` 均按净入账额计算）：

```json
{
  "id": 1,
  "invoice_id": 1,
  "amount": "20.00",
  "currency": "USD",
  "created_at": "2026-09-23T02:10:00Z",
  "reversed_total": "0.00",
  "net_amount": "20.00",
  "paid_total": "20.00",
  "outstanding": "13.11"
}
```

**查询收款记录与未收余额** `GET /api/v1/invoices/{id}/payments`：

```bash
curl -s http://localhost:8000/api/v1/invoices/1/payments
```

```json
{
  "invoice_id": 1,
  "currency": "USD",
  "total_gross": "33.11",
  "paid_total": "20.00",
  "reversed_total": "0.00",
  "outstanding": "13.11",
  "payment_count": 1,
  "payments": [
    {"id": 1, "invoice_id": 1, "amount": "20.00", "currency": "USD",
     "created_at": "2026-09-23T02:10:00Z",
     "reversed_total": "0.00", "net_amount": "20.00"}
  ]
}
```

每条流水附带 `reversed_total`（该流水已冲销金额合计）与 `net_amount`
（净入账额 = `amount` − `reversed_total`）；顶层 `paid_total` 为全发票
**净入账合计**，`outstanding` = `total_gross` − `paid_total`。

### 3.4 收款冲销 `POST /api/v1/payments/{payment_id}/reversals`

对已登记的一笔收款流水做冲销（红冲/退款等场景）。`payment_id` 为收款登记
（或来款分配）响应中的流水 `id`。

请求体（金额用字符串）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `amount` | string | 是 | 本次冲销金额，**发票币种**的十进制字符串；必须 **大于 0**、为币种最小单位的整数倍，且与历史冲销**累计不得超过原收款金额** |

规则：

- **允许同一笔收款分次冲销**：每次冲销独立成一条记录，累计冲销 ≤ 原收款金额。
- 冲销记录写入独立的 `payment_reversals` 表并关联原流水，**不删除原收款**；
  原流水在收款查询中始终可见（`amount` 不变，`reversed_total`/`net_amount` 实时反映冲销）。
- 冲销后全库按**净入账额**（收款 − 冲销）计算：该发票未收余额相应回升、
  可能重新进入逾期列表，释放的余额可被后续收款/来款分配再次使用。
- 累计冲销计算与写入在**同一个 `BEGIN IMMEDIATE` 写事务**内完成，与收款登记、
  来款分配共用同一串行化机制：并发冲销不会超额。
- 任何校验失败都回滚，**不留下任何冲销记录**：流水不存在 → `404`；
  金额 ≤ 0、非最小单位整数倍、累计超额 → `422`。

```bash
# 冲销收款流水 1 中的 5.00 USD（原收款 20.00）
curl -s -X POST http://localhost:8000/api/v1/payments/1/reversals \
  -H 'Content-Type: application/json' \
  -d '{"amount": "5.00"}'
```

响应 `201`（冲销记录 + 原流水最新累计冲销与净入账额）：

```json
{
  "id": 1,
  "payment_id": 1,
  "invoice_id": 1,
  "amount": "5.00",
  "currency": "USD",
  "created_at": "2026-09-23T04:00:00Z",
  "reversed_total": "5.00",
  "net_amount": "15.00"
}
```

随后查询 `GET /api/v1/invoices/1/payments` 可见：`paid_total` 降为 `15.00`、
`reversed_total` 为 `5.00`、`outstanding` 回升为 `18.11`，流水 1 的
`net_amount` 为 `15.00`；再冲 `15.00` 即冲完（`net_amount` 归 `0.00`），
此后对该流水的任何冲销都返回 `422`。

### 3.5 逾期发票查询 `GET /api/v1/invoices/overdue`

查询参数：

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | int | 否 | `1` | 页码，从 1 开始；`< 1` 返回 `422` |
| `page_size` | int | 否 | `20` | 每页条数，范围 1~100；越界返回 `422` |
| `as_of` | string | 否 | UTC 当前日期 | 截止日，ISO 8601 **日期** `YYYY-MM-DD`；格式不符或日期不存在（含带时间成分）返回 `422` |

**逾期口径**（三者同时满足）：

1. 发票设置了 `due_date`（**缺少到期日的不纳入**）；
2. `due_date` **严格早于** `as_of`（到期日当天不算逾期）；
3. **未收余额大于零**（`total_gross − 净入账合计 > 0`，净入账 = 收款 − 冲销），
   已收满的发票不纳入；收款被冲销后余额回升，发票会重新进入逾期列表。

结果**按 id 倒序**分页；每条包含发票 `id`、`currency`、`due_date`、含税总额
`total_gross`、净入账合计 `paid_total` 与精确未收余额 `outstanding`（均为发票币种的
十进制字符串，余额计算全程 `Decimal`，沿用收款登记的最小单位规则），响应体额外
回显实际使用的 `as_of`。

```bash
# 指定截止日
curl -s 'http://localhost:8000/api/v1/invoices/overdue?page=1&page_size=20&as_of=2026-10-15'

# 不传 as_of：以 UTC 当前日期为截止日
curl -s 'http://localhost:8000/api/v1/invoices/overdue'
```

```json
{
  "items": [
    {
      "id": 1,
      "currency": "USD",
      "due_date": "2026-09-30",
      "total_gross": "33.11",
      "paid_total": "20.00",
      "outstanding": "13.11",
      "created_at": "2026-09-23T01:40:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1,
  "pages": 1,
  "as_of": "2026-10-15"
}
```

无逾期发票时 `items` 为空、`total`/`pages` 为 `0`。

### 3.6 历史收款快照 `GET /api/v1/invoices/{id}/payment-snapshot`

查询单张发票在**指定 UTC 时点**的历史收款情况，常用于对账、审计与「某时点账龄」回放。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `at` | string | 是 | 快照截止时点，UTC **秒精度**时间 `YYYY-MM-DDTHH:MM:SSZ`（如 `2026-10-15T12:00:00Z`）；只接受这一种形状（`+00:00` 偏移、带毫秒、空格分隔、日期/时间不存在均返回 `422`） |

统计口径：

1. **截止秒为闭区间**：收款与冲销均按各自的登记时间 `created_at <= at` 筛选，
   截止当秒写入的流水**全部计入**。
2. 分别统计：该时点之前及当秒的**收款总额** `received_total`，以及这些收款
   对应的**冲销总额** `reversed_total`（冲销按其自身登记时间独立过滤）。
3. **净已收** `net_paid = received_total − reversed_total`；
   **未收余额** `outstanding = total_gross − net_paid`。
   全程按发票币种最小单位用 `Decimal` 计算（USD/CNY 保留 2 位、JPY 为整数、KWD 3 位）。
4. 查询只读历史行：**截止时点之后写入的任何收款/冲销都不参与，历史结果不会
   随后续流水改变**——对同一 `at` 重复查询结果恒定。
5. 时点早于发票创建时间（`invoice_created_at`）返回 `422`；发票不存在返回 `404`。

响应同时回显纳入快照的收款（每条带当点口径的 `reversed_total`/`net_amount`）
与冲销明细及各自计数，便于审计核对。

```bash
curl -s 'http://localhost:8000/api/v1/invoices/1/payment-snapshot?at=2026-10-15T12:00:00Z'
```

```json
{
  "invoice_id": 1,
  "currency": "USD",
  "at": "2026-10-15T12:00:00Z",
  "invoice_created_at": "2026-09-23T01:40:00Z",
  "total_gross": "33.11",
  "received_total": "20.00",
  "reversed_total": "5.00",
  "net_paid": "15.00",
  "outstanding": "18.11",
  "payment_count": 1,
  "reversal_count": 1,
  "payments": [
    {"id": 1, "invoice_id": 1, "amount": "20.00", "currency": "USD",
     "created_at": "2026-10-01T08:00:00Z",
     "reversed_total": "5.00", "net_amount": "15.00"}
  ],
  "reversals": [
    {"id": 1, "payment_id": 1, "invoice_id": 1, "amount": "5.00",
     "currency": "USD", "created_at": "2026-10-05T09:30:00Z"}
  ]
}
```

JPY 等 0 位小数币种的快照金额同样不带小数位（如 `"received_total": "1000"`、
`"reversed_total": "600"`、`"net_paid": "400"`、`"outstanding": "600"`）。

### 3.7 来款分配 `POST /api/v1/payment-allocations`

把一笔来款在**多张同币种待结发票**间按到期日顺序逐张抵扣。

请求体（金额用字符串）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `currency` | string | 是 | 来款币种 ISO 4217 代码；只分配给同币种发票，**不做跨币种换算** |
| `amount` | string | 是 | 来款总金额，必须 **大于 0**；不按单一币种精度统一校验——按实际抵扣顺序逐张计算分配额，并以**每张发票保存的** `currency_digits` 最小单位逐张校验，整笔可精确分配才成功（两位批次 `100.00`、零位批次 `1000`） |
| `invoice_ids` | int[] | 是 | 待结发票 ID 列表，**至少 1 个且不得重复**；只处理列表中的发票 |

**抵扣规则**：

1. 将列表内发票按 `due_date` **升序**排列——**未设置到期日的发票排最后**；
   同一到期日（或都未设置到期日）按发票 **ID 升序**。
2. 按该顺序逐张抵扣，每张分配 `min(剩余来款, 该发票正未收余额)`；来款用尽即止，
   之后的发票分配 `0`（**不产生**收款流水）。未收余额按**净入账额**
   （收款 − 冲销）计算，冲销释放的余额可再次被分配。
3. **负额冲红 / 已结清发票不参与抵扣**：候选发票中未收余额 ≤ 0 的（负数冲红单、
   已收满的发票）在结果中分配 `0`、`payment_id` 为 `null`，既不消耗也不增加
   剩余来款；可分配余额合计同样只计入**正未收额**。成功时各笔实际收款合计
   **恒等于来款金额**（`allocated_total == amount`）。
4. **逐张最小单位校验**：每张发票以**其保存时的** `currency_digits` 最小单位
   校验自己的实际分配额（>0 部分），通过后按该发票自己的位数规范化存储/展示
   （零位发票上的分配额为整数，如 `"3"`）。任一张落不下对应零头（如 `0.01`
   落到零位发票）即整笔 `422`、事务回滚，**不会留下任何部分收款**。
5. 列表内发票**正未收余额合计小于来款金额**时，整笔请求返回 `422`，事务回滚，
   **不会留下任何部分收款**。

**校验（全部不通过则整笔拒绝，无任何写入）**：

- 列表中任一发票不存在 → `404`；
- 任一发票币种与来款不一致 → `422`；
- 金额 ≤ 0、非十进制字符串 → `422`；
- 按实际抵扣顺序计算出的某张发票实际分配额不是**该发票保存精度**最小单位整数倍
  （整笔无法按各发票精度精确分配）→ `422`；
- 列表为空或包含重复 ID → `422`；
- 正未收额合计不足（负额冲红与已结清发票不计入）→ `422`。

#### 负额冲红单边界（候选发票未收余额 ≤ 0）

负数冲红发票（`total_gross` 为负，未收余额为负）与已结清发票（未收余额为 0）
可以出现在 `invoice_ids` 列表中，但**不参与抵扣**：

- 结果中这些发票 `allocated` 为 `0`（按各自保存精度展示，如 `"0.00"`）、
  `payment_id` 为 `null`，**不产生收款流水**；`outstanding` 回显其真实余额
  （冲红单为负值，如 `"-100.00"`），分配前后不变；
- 它们**不消耗也不增加剩余来款**——负未收余额绝不会被当作分配额，剩余来款
  不会因此越滚越大（例如 50.00 来款不会因列表中有一张 -100.00 冲红单而实际
  写入 150.00 收款）；成功时各笔收款合计恒等于来款金额；
- 可分配余额合计只计入**正未收额**：正未收额合计小于来款金额时整笔 `422`，
  批次与收款均不落库；列表中全是负额/已结清发票时任何来款都 `422`；
- 正额发票的抵扣顺序不受影响，仍按到期日升序（无到期日排最后）、同日按 ID
  升序逐张抵扣。

#### 混合精度边界（同币种、不同 `currency_digits`）

币种精度配置可经 `CURRENCY_DIGITS` 变更，但**已保存发票不回溯改写**：同一币种
代码下，旧发票可能保留两位小数、新发票按零位保存。来款分配的口径是：

- **排序只看到期日与 ID**，与精度无关；抵扣顺序仍是到期日升序（无到期日最后）、
  同日 ID 升序。
- **最小单位逐张判定**：先按实际抵扣顺序算出每张发票的分配额，再逐张以该发票
  保存的 `currency_digits` 判定是否为其最小单位整数倍，而不是只看列表首张发票
  的位数。
- 例：旧发票 A（两位，未收 `5.00`，先到期）+ 新发票 B（零位，未收 `10`）：
  - 来款 `5.01` → A 抵 `5.00`、余 `0.01` 需落到 B，`0.01` 不是 B 的最小单位
    整数倍 → **整笔 `422`，批次/收款/明细均不落库**（绝不会把 `0.01` 写进零位
    发票）；
  - 来款 `8.00` → A 抵 `5.00`、B 抵 `3`，各自都是自身最小单位整数倍 → 成功，
    A 的金额带两位、B 的金额为整数；
  - 若列表中先到期的两位发票未收就足以吸收整笔（如另一张两位发票未收 `5.01`、
    来款 `5.01`，零位 B 本次实际分配为 `0`），来款合法、**不会被误拒绝**。
- 整笔成功后的**批次合计**按列表内最细精度展示（上例为 `8.00`）；按批次整批
  撤销同样逐笔按各发票净入账额冲销，批次合计保留最细位数。

余额读取、校验与多条流水写入在**同一个 `BEGIN IMMEDIATE` 写事务**内完成，
与单张收款接口共用同一串行化机制：来款分配之间、来款分配与单张收款并发时
都不会超收。

```bash
# 假设发票 2（due 2026-09-05，gross 22.00）、1（due 2026-09-10，gross 11.00）、
# 4（due 2026-09-10，gross 44.00）、3（无到期日，gross 33.00）
curl -s -X POST http://localhost:8000/api/v1/payment-allocations \
  -H 'Content-Type: application/json' \
  -d '{"currency": "USD", "amount": "50.00", "invoice_ids": [3, 4, 2, 1]}'
```

即使入参顺序是 `[3, 4, 2, 1]`，响应仍按实际抵扣顺序返回 `2 → 1 → 4 → 3`，
`201`：

```json
{
  "batch_id": 1,
  "currency": "USD",
  "amount": "50.00",
  "allocated_total": "50.00",
  "created_at": "2026-09-23T03:00:00Z",
  "allocations": [
    {"invoice_id": 2, "due_date": "2026-09-05", "allocated": "22.00",
     "outstanding": "0.00", "payment_id": 10},
    {"invoice_id": 1, "due_date": "2026-09-10", "allocated": "11.00",
     "outstanding": "0.00", "payment_id": 11},
    {"invoice_id": 4, "due_date": "2026-09-10", "allocated": "17.00",
     "outstanding": "27.00", "payment_id": 12},
    {"invoice_id": 3, "due_date": null, "allocated": "0.00",
     "outstanding": "33.00", "payment_id": null}
  ]
}
```

- 顶层 **`batch_id`** 即本次分配的**批次标识**：分配成功时在同一事务内保存批次头
  与本次实际生成（`allocated > 0`）的收款关联，供整批撤销使用（见下一小节）。
- `allocated` 为该发票本次分配金额，`outstanding` 为抵扣后各自剩余未收余额；
- `payment_id` 指向沿用现有 `payments` 表写入的流水 ID；分配为 `0` 时为 `null`。
  因此 `GET /api/v1/invoices/{id}/payments` 与逾期分页查询都能看到分配结果，
  与单张收款登记完全一致。

### 3.7.1 按批次整笔撤销 `POST /api/v1/payment-allocation-batches/{batch_id}/reversals`

对一次跨发票来款分配做**整批撤销**（整笔退款/分配作废等场景）。`batch_id` 为来款
分配响应中的批次标识；请求体为空（无入参）。

撤销规则（全部在**同一个 `BEGIN IMMEDIATE` 写事务**内完成，与收款登记、手工冲销、
来款分配共用同一串行化机制）：

1. 按原分配抵扣顺序（`seq` 升序）逐笔取批次内本次实际生成的收款流水；
2. 逐笔计算当前**净入账额 = 收款金额 − 该收款已有冲销合计**——已有冲销既包括本接口
   此前生成的冲销，也包括通过 `POST /api/v1/payments/{payment_id}/reversals` 做的
   手工冲销；
3. **仅对正余额（净入账额 > 0）追加一条等额冲销**，并把冲销流水关联回批次明细；
   若某笔已被手工部分冲销，本次只冲剩余净余额（如原收款 20.00、已手工冲 8.00，
   整批撤销只再冲 12.00）；净入账额已为 0 的收款**不新增任何冲销**。

**幂等**：第一次请求为每笔仍有正余额的收款各追加一条冲销；再次请求时这些收款的净
入账额已为 0，**不会新增第二条冲销**，响应内容与首次一致（各笔撤销金额、冲销流水
ID、批次合计恒定）。批次撤销前已被手工全额冲完的收款，其撤销金额为 `"0.00"`、
`reversal_id` 为 `null`，同样不新增冲销。

- **首次实际写入至少一条冲销 -> `201`；重复请求（或批次收款此前已全部手工冲完、
  本次无新增冲销）-> `200`**；批次不存在 -> `404`。
- 生成的冲销写入现有 `payment_reversals` 表，与手工冲销**同表同口径**：撤销后各
  发票未收余额按净入账额回升、可能重新进入逾期列表，释放的余额可被后续收款/来款
  分配再次使用；历史快照、分期计划、账龄汇总全部自动反映。
- **旧的无批次收款**（批次功能上线前登记、或通过单张收款接口登记的流水）不在批次
  明细中，没有 `batch_id`，仍通过 `POST /api/v1/payments/{payment_id}/reversals`
  查询与逐笔冲销，行为不变。

```bash
# 撤销 batch_id=1 的来款分配（首次 -> 201；再发一次同样的请求 -> 200 且内容一致）
curl -s -X POST http://localhost:8000/api/v1/payment-allocation-batches/1/reversals
```

```json
{
  "batch_id": 1,
  "currency": "USD",
  "created_at": "2026-09-23T03:00:00Z",
  "allocated_total": "50.00",
  "reversed_total": "42.00",
  "reversal_count": 3,
  "items": [
    {"seq": 1, "payment_id": 10, "invoice_id": 2, "allocated": "22.00",
     "reversed_amount": "22.00", "reversal_id": 21,
     "reversal_created_at": "2026-09-23T05:00:00Z",
     "net_amount": "0.00", "currency": "USD"},
    {"seq": 2, "payment_id": 11, "invoice_id": 1, "allocated": "11.00",
     "reversed_amount": "11.00", "reversal_id": 22,
     "reversal_created_at": "2026-09-23T05:00:00Z",
     "net_amount": "0.00", "currency": "USD"},
    {"seq": 3, "payment_id": 12, "invoice_id": 4, "allocated": "17.00",
     "reversed_amount": "9.00", "reversal_id": 23,
     "reversal_created_at": "2026-09-23T05:00:00Z",
     "net_amount": "0.00", "currency": "USD"}
  ]
}
```

上例中第 3 笔原分配 17.00 在撤销前已被手工冲销 8.00，故整批撤销只追加 9.00；批次
合计 `reversed_total` = 22.00 + 11.00 + 9.00 = **42.00**（`reversal_count` 为批次
已关联的冲销笔数；手工冲销不计入）。每个条目：`allocated` 为原分配金额，
`reversed_amount` 为该笔的撤销金额（重复请求恒定），`reversal_id` 为整批撤销生成的
冲销流水（无新增时为 `null`），`net_amount` 为撤销后该收款的净入账额。JPY 等 0 位
小数币种金额同样不带小数位。

### 3.8 分期回款计划

每张发票可以创建**唯一一份**分期回款计划：计划本身只保存期次号、到期日与金额，
每期是否已覆盖完全在查询时按现有收款数据（净已收额 = 收款 − 冲销）实时计算，
**冲销、再收款后重复查询会得到最新覆盖状态，而计划行（期次/到期日/金额）永不改写**。

**创建计划** `POST /api/v1/invoices/{id}/installment-plan`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `installments` | array | 是 | 期次列表，至少 1 期，顺序即期次号（第 1 期、第 2 期……） |
| `installments[].due_date` | string | 是 | 本期到期日，ISO 8601 **日期** `YYYY-MM-DD`；整个列表必须按到期日**升序**排列（**允许同日多期**），乱序返回 `422` |
| `installments[].amount` | string | 是 | 本期金额，**发票币种**十进制字符串，必须 **大于 0** 且为币种最小单位的正整数倍（USD `10.00`、JPY `400`） |

创建规则：

- **每张发票只允许一份计划**：重复创建返回 `409`；发票不存在返回 `404`。
- **期次合计必须恰好等于发票含税总额** `total_gross`（如 `33.11`），否则 `422`。
- 发票存在性、计划唯一性、正数/最小单位、合计校验与计划头/期次写入在
  **同一个 `BEGIN IMMEDIATE` 事务**内完成，任一校验失败整体回滚，不留任何计划行。
- 已有收款流水的发票也可以创建计划；创建响应直接按 **UTC 当日**计算一次覆盖状态。

```bash
# 发票 1 含税总额 33.11 USD
curl -s -X POST http://localhost:8000/api/v1/invoices/1/installment-plan \
  -H 'Content-Type: application/json' \
  -d '{"installments": [
    {"due_date": "2026-09-01", "amount": "10.00"},
    {"due_date": "2026-10-01", "amount": "13.11"},
    {"due_date": "2026-11-01", "amount": "10.00"}
  ]}'
```

响应 `201`（与查询接口同构，`as_of` 为创建时的 UTC 当日）：

```json
{
  "plan_id": 1,
  "invoice_id": 1,
  "currency": "USD",
  "created_at": "2026-09-24T08:00:00Z",
  "as_of": "2026-09-24",
  "total_gross": "33.11",
  "net_paid": "0.00",
  "covered_total": "0.00",
  "uncovered_total": "33.11",
  "overdue_total": "10.00",
  "installments": [
    {"installment": 1, "due_date": "2026-09-01", "amount": "10.00",
     "covered": "0.00", "uncovered": "10.00", "overdue": "10.00"},
    {"installment": 2, "due_date": "2026-10-01", "amount": "13.11",
     "covered": "0.00", "uncovered": "13.11", "overdue": "0.00"},
    {"installment": 3, "due_date": "2026-11-01", "amount": "10.00",
     "covered": "0.00", "uncovered": "10.00", "overdue": "0.00"}
  ]
}
```

**查询计划** `GET /api/v1/invoices/{id}/installment-plan`：

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `as_of` | string | 否 | UTC 当前日期 | 查询日期 `YYYY-MM-DD`；仅到期日**严格早于**该日的未覆盖额计为逾期额（到期日当天不逾期）；非法日期 `422` |

抵扣与逾期口径：

1. 取发票当前**净已收额**（Σ 收款 − Σ 冲销，与收款查询、逾期查询、来款分配同一口径，
   全程 `Decimal`、按发票币种最小单位取整）。
2. **按期次顺序逐期 FIFO 抵扣**：先覆盖第 1 期，剩余净已收再覆盖第 2 期，依次类推。
   每期返回：
   - `covered`：已覆盖额（`0 ≤ covered ≤ amount`）；
   - `uncovered`：未覆盖额 = `amount − covered`；
   - `overdue`：逾期额 = 仅当 `due_date < as_of` 时等于 `uncovered`，否则为 `0`
     （到期日当天不算逾期）。
3. 顶层汇总 `covered_total` / `uncovered_total` / `overdue_total` 分别为各期之和，
   恒有 `covered_total + uncovered_total = total_gross`。
4. 查询是**纯读计算**：收款被冲销后净已收回降，较后期次会**重新打开**
   （`uncovered` 回升、可能重新计逾期）；原计划的期次、到期日、金额始终不变。
5. 发票不存在、或发票尚未创建计划均返回 `404`。

```bash
# 登记 20.00 收款后：第 1 期覆盖满 10.00，第 2 期覆盖 10.00/未覆盖 3.11（已到期），
# 第 3 期未到期、未覆盖但不计逾期
curl -s -X POST http://localhost:8000/api/v1/invoices/1/payments \
  -H 'Content-Type: application/json' -d '{"amount": "20.00"}'

curl -s 'http://localhost:8000/api/v1/invoices/1/installment-plan?as_of=2026-10-15'
```

```json
{
  "plan_id": 1,
  "invoice_id": 1,
  "currency": "USD",
  "created_at": "2026-09-24T08:00:00Z",
  "as_of": "2026-10-15",
  "total_gross": "33.11",
  "net_paid": "20.00",
  "covered_total": "20.00",
  "uncovered_total": "13.11",
  "overdue_total": "3.11",
  "installments": [
    {"installment": 1, "due_date": "2026-09-01", "amount": "10.00",
     "covered": "10.00", "uncovered": "0.00", "overdue": "0.00"},
    {"installment": 2, "due_date": "2026-10-01", "amount": "13.11",
     "covered": "10.00", "uncovered": "3.11", "overdue": "3.11"},
    {"installment": 3, "due_date": "2026-11-01", "amount": "10.00",
     "covered": "0.00", "uncovered": "10.00", "overdue": "0.00"}
  ]
}
```

随后冲销该笔收款中的 8.00（`POST /api/v1/payments/{payment_id}/reversals`），
净已收降为 12.00，再次查询同一 `as_of`：第 1 期仍覆盖 10.00，第 2 期变为已覆盖
2.00 / 未覆盖 11.11（逾期 11.11），第 3 期全额重新打开；`installments[].amount`
与 `due_date` 与创建时完全一致。0 位小数币种（JPY 等）金额同样不带小数位。

### 3.9 供应商发票号码批次查重 `POST /api/v1/invoice-number-check`

录入供应商发票前批量检查号码是否在同一供应商下重复。接口**无状态、不落库、不涉及金额**，
一次提交 **1~500 条**记录。

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `items` | array | 是 | 查重条目列表，长度 1~500 |
| `items[].supplier_code` | string | 是 | 供应商编号，规范化后不得为空 |
| `items[].invoice_number` | string | 是 | 供应商发票号码，**原始**值不得为空、长度不超过 **100 个 Unicode 字符** |

规范化规则（供应商编号与发票号码都做同样处理，规范化结果用于比较）：

1. **Unicode NFKC** 规范化（兼容分解 + 规范组合）：全角字母数字 `ＡＢＣ１２３`
   折叠为半角 `ABC123`，全角连字符/减号、连字、上下标兼容字符等折叠为规范形式；
2. 去除**首尾空白**（Unicode 意义上的空白，含全角空格 `　`、制表符、换行）；
3. **大小写折叠** `casefold`（比 `lower` 更彻底，如 `ß → ss`、`K`（Kelvin）`→ k`）。

判重与响应：

- 只在**同一供应商内**比较：不同供应商即使号码完全相同也不算重复；
- 只有规范化后号码相同且出现 **≥ 2 次**才构成重复组；未重复号码不列出；
- 重复组按**组内最早输入位置升序**排列；组内 `positions` 为全部输入位置
  （**从 1 开始**，升序），`original_invoice_numbers` 为对应位置的**原始号码**
  （不做任何修剪，原样回显）；`normalized_invoice_number` 为规范化后的号码；
- `supplier_code` 回显组内**首次出现位置的原始值**；
- 无重复时返回 `{"items": []}`。

```bash
curl -s -X POST http://localhost:8000/api/v1/invoice-number-check \
  -H 'Content-Type: application/json' \
  -d '{"items": [
    {"supplier_code": "SUP-A", "invoice_number": "INV-001"},
    {"supplier_code": "sup-a", "invoice_number": " inv-001 "},
    {"supplier_code": "SUP-A", "invoice_number": "INV-002"},
    {"supplier_code": "SUP-B", "invoice_number": "INV-001"}
  ]}'
```

上例中第 1、2 条属于同一供应商（`SUP-A` 与 `sup-a` 规范化后相同），号码
`INV-001` 与 ` inv-001 ` 规范化后均为 `inv-001`，构成重复；第 4 条虽然号码
相同但属于另一供应商 `SUP-B`，不重复。响应 `200`：

```json
{
  "items": [
    {
      "supplier_code": "SUP-A",
      "normalized_invoice_number": "inv-001",
      "positions": [1, 2],
      "original_invoice_numbers": ["INV-001", " inv-001 "]
    }
  ]
}
```

校验失败返回 `422`：`items` 为空或超过 500 条、缺少字段、供应商编号规范化后为空、
发票号码（原始值）为空/纯空白、发票号码超过 100 个 Unicode 字符。注意长度按
**Unicode 码点**计数（每个 emoji/汉字均计 1 个字符）。

### 3.10 原发票贷项凭证（部分抵扣）

对**已保存发票**的正净额原行开具贷项凭证（credit memo）：一张凭证可以同时抵扣
多个原行，也可以对同一原行分多次部分抵扣。凭证只追加到独立的
`credit_memos` / `credit_memo_lines` 两张表，**原发票（逐行与汇总金额）与收款
流水（收款、冲销、未收余额、逾期列表、分期计划视图）始终保持原样、不受影响**。

**创建凭证** `POST /api/v1/invoices/{id}/credit-memos`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `lines` | array | 是 | 本次抵扣明细，至少 1 行；行在响应中按提交顺序回显 |
| `lines[].line_no` | int | 是 | 被抵扣的**原发票行号**（创建发票时按输入顺序从 1 开始）；必须为正整数且该原行存在，**同一凭证内不得重复** |
| `lines[].net` | string | 是 | **本次抵扣净额**，发票币种十进制字符串；必须 **大于 0** 且为币种最小单位的正整数倍（USD `5.00`、JPY `500`） |

校验与计算规则：

1. **只允许抵扣正净额原行**：引用不存在的行号、或原行净额 ≤ 0（负数冲红行）
   均返回 `422`；本次抵扣净额 ≤ 0、不是币种最小单位整数倍、凭证内行号重复同样
   返回 `422`。
2. **累计抵扣净额不得超过原行净额**：同一原行历次凭证的 `net` 合计加上本次净额
   必须 `≤` 原行 `net`，否则返回 `422`，事务回滚、**整张凭证不写入任何行**。
3. **税额按累计净额比例摊算**（全程 Decimal 高精度，结果按发票币种最小单位
   `ROUND_HALF_UP`）：

   ```
   抵扣后累计应记税额 = round(原行已确认税额 × 抵扣后累计净额 ÷ 原行净额)
   本次抵扣税额       = 抵扣后累计应记税额 − 历次已记税额
   本次抵扣含税额     = 本次抵扣净额 + 本次抵扣税额
   ```

   按“累计口径”取整后再减历次已记税额，多次部分抵扣的税额之和与一次性按同一
   累计净额抵扣的结果一致。
4. **完全抵扣恰好抵完原行税额**：当本次后累计净额恰好等于原行净额时，累计应记
   税额强制等于原行已确认税额（`invoice_lines[].tax`），因此无论之前分多少次、
   每次比例如何取整，逐次抵扣税额之和恒等于原行税额，不多也不少。
5. 发票存在性、重复行号、原行存在性/正净额、金额最小单位、累计超额等全部检查
   与凭证头/凭证行写入在**同一个 `BEGIN IMMEDIATE` 写事务**内完成，并发对同一
   原行抵扣不会超额。

```bash
# 发票 1：第 1 行 net 10.00、tax 1.30；第 2 行 net 3.34、tax 0.43（13%）
# 先抵扣第 1 行的 3.34 与第 2 行的 1.00
curl -s -X POST http://localhost:8000/api/v1/invoices/1/credit-memos \
  -H 'Content-Type: application/json' \
  -d '{"lines": [
    {"line_no": 1, "net": "3.34"},
    {"line_no": 2, "net": "1.00"}
  ]}'
```

响应 `201`（行序与提交一致；每行含本次净额/税额/含税额与**抵扣后累计**抵扣）：

```json
{
  "id": 1,
  "invoice_id": 1,
  "currency": "USD",
  "created_at": "2026-09-24T09:00:00Z",
  "net_total": "4.34",
  "tax_total": "0.56",
  "gross_total": "4.90",
  "lines": [
    {"line_no": 1, "net": "3.34", "tax": "0.43", "gross": "3.77",
     "cumulative_net": "3.34", "cumulative_tax": "0.43"},
    {"line_no": 2, "net": "1.00", "tax": "0.13", "gross": "1.13",
     "cumulative_net": "1.00", "cumulative_tax": "0.13"}
  ]
}
```

- 第 1 行：`round(1.30 × 3.34 / 10.00) = round(0.4342) = 0.43`；
- 第 2 行：`round(0.43 × 1.00 / 3.34) = round(0.1287…) = 0.13`。

对第 1 行继续抵扣 `3.33`（累计 6.67）时，本次税额为
`round(1.30 × 6.67 / 10.00) − 0.43 = round(0.8671) − 0.43 = 0.87 − 0.43 = 0.44`；
最后再抵 `3.33`（累计净额恰好 10.00），累计税额恰好为 `1.30`、本次税额 `0.43`，
三次税额 `0.43 + 0.44 + 0.43 = 1.30`，**原行税额被恰好抵完**。

**按原发票查询** `GET /api/v1/invoices/{id}/credit-memos`：

```bash
curl -s http://localhost:8000/api/v1/invoices/1/credit-memos
```

- 顶层 `credited_net` / `credited_tax` / `credited_gross` 为该发票全部凭证的
  累计抵扣合计；`credit_count` 为凭证张数。
- `line_credits` 按**原行号**汇总每个被抵扣原行当前的累计抵扣净额/税额/含税额
  与 `remaining_net`（原行净额 − 累计抵扣净额，完全抵扣后为 `"0.00"`）。
- `memos` 按凭证创建顺序（id 升序）列出全部凭证，凭证内各行按 `line_no` 升序，
  每行带本次 `net` / `tax` / `gross` 与该时点的 `cumulative_net` /
  `cumulative_tax`。无凭证时 `credit_count` 为 `0`、两个列表均为空；发票不存在
  返回 `404`。

```json
{
  "invoice_id": 1,
  "currency": "USD",
  "total_net": "13.34",
  "total_tax": "1.73",
  "total_gross": "15.07",
  "credit_count": 1,
  "credited_net": "4.34",
  "credited_tax": "0.56",
  "credited_gross": "4.90",
  "line_credits": [
    {"line_no": 1, "credited_net": "3.34", "credited_tax": "0.43",
     "credited_gross": "3.77", "remaining_net": "6.66"},
    {"line_no": 2, "credited_net": "1.00", "credited_tax": "0.13",
     "credited_gross": "1.13", "remaining_net": "2.34"}
  ],
  "memos": [ /* 与创建接口同构的完整凭证对象，行按 line_no 升序 */ ]
}
```

JPY 等 0 位小数币种同样支持：金额不带小数位，税额按整数四舍五入，完全抵扣时
累计税额同样恰好等于原行税额（如 net `1000`、tax `100` 的行，依次抵 `333` /
`333` / `334`，三次税额为 `33` / `34` / `33`，合计 `100`）；`0.5` 等小于币种
最小单位的金额返回 `422`。

### 3.11 应收账龄汇总 `GET /api/v1/receivables/aging`

按**查询日 UTC 结束时点**汇总全部应收发票的账龄，常用于月末/季末对账。

查询参数：

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `as_of` | string | 否 | UTC 当前日期 | 查询日期，ISO 8601 **日期** `YYYY-MM-DD`；格式不符或日期不存在（含带时间成分）返回 `422` |

统计口径：

1. **时点**：`as_of` 当日 UTC `23:59:59Z`（截止秒为**闭区间**，当秒写入的发票、
   收款、冲销全部计入）；响应回显实际使用的 `cutoff_at`。
2. **纳入条件**（三者同时满足）：
   - 发票在该时点前**已创建**（`created_at <= cutoff_at`）；
   - 含税总额 `total_gross` **严格为正**（负数冲红发票不纳入）；
   - 截至该时点的**未收额大于零**：`未收额 = total_gross − 净入账合计`，
     净入账 = 截止前收款 − 截止前冲销（与历史收款快照同一口径，全程 `Decimal`）。
3. **分桶**：每张发票的**全部未收额只进入一个账龄桶**（不做拆分）：
   - `no_due_date`：未设置到期日；
   - `current`：未到期，**含到期当日**（查询日 ≤ 到期日）；
   - `overdue_1_30` / `overdue_31_60` / `overdue_61_90` / `overdue_91_plus`：
     按查询日与到期日之差（逾期天数）分入 1~30、31~60、61~90、91 天及以上。
4. **按发票币种分别汇总，不跨币种相加**：`currencies` 按币种代码升序，每个币种
   返回 `invoice_count`（纳入发票数）、`total_outstanding`（总未收额）与六个桶
   （固定全部返回，空桶为 `0`）各自的 `invoice_count` 与 `outstanding`。
   **混合保存精度**：每张发票的未收额只按**它自己入库时的** `currency_digits`
   计算；同币种内各发票入库位数可能不同（`CURRENCY_DIGITS` 变更后旧发票保留旧
   位数、新发票按新位数保存），汇总时 Decimal 原样累加，输出才按组内**最高入库
   小数位**格式化——单个账龄桶取落入该桶的参与发票最高位数（非空桶），币种合计
   与回显的 `currency_digits` 取该币种全部参与发票的最高位数，空桶（无参与记录）
   沿用币种位数。例如先存 0 位 USD 发票（未收 `1`）、再存 2 位 USD 发票（未收
   `0.55`），合计显示 `1.55`、`currency_digits` 为 `2`，**不会沿用首条记录的
   0 位精度把 `1.55` 舍成 `2`**；组精度取参与记录的 `max`，与发票插入顺序无关。
   金额一律为按该组最高入库位数取整的十进制字符串（USD `960.00`、JPY `5000`）。
5. 查询为纯读计算，不写库；截止时点之后写入的流水不影响该 `as_of` 的结果，
   同一 `as_of` 重复查询结果恒定。无符合条件的发票时 `currencies` 为空列表。

```bash
curl -s 'http://localhost:8000/api/v1/receivables/aging?as_of=2030-01-15'
```

```json
{
  "as_of": "2030-01-15",
  "cutoff_at": "2030-01-15T23:59:59Z",
  "currencies": [
    {
      "currency": "EUR",
      "currency_digits": 2,
      "invoice_count": 10,
      "total_outstanding": "960.00",
      "buckets": [
        {"bucket": "no_due_date", "invoice_count": 1, "outstanding": "100.00"},
        {"bucket": "current", "invoice_count": 1, "outstanding": "100.00"},
        {"bucket": "overdue_1_30", "invoice_count": 2, "outstanding": "160.00"},
        {"bucket": "overdue_31_60", "invoice_count": 2, "outstanding": "200.00"},
        {"bucket": "overdue_61_90", "invoice_count": 2, "outstanding": "200.00"},
        {"bucket": "overdue_91_plus", "invoice_count": 2, "outstanding": "200.00"}
      ]
    },
    {
      "currency": "KRW",
      "currency_digits": 0,
      "invoice_count": 1,
      "total_outstanding": "5000",
      "buckets": [
        {"bucket": "no_due_date", "invoice_count": 1, "outstanding": "5000"},
        {"bucket": "current", "invoice_count": 0, "outstanding": "0"},
        {"bucket": "overdue_1_30", "invoice_count": 0, "outstanding": "0"},
        {"bucket": "overdue_31_60", "invoice_count": 0, "outstanding": "0"},
        {"bucket": "overdue_61_90", "invoice_count": 0, "outstanding": "0"},
        {"bucket": "overdue_91_plus", "invoice_count": 0, "outstanding": "0"}
      ]
    }
  ]
}
```

每个币种内恒有：各桶 `invoice_count` 之和 == `invoice_count`，各桶
`outstanding` 之和 == `total_outstanding`。

### 3.12 期间税额归集 `GET /api/v1/tax-ledger`

按**纳税期间**（UTC 自然日闭区间）归集开票与贷项抵减的税额台账，常用于增值税
申报取数。

查询参数（均为必填）：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `start` | string | 是 | 期间起始日期，ISO 8601 **日期** `YYYY-MM-DD`；UTC 当日 `00:00:00Z` 起计入（**含当日**）；格式不符或日期不存在（含带时间成分）返回 `422` |
| `end` | string | 是 | 期间截止日期，ISO 8601 **日期** `YYYY-MM-DD`；UTC 当日 `23:59:59Z` 前均计入（**含当日**）；非法返回 `422`，早于 `start`（期间倒置）同样返回 `422` |

统计口径：

1. **选取（均按 UTC 发生时间）**：
   - **发票行**：`invoices.created_at` 落在 `startT00:00:00Z`（含）~
     `endT23:59:59Z`（含）之间的**已保存发票的全部行**；
   - **贷项凭证行**：`credit_memos.created_at` 落在同一区间的凭证行。
     **贷项凭证即使对应更早期开票，也计入凭证创建期间的抵减**（10 月开出的
     贷项凭证抵减 8 月发票，抵减进入 10 月台账）；凭证行税率取自被抵扣的原发票行。
2. **分组**：按**原币种 + 原发票行税率**分组。税率按**数值**规范化——
   `"0.13"` 与 `"0.130"`、`"0.1300"` 数值相同即归入同一组，响应中的 `tax_rate`
   为规范化文本（如 `"0.13"`）；**不同币种绝不混合**（USD 与 JPY 各自成组）。
3. **金额**：只取发票行的**原币列**（`net/tax/gross`），**不使用 `conv_*`
   目标币换算列**；逐行在 Python 侧用 `Decimal` 累计（不走 SQLite 浮点聚合），
   每组分别给出：
   - `invoiced`：本期开票金额；
   - `credited`：本期贷项抵减金额（凭证行存储为正数，直接累加）；
   - `net`：净额 = `invoiced − credited`；
   
   三者各自包含 `net`（未税）、`tax`（税）、`gross`（含税）三列。**混合保存
   精度**：同币种同税率的组可能由不同入库小数位的发票行构成，各行按自身发票的
   入库精度保存并原样累加，输出时按**该组实际参与记录中最高的入库小数位**统一
   取整，并在 `currency_digits` 回显该位数；**贷项凭证行同样参与组精度判定**——
   即使组内发票行都是 0 位，本期存在 2 位精度发票的凭证抵减时，整组（开票/抵减/
   净额）都按 2 位展示（如开票 `10.00`、抵减税 `0.01`），抵减额不会被抹成整数。
   组精度取参与记录的 `max` 而非首条记录的位数，**查询结果与插入顺序无关**。
   JPY 等 0 位币种的纯 0 位组不带小数位。负数冲红发票行按原值计入开票，因此净额
   可能为负。只有本期抵减、本期未开票的分组（凭证对应更早期发票）同样列出。
4. **排序固定**：分组按币种代码升序；同一币种内按税率**数值**升序。
5. 纯读接口：期间内无任何发票行或凭证行时 `groups` 为空列表（`start_date` /
   `end_date` / `period_start` / `period_end` 照常回显）。

```bash
curl -s 'http://localhost:8000/api/v1/tax-ledger?start=2026-10-01&end=2026-10-31'
```

```json
{
  "start_date": "2026-10-01",
  "end_date": "2026-10-31",
  "period_start": "2026-10-01T00:00:00Z",
  "period_end": "2026-10-31T23:59:59Z",
  "groups": [
    {
      "currency": "JPY",
      "currency_digits": 0,
      "tax_rate": "0.1",
      "invoiced": {"net": "1000", "tax": "100", "gross": "1100"},
      "credited": {"net": "0", "tax": "0", "gross": "0"},
      "net": {"net": "1000", "tax": "100", "gross": "1100"}
    },
    {
      "currency": "USD",
      "currency_digits": 2,
      "tax_rate": "0.13",
      "invoiced": {"net": "100.00", "tax": "13.00", "gross": "113.00"},
      "credited": {"net": "20.00", "tax": "2.60", "gross": "22.60"},
      "net": {"net": "80.00", "tax": "10.40", "gross": "90.40"}
    }
  ]
}
```

每组恒有：`net = invoiced − credited`（未税/税/含税三列各自成立）。

### 3.13 错误码

| 状态码 | 场景 |
| --- | --- |
| `404` | 发票 id 不存在（含单张收款登记/查询、历史快照、分期计划创建/查询、贷项凭证创建/查询、来款分配时列表中任一 id 不存在）；发票尚未创建分期计划时查询计划；冲销时收款流水 `payment_id` 不存在；按批次撤销时来款分配批次 `batch_id` 不存在 |
| `409` | 为已存在分期回款计划的发票再次创建计划（每张发票只允许一份） |
| `422` | 参数错误：金额非十进制字符串、净额不满足最小单位、税率越界、跨币种缺汇率、未知币种、收款/来款/冲销金额 ≤ 0、金额小于币种最小单位、按实际抵扣顺序计算出的某张发票实际分配额不是其保存的 `currency_digits` 最小单位整数倍（同币种混合精度下来款无法精确分配）、累计收款超额或列表正未收额合计不足（负额冲红与已结清发票不计入）、累计冲销超过原收款、来款列表为空或 ID 重复、来款与发票币种不一致、`due_date` / `as_of`（逾期查询、分期计划查询、应收账龄汇总）不是合法 ISO 日期、期间税额归集缺少 `start`/`end`、二者不是合法 ISO 日期或 `end` 早于 `start`（期间倒置）、分期期次为空、期次到期日未按升序排列、期次金额 ≤ 0 或非最小单位正整数倍、期次合计不等于含税总额、历史快照的 `at` 缺失或不是合法 UTC 秒精度时间（`YYYY-MM-DDTHH:MM:SSZ`）或早于发票创建时间、贷项凭证明细为空/行号非正或重复、引用不存在或净额非正的原行、本次抵扣净额 ≤ 0 或非最小单位正整数倍、同一原行历次累计抵扣超过原行净额、分页参数越界、发票号码查重批次为空或超过 500 条/供应商编号为空/原始发票号码为空或超过 100 个 Unicode 字符等（响应体 `detail` 为中文说明或 FastAPI 校验错误列表） |

---

## 4. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/invoices.db` | SQLite 文件路径；容器内位于数据卷 `/data`（WAL 旁路文件 `invoices.db-wal` / `-shm` 同目录） |
| `CURRENCY_DIGITS` | 空 | 追加/覆盖币种小数位，逗号分隔，如 `KWD:3,ABC:0`，取值 0~4。只对**新建**发票生效；已保存发票固定其入库时的原币/目标币小数位，配置变更后读取与复算仍按旧精度 |
| `API_PORT`（仅 Compose） | `8000` | 宿主机映射端口，容器内固定 8000 |

容器内服务监听 `0.0.0.0:8000`，SQLite 数据保存在命名卷 `invoice-data` 中，
`docker compose down` 不会删数据；`docker compose down -v` 才会删除。

---

## 5. 本地开发（不用 Docker）

要求 Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/1            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 默认 DB_PATH=/data/invoices.db，本地运行请改到可写目录
export DB_PATH="$(pwd)/data/invoices.db"
mkdir -p data
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 6. 运行测试

```bash
pip install -r requirements.txt

# 计算核心（舍入 / 残差分配 / 跨币种 / 确定性 / 收支累计局部精度），仅依赖标准库
python -m unittest tests.test_calc -v

# 收款余额读取链路分段取数（固定批量低于 SQLite 绑定参数上限且截止时点计入预算/
# 空集合不查询/超大 ID 集合可查询/分段与小批量结果一致/快照冲销明细保持流水 id 升序/
# 分段合并局部精度不丢分位），仅依赖标准库
python -m unittest tests.test_db -v

# API 端到端（自动在空闲端口起真实 uvicorn：健康检查、增查、分页、收款登记/余额、
# 超额 422 不留记录、并发不超收、due_date 持久化、逾期分页/422、
# 超大金额发票（30 位整数净额）收款精度（0.01 不丢分位、大额收款/冲销精确、
# 响应/查询/快照/逾期口径一致、超收超冲 422 不留流水、大额来款分配与整批撤销）、
# 来款分配排序/整笔拒绝/分配与单张收款混合并发不超收、
# 收款冲销分次/超额 422 不留记录/并发冲销不超额/冲销后余额回升再分配、
# 分配批次整批撤销（同事务按净入账额逐笔等额冲销/手工部分冲销只冲净余额/
# 重复请求幂等不新增冲销返回 200 且内容恒定/批次不存在 404/旧无批次收款仍可手工冲销/
# 撤销后余额回升可再次分配/批次撤销与手工冲销并发不超额）、
# 历史收款快照（截止秒闭区间/同秒流水/截止后流水不改变历史/格式与早于发票创建 422/404）、
# 超大 ID 集合端到端（直写 600 张发票及收款/冲销后，账龄汇总与逾期分页经 HTTP 核对金额口径与排序）、
# 分期回款计划（FIFO 逐期抵扣/逾期额按查询日/冲销重新打开/合计与升序校验/每发票唯一 409/JPY/并发创建）、
# 供应商发票号码批次查重（NFKC/去空白/大小写折叠、同供应商判重、组顺序与原始值回显、空值/超长/批次越界 422）、
# 原发票贷项凭证（多行部分抵扣/累计比例摊算税额 HALF_UP/完全抵扣恰好抵完原行税额/
# 重复行号/不存在或非正原行/非最小单位/累计超额 422 不留记录/发票 404/JPY/并发不超额/原发票与收款流水保持原样）、
# 应收账龄汇总（逐桶边界/无到期日单列/含税总额为负与已收满与截止后创建均排除/
# 截止秒闭区间含当秒发票收款冲销/按币种分桶不跨币种/0 位小数币种/
# 同币种混合入库精度按组内最高位数格式化且与插入顺序无关/非法 as_of 422）、
# 期间税额归集（UTC 自然日闭区间含首尾/发票按保存时间与凭证按创建时间分别计入/
# 凭证抵减更早期发票计入本期/税率数值规范化合并/分币种不混合/不使用换算列/
# 混合精度按组内（含贷项凭证行）最高入库位数格式化、开票减抵减恒等于净额且
# 与插入顺序无关/空期间空分组/缺参与非法倒置日期 422）、
# 币种精度按发票固定（CURRENCY_DIGITS 变更后旧发票读取不 422、行/合计/残差
# 分配与 recompute_matched 沿用入库精度/历史 FX 发票从持久化换算金额恢复目标币
# 精度/新建发票按当前配置校验/同币种新旧精度共存）、持久化）
python -m unittest tests.test_api -v
```

---

## 7. 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由：健康检查 / 创建 / 查询 / 分页 / 逾期查询 / 单张收款 / 历史收款快照 / 收款冲销 / 来款分配（批次）/ 按批次整批撤销 / 分期回款计划 / 供应商发票号码批次查重 / 原发票贷项凭证 / 应收账龄汇总 / 期间税额归集
│   ├── calc.py        # Decimal 计算核心：HALF_UP + 最大余数法残差分配（纯函数）
│   ├── currency.py    # ISO 4217 币种最小单位表 + CURRENCY_DIGITS 扩展
│   ├── duplicates.py  # 发票号码查重纯函数：NFKC + 去首尾空白 + casefold 规范化，同供应商内分组判重
│   ├── schemas.py     # Pydantic 入参/出参模型（金额一律字符串，含 due_date / 逾期 / 冲销 / 来款分配与批次标识 / 批次整批撤销 / 历史快照 / 分期计划 / 发票号码查重 / 贷项凭证 / 应收账龄汇总 / 期间税额归集模型）
│   └── db.py          # SQLite 建表（含旧库迁移）/ 事务写入（发票 + 收款 + 冲销 + 来款分配批次与明细 + 批次整批撤销 + 分期计划 + 贷项凭证）/ 查询（含逾期、历史快照、分期 FIFO 抵扣、贷项凭证累计汇总、应收账龄汇总与期间税额归集；收款/冲销按固定批量分段取数，WAL）
├── tests/
│   ├── test_calc.py   # 33 个计算规则与收支精度单测（含入库时固定币种小数位、历史记录目标币精度恢复）
│   ├── test_db.py     # 收款余额读取链路分段取数测试（绑定参数预算/空集合不查询/超大 ID 集合/分段与小批量一致/快照冲销顺序/合并精度）
│   └── test_api.py    # 真实 HTTP 端到端测试（含收款/冲销/来款分配并发不超收不超额、超大金额发票收款/冲销/分配精度、分配批次整批撤销幂等、到期日与逾期查询、历史收款快照、分期回款计划、发票号码批次查重、贷项凭证多行抵扣与税额摊算、应收账龄汇总分桶与截止口径、期间税额归集分组与期间口径、同币种混合入库精度下账龄/税额组按组内最高位数格式化且与插入顺序无关（贷项凭证行参与组精度判定）、CURRENCY_DIGITS 变更后已存发票沿用入库精度、超大 ID 集合下账龄汇总与逾期分页）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

存储模型：`invoices`（发票头 + 汇总金额 + 可选到期日 `due_date` + 原始请求 JSON，
供复算）、`invoice_lines`（逐行最终税额/含税及换算列）、`payments`（收款登记：
金额、币种、登记时间）、`payment_reversals`（收款冲销：金额、币种、登记时间，
外键关联 `payments.id`）、`payment_allocation_batches`（跨发票来款分配批次头：
批次币种、来款金额、创建时间，批次标识即自增 id 并随分配响应返回）与
`payment_allocation_batch_items`（批次明细：批次内本次实际生成的收款关联——批次 +
收款流水 + 发票、本次分配金额、抵扣顺序序号、整批撤销生成的冲销流水 id；
`payment_id` 唯一，一条收款至多属于一个批次）、`installment_plans`（分期计划头，
`invoice_id` 上有唯一约束保证每张发票至多一份）与 `installment_plan_items`
（计划期次：期次号、到期日、金额，外键级联删除）、`credit_memos`（原发票贷项
凭证头：关联发票、币种与本次抵扣净额/税额/含税额合计、登记时间，外键级联删除）
与 `credit_memo_lines`（凭证行：原行号、本次抵扣净额/税额/含税额、抵扣后累计
净额/税额，`UNIQUE(memo_id, line_no)` 兜底同凭证行号不重复）十张表。冲销不改动
原收款流水，余额一律按净入账额（收款 − 冲销）实时计算。来款分配仍沿用 `payments`
表：每张被抵扣的发票向 `payments` 写入一条同构流水（整笔失败则批次头、明细与流水
全部不写），因此单张收款查询与逾期查询天然可见分配结果，分配产生的流水同样可以被
手工冲销或按批次整批撤销。整批撤销只向 `payment_reversals` 追加冲销并在批次明细
回写 `reversal_id`（每条批次明细至多一条），按净入账额逐笔等额冲销、重复请求不
新增；旧的无批次收款不在批次明细中，仍按原接口查询与冲销。分期
计划一经创建**不再改写**：每期覆盖/逾期状态在查询时用净已收额按期次顺序 FIFO
实时抵扣得出，冲销后重新查询即反映重新打开的期次。贷项凭证只追加、从不回写
`invoices` / `invoice_lines` / `payments` / `payment_reversals`：原发票逐行与
汇总金额、未收余额、逾期结果、分期计划视图在开具凭证前后完全一致；逐原行
累计抵扣与剩余净额由 `credit_memo_lines` 在查询时实时汇总。应收账龄汇总不引入新表：
直接复用 `invoices` / `payments` / `payment_reversals` 按查询日 UTC 结束时点实时
计算未收额并分桶——每张发票按自己入库时的 `currency_digits` 计算未收额，同币种
账龄桶/币种合计按参与记录的**最高入库小数位**格式化（不沿用首条记录的位数，
与插入顺序无关），同一 `as_of` 重复查询结果恒定。期间税额归集同样不引入新表：
直接复用 `invoices` / `invoice_lines` / `credit_memos` / `credit_memo_lines`
按 UTC 自然日闭区间实时分组累计，同币种同税率组的格式位数取该组发票行与贷项
凭证行参与记录的最高入库小数位，`invoiced − credited` 恒等于 `net`、结果与
插入顺序无关。升级到含 `due_date`
/ `payment_reversals` / 分配批次表 / 分期计划 / 贷项凭证的版本时，服务启动会自动
对旧库执行 `ALTER TABLE ... ADD COLUMN due_date` 与 `CREATE TABLE IF NOT EXISTS`
（批次两张新表对旧库直接建出），历史发票的到期日为空、不会出现在逾期结果中，
历史收款无冲销记录、净入账额等于原收款金额，历史发票无分期计划、查询返回 404，
历史发票无贷项凭证、查询返回空列表，历史收款不归属任何分配批次、没有 `batch_id`，
仍按原接口查询与逐笔手工冲销（仅无法按批次整批撤销）。升级到币种精度按发票固定
的版本时，服务启动会自动为旧库 `ALTER TABLE ... ADD COLUMN target_digits`：升级
前发票该列为 NULL，其原币精度取已有的 `currency_digits` 列、目标币精度读取时从
已持久化换算金额恢复，配置变更前后的已存行、合计与残差分配保持一致。
