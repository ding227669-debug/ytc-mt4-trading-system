# 缠论+波浪共振交易信号系统 (Chan + Elliott Wave Resonance)

**波浪定大级别方向，缠论找精确买卖点，双向共振才生成信号，冲突则放弃。**

⚠️ **免责声明**：本项目仅供算法研究与回测学习，**禁止接入真实账户自动交易**。缠论/波浪均存在主观划分问题，市场黑天鹅、跳空、流动性可破坏全部规则。实盘交易存在巨大风险。

## 架构

```
.hst历史数据(周/日/M30/M5) → wave_engine(周线数浪) ─┐
                                                    ├→ resonance_engine(共振决策) → chan_wave_daemon(信号扫描+模拟账本)
                             chan_engine(缠论) ─────┘
```

| 模块 | 职责 |
|---|---|
| `wave_engine.py` | 周线自动数浪：摆动点→5浪+ABC标注，铁律（3浪非最短/4浪不进1浪/2浪不破起点），浪号锁定防抖动，输出 BULL/BEAR/UNCERTAIN 方向 |
| `chan_engine.py` | 缠论全流程：包含合并→分型→笔→线段→中枢→背驰(MACD面积)→B1/B2/B3/S1/S2/S3买卖点→防守位 |
| `resonance_engine.py` | 共振决策：前置过滤/开仓全AND条件/三类平仓/黑名单/仓位上限0.6/连续3次证伪冷却 |
| `chan_wave_daemon.py` | 信号扫描守护：读.hst→三引擎→模拟持仓+模拟账本+Windows弹窗提醒。**无任何下单路径**，无持仓时平仓信号一律降级NONE |

## 运行

```bash
python chan_wave_daemon.py          # 单次扫描（配合计划任务每30分钟）
python chan_wave_daemon.py --selftest   # 端到端自测（13项）
python smoke_test.py                # 波浪引擎契约测试
python test_resonance.py            # 共振决策器21分支验证
```

数据依赖：Alpari MT4 `.hst` 历史文件（148字节头+60字节/记录），每周期2048根上限（M30约42天、M5约7天），`.hst` 仅实时更新于可见图表（需MT4图表预热）。

## 状态

第一阶段：信号扫描 + 模拟账本（研究模式）。待模拟账本攒够30个真实信号、验证正期望后，才考虑第二阶段自动下单（需用户明确确认）。

测试：`test_synth.py`（缠论买卖点合成场景）、`test_resonance.py`（决策器21分支）、`chan_wave_daemon.py --selftest`（端到端13项）。
