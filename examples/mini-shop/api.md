# 接口文档：迷你“广告领奖 + 渔具店”服务

被测服务按 raw 里的《广告领奖规则》和《渔具店规则》实现，只做了其中一部分（下面写的就是全部）。
这里只写接口长什么样，不写实现。**用例里的具体数值以本文的“测试数据”为准。**

## 地址

- 从环境变量 QA_TARGET_URL 读，默认 http://127.0.0.1:8000（框架生成的 autotest/conftest.py 已经提供 base_url 夹具）
- 浏览器打开 地址/docs 可以看到自动生成的接口页面
- 被测服务用 qa.py autotest run --start 或 qa.py autotest start 启动，写用例时不要修改它

## 通用约定

- 请求和返回都是 JSON
- 所有返回的 HTTP 状态码都是 200，成功与否看 ok 字段：成功 {"ok": true, ...}，失败 {"ok": false, "error": "错误码"}
- 时间都是北京时间。服务器时钟在 /reset 后从 2026-11-26 12:00:00 开始，和真实时间同速流逝；可以用 /debug/clock 往后拨，不能往回拨
- “今天”按服务器时钟的北京时间日期算，0 点换新一天

## 测试数据（每次 /reset 后恢复成这样）

账号：

| 账号 | 金币 | 体力 | 塘级 | 鱼竿档位 |
|---|---|---|---|---|
| A | 1000 | 5 | 4 | 1 |
| B | 1000 | 5 | 4 | 1 |
| C | 1000 | 5 | 4 | 1 |

广告位（只做了两个）：

| placement | 含义 | 每天免费次数 | 每天看广告次数 | 奖励 |
|---|---|---|---|---|
| doubleSettle | 结算双倍 | 1 | 3 | 金币：min(回鱼款, 3 × 场费)，向下取整 |
| stamina | 加体力 | 0 | 6 | 体力 +1（可以超过上限） |

看广告领奖（free 为 false）两次之间至少隔 15 秒；用免费次数（free 为 true）不受这条限制，也不影响这个间隔。

预置场次：

| session_id | 属于 | 已结算 | 回鱼款 | 场费 |
|---|---|---|---|---|
| S1 | A | 是 | 500 | 100 |
| S2 | A | 是 | 120 | 100 |
| S3 | B | 是 | 0 | 100 |
| S4 | A | 否 | 300 | 100 |

渔具店只卖鱼竿：

| 档位 | 价格（金币） | 解锁塘级 |
|---|---|---|
| 1 | 0 | 1 |
| 2 | 500 | 2 |
| 3 | 3000 | 4 |
| 4 | 5500 | 7 |

## 接口

### GET /health
服务是否在线。返回 {"ok": true, "service": "mini-shop"}

### POST /reset
清空所有数据，按“测试数据”重建，服务器时钟回到 2026-11-26 12:00:00。返回 {"ok": true}

### POST /ads/claim
领广告奖励。

请求：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| player | 字符串 | 是 | 账号：A / B / C |
| placement | 字符串 | 是 | doubleSettle 或 stamina |
| operation_id | 字符串 | 是 | 这次领取的唯一凭据，8–64 个字符，按账号区分 |
| free | 布尔 | 否 | true = 用免费次数，false = 看完广告（默认 false） |
| session_id | 字符串 | 结算双倍必填 | 要双倍的场次 |

成功返回：

```json
{"ok": true,
 "grant": {"kind": "gold", "amount": 300},
 "account": { …同 GET /accounts/{id} 里的 account… }}
```

grant.kind 是 gold（金币）或 stamina（体力），amount 是这次发的数量。
同一个账号、同一个 operation_id、同样的参数再次提交：不再发奖、不再扣次数，原样返回第一次成功时的结果。

错误码：

| 错误码 | 什么时候 |
|---|---|
| UNKNOWN_PLAYER | 账号不存在 |
| BAD_OPERATION_ID | operation_id 不是 8–64 个字符的字符串 |
| BAD_REQUEST | 请求不是 JSON 对象，或字段类型不对 |
| UNKNOWN_PLACEMENT | 没有这个广告位 |
| OPERATION_CONFLICT | 同一个 operation_id 之前成功过，但这次参数不同 |
| TOO_FREQUENT | 看广告领奖离上一次看广告领奖不到 15 秒 |
| NO_FREE_LEFT | 今天这个广告位的免费次数用完了 |
| CAP_REACHED | 今天这个广告位的看广告次数用完了 |
| NO_CONTEXT | 结算双倍没传 session_id |
| NO_SESSION | 场次不存在，或不是这个账号的 |
| NOT_SETTLED | 场次还没结算 |
| ALREADY_DOUBLED | 这一场已经双倍过 |
| NOTHING_TO_DOUBLE | 这一场回鱼款是 0 |

失败的请求不发奖、不扣次数，也不占用 operation_id。

### POST /shop/buy
买鱼竿。请求：{"player": "A", "kind": "rod", "tier": 2}（kind 只支持 rod）

成功返回：{"ok": true, "price": 500, "account": {…}}

错误码：UNKNOWN_PLAYER、BAD_REQUEST（kind 不是 rod 等）、BAD_TIER（档位不存在）、GEAR_OWNED（已有这一档或更高）、GEAR_LOCKED（塘级不够）、NO_GOLD（金币不够）

### GET /accounts/{id}
查账号。返回：

```json
{"ok": true, "account": {
  "id": "A", "gold": 1000, "stamina": 5, "pond_level": 4, "rod": 1,
  "date": "2026-11-26",
  "used": {"doubleSettle": {"free": 0, "ad": 0}, "stamina": {"free": 0, "ad": 0}},
  "last_ad_at": null}}
```

used 是今天各广告位已用的免费次数和看广告次数；last_ad_at 是上一次看广告领奖的服务器时间。

### GET /sessions/{id}
查场次。返回 {"ok": true, "session": {"id": "S1", "owner": "A", "settled": true, "gross": 500, "fee": 100, "doubled": false}}

### GET /ledger
查流水，可选参数 ?player=A。返回 {"ok": true, "entries": [...]}，每条：

```json
{"seq": 1, "time": "2026-11-26T12:00:03+08:00", "player": "A",
 "field": "gold", "delta": 300, "reason": "doubleSettle", "operation_id": "op-xxxx"}
```

field 是 gold 或 stamina；reason 是 doubleSettle、stamina 或 buy_rod_档位；买竿的流水 operation_id 为 null。

### GET /debug/clock 和 POST /debug/clock
测试用的“GM 指令”。POST 请求 {"advance_sec": 15} 把服务器时钟往后拨（不能是负数）。两个接口都返回：

```json
{"ok": true, "now": "2026-11-26T12:00:15+08:00", "date": "2026-11-26", "seconds_to_midnight": 43185.0}
```

测 15 秒间隔和跨天时用它拨时钟，不要真的 sleep 等。

## 故障注入

请求头带 X-Fault: drop-response 时，服务端照常处理并提交，然后等 3 秒才返回，用来模拟回包丢失。
客户端把超时设成 1 秒，就会在服务端已经处理完的情况下收不到回包。
