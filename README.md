# YTC MT4 交易系统

YTC 价格行为策略（多周期趋势跟随 + 关键位回调进场）的完整工具链：**MT4 EA 数据桥 + 任意品种历史回测 + 策略规则文档**。

> 适用品种：黄金 XAUUSD / 白银 XAGUSD / 原油 WTI / 比特币 BITCOIN（Alpari MT4 符号）
> 策略出处：Lance Beggs《The YTC Price Action Trader》思想 + Al Brooks 价格行为体系 + Elder 三重滤网框架

---

## 目录结构

```
├── ea/                              # MT4 专家顾问（数据桥 + FVG 自动交易）
│   ├── MCPBridge_Unified.mq4        #   主 EA：行情采集 + 订单桥 + 历史预热 + 实时K线快照 + 合约规格探测
│   └── FVG_Autotrader.mq4           #   FVG 开盘区间突破全自动 EA（US500/NAS100，2%风险，2:1止盈）
├── backtest/                        # 回测与实时监控工具（Python，纯本地零API成本）
│   ├── ytc_backtest.py              #   历史回测：任意品种/区间 → 机会清单 + 模拟执行
│   ├── scan_live.py                 #   单次实时扫描：EA K线快照 + .hst → 压缩版 YTC 信号
│   ├── scan_live_daemon.py          #   常驻监控：每30秒扫4品种，合格信号→弹窗+声音提醒
│   ├── monitor_trade.py             #   持仓移动止损：+1R保本，之后每+1R抬一档，平仓弹窗
│   ├── notify_popup.py              #   Windows 弹窗+Beep 提醒（独立进程，不阻塞监控）
│   ├── watch_signal.py              #   单点盯盘：监控关键位 + M5/M15 反转信号（一次性）
│   ├── fvg_backtest.py              #   FVG 策略回测：开盘区间+FVG三K线，任意品种（--m15 强制M15信号）
│   ├── fvg_watch.py                 #   FVG 实时监控：读 .hst 判区间/FVG，信号弹窗（配合定时任务）
│   ├── gold_diag.py                 #   诊断：方向/关键位/触碰数（解释为何 0 信号）
│   └── check_data.py                #   数据检查：各周期 .hst 覆盖范围
└── README.md
```

> 📚 策略规则、宏观思维雷达、技能文档（知识库）在**私有仓库 ytc-trading-knowledge**，不在此公开仓库。

---

## EA：MCPBridge_Unified

### 架构（文件轮询，无需 DLL）

```
Hermes (MCP) → HTTP bridge (127.0.0.1:8080) → MQL4\Files\*.txt → MT4 内 EA 每秒轮询
```

EA 写 `account_info.txt` / `market_data_*.txt` / `positions.txt`；读 `order_commands.txt` / `close_commands.txt`。

### 功能

| 功能 | 说明 |
|---|---|
| 行情采集 | 14 个品种（10 外汇 + XAUUSD/XAGUSD/WTI/BITCOIN）每 5 秒写 market_data 文件 |
| 订单桥 | 通过 bridge API 下单（市价/挂单 6 种操作）、平仓、查持仓 |
| 回测支持 | 历史数据预热（ChartOpen H1 触发 MT4 下载） |
| 符号探测 | ProbeSymbols() 用 MarketInfo 探测服务器符号存在性，写 symbols_probe.txt |

### 部署（经典布局 Alpari MT4）

1. 编译：`metaeditor.exe /compile:"<path>\MCPBridge_Unified.mq4" /log:"...\MQL4\logs\compile.log"`（GUI 程序，用 Start-Process 触发后轮询 .ex4 生成）
2. 复制 `.ex4` 到 `MQL4\Experts\`
3. MT4 里拖 EA 到任意图表，勾选 Allow live trading
4. 验证：`MQL4\Files\` 出现 account_info.txt 等文件

### 已知坑（实测记录）

- **Alpari 服务器不提供 M1 历史下载**（只给实时）：开 M1 图表 / CopyRates(M1) 都拿不到历史。**用 H1 周期打开图表才触发下载**（H1/D1/M15/M5 等）。EA 的 PrefetchViaCharts() 用 `ChartOpen(sym, PERIOD_H1)` + `CHART_BRING_TO_TOP` 自动触发，`ChartOpen` 是异步的不会卡 EA；`CopyRates` 是阻塞的，在 OnInit/OnTimer 里调用会**卡死整个 EA**（本 build 1478 实测）。
- **EA 的 Print 日志在 `MQL4\Logs\YYYYMMDD.log`**（不是安装目录 `logs\`）。排查 EA 行为先看 MQL4\Logs。
- 周末/休市无 tick：EA 已加 `EventSetTimer(5)` + `OnTimer(){OnTick();}` 兜底。
- 经典布局（Alpari）：MQL4 在安装目录，不是 `%APPDATA%\MetaQuotes\Terminal\<hash>\`。

---

## 回测工具

### 依赖

- Python 3.10+，无第三方库（只用标准库 struct/os/datetime/bisect）
- 数据：Alpari MT4 历史文件 `C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo\<SYMBOL><TF>.hst`

### 用法

```bash
# 主回测：比特币 2026-08-24 ~ 08-30
python ytc_backtest.py BITCOIN 2026-08-24 2026-08-31

# 实时扫描（压缩版 YTC）：读 EA 的 K线快照，找当前信号
python scan_live.py

# 盯盘：监控比特币 77568 支撑的 M5/M15 反转信号（后台）
python watch_signal.py

# 诊断（为什么 0 信号）：D1/H4 方向、关键位触碰数
python gold_diag.py XAUUSD

