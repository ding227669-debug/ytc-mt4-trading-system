---
name: ytc-mt4-backtest
description: YTC 价格行为策略回测——读 Alpari MT4 的 .hst 历史文件，对任意品种/任意区间回测"多周期趋势跟随+关键位回调进场"策略，输出机会清单和模拟执行结果（R 数）。触发：回测本周/上周、统计某品种某段时间的 YTC 信号机会。
---

# YTC MT4 回测

对 Alpari MT4（经典布局）本地历史数据跑 YTC 策略回测。

## 数据源

- 目录：`C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo`
- 文件：`<SYMBOL><TF>.hst`（TF: 1/5/15/30/60/240/1440/10080/43200）
- **.hst 格式（build 600+）**：头部 148 字节，每条记录 60 字节。`struct.unpack('<qdddd', rec[:40])` = time(8B __int64) + open/high/low/close(各8B double)。**time 是 8 字节 `q` 不是 4 字节 `i`**——用错全部价格变天文数字。
- 时间戳为服务器时间（UTC），`fromtimestamp` 显示为北京时间（+8h）。

## 已知坑（勿重踩）

1. **Alpari 服务器不提供 M1 历史下载**（只给实时）：开 M1 图表/EA CopyRates(M1) 都拿不到历史。**用 H1 周期打开图表才会下载 H1+D1+M15 等**。EA 里 ChartOpen(sym, PERIOD_H1) 可自动触发（CHART_BRING_TO_TOP 后 MT4 下载），CopyRates 在 OnInit/OnTimer 会阻塞卡死 EA（该 build 实测）。
2. **EA 的 Print 日志在 `MQL4\Logs\YYYYMMDD.log`**（不是安装目录 `logs\`）！排查 EA 行为先看 MQL4\Logs。
3. 新品种首次回测前先确认 .hst 存在（`check_data.py`）；H4 文件缺失时脚本自动从 H1 重采样；H1 缺失时触碰统计自动降级用信号周期（M15）。
4. 历史文件只覆盖"打开过图表"的品种；周末下载的历史在周一开盘后自动补最新K线。
5. 回测合并规则：同方向同关键位（±0.2%）24h 内合并为一次机会。

## 脚本

- `ytc_backtest.py <SYMBOL> <START YYYY-MM-DD> <END YYYY-MM-DD>`：主回测。信号周期自动选 M15（有 M1/M15 文件）否则 H1。
- `gold_diag.py <SYMBOL>`：诊断方向/关键位/触碰，用于解释"为什么 0 信号"。
- `check_data.py`：列出品种各周期数据覆盖。
- 脚本在 `C:\Users\Administrator\Documents\Trading\`，GitHub 仓库 ytc-mt4-trading-system 同步。

## 策略判定逻辑（回测内实现）

1. D1 方向：收盘 > MA200 且结构 BULL/BULLISH-BIAS → 多头；< MA200 且 BEAR/BEARISH-BIAS → 空头；否则 CHOP 不交易
2. H4 共振：H4 方向与 D1 一致（偏多/偏空算共振）才允许找信号
3. 关键位：D1/H4 swing 分形（k=2，确认滞后 2 天/8 小时）+ 整数关口；触碰 ≥3 次（D1 位 ≥2 次）为有效，0.2% 容差
4. 进场信号（M15/H1）：吞没、pin bar（影线 ≥2×实体）、inside bar 突破，且价格在关键位区域内
5. 模拟执行：进场=信号K线收盘，止损=关键位×0.999/1.001，止盈=2R
6. 输出：机会时间/方向/价位/信号/结果（R 数）

## 周报流程

```
python ytc_backtest.py BITCOIN <周一> <下周一>   # 比特币（24/7，含周末）
python ytc_backtest.py XAUUSD <周一> <下周一>     # 黄金（外汇时段）
python ytc_backtest.py XAGUSD <周一> <下周一>     # 白银
python ytc_backtest.py WTI <周一> <下周一>        # 原油（CME 时段）
```
汇总表：信号数、合规机会（周末过滤器：周五 23:00 后~周一 05:00 前的信号按纪律不做，比特币除外需人工判断流动性）、模拟 R 数、锁死原因（H4 不共振 / D1 CHOP / 周末过滤）。