# 数据检查：各品种各周期覆盖范围
python check_data.py
```

### 输出

```
== YTC backtest: BITCOIN 2026-08-24 ~ 2026-08-31 ==
raw signals: 29, merged opportunities: 4

2026-08-28 22:45 BUY @ 79473 [D1-H] touches=27 sig=PINBAR close=79556
...

== Simulated execution (entry=signal close, SL=level*0.999, TP=+2R) ==
... => SL @ 79394 (08-28 23:30) pnl=-1.0R
TOTAL: 4 trades, net -4.0R
```

### .hst 文件格式（build 600+）

头部 148 字节，每条记录 60 字节：
`struct.unpack('<qdddd', rec[:40])` = time(**8 字节 __int64**) + open/high/low/close（各 8 字节 double）。

> ⚠️ time 是 8 字节 `q` 不是 4 字节 `i`——用 `i` 解析价格全变天文数字（时间戳低 4 字节碰巧正确，极具迷惑性）。

### 回测策略逻辑

1. **D1 方向**：收盘 > MA200 且结构 BULL/BULLISH-BIAS → 多头；< MA200 且 BEAR/BEARISH-BIAS → 空头；否则 CHOP 不交易
2. **H4 共振**：H4 方向与 D1 一致才允许找信号（偏多/偏空算共振）
3. **关键位**：D1/H4 swing 分形（k=2，确认滞后 2 天/8 小时）+ 整数关口；触碰 ≥3 次（D1 位 ≥2 次）有效，±0.2% 容差
4. **进场信号**（M15 优先，无则 H1）：吞没 / pin bar（影线 ≥2×实体）/ inside bar 突破，且价格在关键位区域内
5. **模拟执行**：进场 = 信号K线收盘价，止损 = 关键位 ×0.999（多）/×1.001（空），止盈 = 2R
6. **合并规则**：同方向同关键位（±0.2%）24h 内合并为一次机会

## 实时监控系统（弹窗 + 声音提醒）

纯本地运行，**零 API 成本**（不经过任何大模型），信号检测由本地 Python 完成。

### 架构

```
MT4 (EA) ──每5-15秒──> MQL4\Files\*.txt (价格/K线快照)
                           │
                           ▼
scan_live_daemon.py ──每30秒扫描4品种──> 合格信号?
                           │ 是
                           ▼
notify_popup.py ──> Windows 弹窗 + Beep 声音（独立进程，不阻塞监控）
```

### 启动方式

```bash
# 1. 常驻信号监控（4品种：XAUUSD/XAGUSD/WTI/BITCOIN）
python scan_live_daemon.py

# 2. 持仓移动止损监控（每单一个实例）
python monitor_trade.py <ticket> <开仓价> <初始止损> <1R点数>

# 3. 单次手动扫描
python scan_live.py
```

### 信号纪律（内置在监控逻辑里）

- **只报合格信号**：D1 方向明确（多头/空头）+ 价格在关键位 ±0.2% + M15/M5 反转信号（吞没/Pin Bar/内包突破），三者齐备才提醒
- D1 震荡（CHOP）的品种**静默**——震荡市信号是噪音，不做
- **已有同向持仓时同品种信号自动跳过**（不加仓纪律）
- 同一信号 30 分钟冷却（持久化到 `notify_cache.json`，重启不重复提醒）

### 移动止损纪律（monitor_trade.py）

- 价格 ≥ 开仓 + 1R → 自动调用 `/api/modify` 把止损抬到成本价（保本，零风险）
- 之后每 +1R 抬一档（+2R 锁 1R 利润，+3R 锁 2R……），利润奔跑
- 持仓消失（止损触发/平仓）→ 弹窗 + 4 声 Beep 提醒

### 提醒效果

- 信号：弹窗显示「品种/方向/信号类型/关键位/现价」+ 3 声高音 Beep
- 平仓：弹窗显示「ticket 已平仓」+ 4 声 Beep
- 弹窗置顶（MB_TOPMOST），不会被其他窗口挡住

### 注意事项

- 需要 MT4 运行 + EA 挂载（K线快照依赖 EA 的 WriteKlineSnapshots，每 15 秒刷新）
- 监控是本地脚本，电脑关机即停止；持仓的 SL/TP 在 MT4 服务器端，关机也会执行
- 信号提醒是"辅助"，下单决策请结合策略纪律（知识库 ytc-price-action-system）

### 已知限制

- 回测只覆盖 `.hst` 已有的历史（新品种需先让 MT4 下载：打开该品种 H1 图表）
- 无 M1/M15 文件的品种自动降级用 H1 做信号周期（信号更粗）
- 周末/假日无数据段自然跳过
- 结果基于收盘价模拟，未计点差/滑点（周末比特币点差可达 40+ 美元，实盘需额外考虑）

---

## 策略文档

`strategy/ytc-price-action-system.md` 是完整的可执行策略：

- 周期框架：D1 定方向 → H1 找关键位 → M15 等回调
- 方向判定量化：MA200 + HH/HL 结构（非模糊的主观判断）
- 关键位验证："三次触碰"量化版（±0.2% 容差、大级别位 2 次即可）
- 进场信号：吞没 / pin bar / inside bar 突破
- 资金管理：单笔 1% 风险（1R）、止盈 ≥2R、保本止损、单日 -3% 熔断、连亏 2 单休息
- 禁止清单：震荡市、未验证关键位、破位、数据发布窗口、周末流动性薄
- 每单执行检查清单（10 项全 ✅ 才下单）

---

## 免责声明

本项目用于**模拟交易学习与策略研究**。回测结果不代表未来收益；策略在震荡市会连续小亏（正常现象）；真实交易前请在模拟账户（如 Alpari-Demo）充分验证。投资有风险，决策需谨慎。
