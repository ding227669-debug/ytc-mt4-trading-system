//+------------------------------------------------------------------+
//| FVG_Autotrader.mq4                                              |
//| FVG 开盘区间突破策略 · 全自动 (模拟盘)                             |
//| 规则来源: 交易之家B站《这套'无聊到炸'的交易系统》(BV1ec2TBZEg6)    |
//|  1. 服务器 16:30-16:45 第一根 M15 K线 = 当日区间                  |
//|  2. M5 出现 FVG 三K线 (中间大实体, 第一/三根影线留缺口,           |
//|     至少一根收盘在区间内+一根在区间外, 不限于首次突破)             |
//|  3. 限价单挂缺口处, 止损 = FVG 第一根K线极值外侧                  |
//|  4. 止盈 = 固定 2:1, 平台挂 TP                                   |
//|  5. 服务器 19:00 未成交撤单收工                                   |
//|  6. 单笔风险 = 账户 RiskPercent% (默认 2%), 每天最多1单           |
//| 用法: 挂到 US500 图表 (任意周期), 开启 Allow Live Trading        |
//+------------------------------------------------------------------+
#property strict
#property copyright "Hermes"

input string  SymbolName      = "US500";    // 交易品种 (挂载图表须一致)
input double  RiskPercent     = 2.0;        // 单笔风险 % (账户)
input int     MaxTradesPerDay = 1;          // 每天最多单数
input int     DeadlineHour    = 19;         // 服务器时间收工 (未成交撤单)
input double  MinBodyPct      = 0.0005;     // 中间K线最小实体 (占价格比例)
input double  SLBuffer        = 0.0001;     // 止损缓冲 (0.01%)
input bool    EnableAlert     = true;       // 信号/下单时 Alert 弹窗
input bool    EnableLog       = true;       // 写 fvg_ea_log.txt

int    g_day          = -1;      // 当前交易日 (日重置用)
bool   g_fvgSignal    = false;   // 本日 FVG 已挂单
int    g_pendingTicket= 0;       // 挂单 ticket
datetime g_rangeTime = 0;
double g_rangeHigh = 0, g_rangeLow = 0;
int    g_filledDay   = 0;        // 本日已成交单数
string g_side        = "";

//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(1);
   Log("FVG_Autotrader initialized on " + SymbolName + ", risk=" + DoubleToString(RiskPercent,1) + "%");
   return(INIT_SUCCEEDED);
}
void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer() { OnTick(); }

//+------------------------------------------------------------------+
void OnTick()
{
   if (StringCompare(Symbol(), SymbolName) != 0) return; // 必须挂在同名图表
   datetime now = TimeCurrent();
   int day = TimeDay(now);
   int dow = TimeDayOfWeek(now);
   int hm  = TimeHour(now) * 60 + TimeMinute(now);

   // ---- 日重置 (每天 00:00) ----
   if (day != g_day)
   {
      g_day = day; g_fvgSignal = false; g_pendingTicket = 0;
      g_rangeTime = 0; g_rangeHigh = 0; g_rangeLow = 0; g_filledDay = 0; g_side = "";
   }

   // ---- 周末不交易 ----
   if (dow == 0 || dow == 6) return;

   // ---- 1. 抓开盘区间 (16:30-16:45 服务器时间) ----
   if (g_rangeTime == 0 && hm >= 16 * 60 + 45)
   {
      string dayStr = TimeToString(now, TIME_DATE);
      datetime t1630 = StrToTime(dayStr + " 16:30:00");
      int shift = iBarShift(SymbolName, PERIOD_M15, t1630, false);
      if (shift >= 0)
      {
         g_rangeHigh = iHigh(SymbolName, PERIOD_M15, shift);
         g_rangeLow  = iLow(SymbolName, PERIOD_M15, shift);
         g_rangeTime = t1630;
         Log("RANGE set: high=" + DoubleToString(g_rangeHigh, 1) +
             " low=" + DoubleToString(g_rangeLow, 1));
         if (EnableAlert) Alert("FVG: 今日区间 High=" + DoubleToString(g_rangeHigh,1) +
                                " Low=" + DoubleToString(g_rangeLow,1));
      }
   }
   if (g_rangeTime == 0) return;

   // ---- 已成交数达标 / 已有挂单 → 不再挂 ----
   if (g_filledDay >= MaxTradesPerDay || g_fvgSignal) { CleanupPending(now, hm); return; }

   // ---- 2. 检测 FVG (M5, 仅 16:45 之后) ----
   MqlRates rt[];
   int got = CopyRates(SymbolName, PERIOD_M5, 0, 30, rt);
   if (got < 3) return;
   double entry = 0, sl = 0, tp = 0;
   int op = -1;
   for (int i = got - 1; i >= 2; i--)
   {
      if (rt[i].time < g_rangeTime + 15 * 60) continue;   // 开盘K线本身不算
      // 只用已收盘K线 (当前未收盘K线跳过)
      if (rt[i].time + 5 * 60 > now) continue;
      if (IsBullFVG(rt[i-2], rt[i-1], rt[i], entry, sl))
      {
         op = OP_BUYLIMIT; g_side = "BUY"; break;
      }
      if (IsBearFVG(rt[i-2], rt[i-1], rt[i], entry, sl))
      {
         op = OP_SELLLIMIT; g_side = "SELL"; break;
      }
   }
   if (op < 0) { CleanupPending(now, hm); return; }

   // ---- 3. 计算手数 + 挂单 ----
   double tickVal  = MarketInfo(SymbolName, MODE_TICKVALUE);
   double tickSize = MarketInfo(SymbolName, MODE_TICKSIZE);
   double perPoint = (tickSize > 0) ? tickVal / tickSize : 1.0; // 每 1.0 价格单位价值
   double minLot   = MarketInfo(SymbolName, MODE_MINLOT);
   double lotStep  = MarketInfo(SymbolName, MODE_LOTSTEP);
   if (lotStep <= 0) lotStep = 0.1;

   double stopDist = MathAbs(entry - sl);
   double riskMoney = AccountBalance() * RiskPercent / 100.0;
   double lots = (stopDist * perPoint > 0) ? riskMoney / (stopDist * perPoint) : minLot;
   lots = MathFloor(lots / lotStep) * lotStep;   // 向下取整到步长
   if (lots < minLot) lots = minLot;

   tp = (op == OP_BUYLIMIT) ? entry + 2 * stopDist : entry - 2 * stopDist;
   string cmt = "FVG auto " + g_side + " " + TimeToString(TimeCurrent(), TIME_DATE|TIME_MINUTES);

   int ticket = OrderSend(SymbolName, op, lots, entry, 3, sl, tp, cmt, 0, 0,
                          (op == OP_BUYLIMIT) ? clrBlue : clrRed);
   if (ticket > 0)
   {
      g_pendingTicket = ticket;
      g_fvgSignal = true;
      Log("PENDING " + g_side + " lots=" + DoubleToString(lots,2) +
          " entry=" + DoubleToString(entry,1) + " sl=" + DoubleToString(sl,1) +
          " tp=" + DoubleToString(tp,1) + " ticket=" + IntegerToString(ticket));
      if (EnableAlert) Alert("FVG 挂单! " + g_side + " lots=" + DoubleToString(lots,2) +
                             " entry=" + DoubleToString(entry,1) + " SL=" + DoubleToString(sl,1) +
                             " TP=" + DoubleToString(tp,1));
   }
   else
   {
      Log("OrderSend FAIL: " + IntegerToString(GetLastError()));
   }
}

//+------------------------------------------------------------------+
//| 多头 FVG: K3 收盘 > 区间高, 缺口 = K1.high ~ K3.low             |
//+------------------------------------------------------------------+
bool IsBullFVG(MqlRates &k1, MqlRates &k2, MqlRates &k3, double &entry, double &sl)
{
   if (k3.close <= g_rangeHigh) return false;
   if (k1.high >= k3.low) return false;                       // 无缺口
   double body2 = MathAbs(k2.close - k2.open);
   if (body2 < MinBodyPct * k3.close) return false;           // 中间K线不够强势
   bool in = (k1.close <= g_rangeHigh && k1.close >= g_rangeLow) ||
             (k2.close <= g_rangeHigh && k2.close >= g_rangeLow) ||
             (k3.close <= g_rangeHigh && k3.close >= g_rangeLow);
   if (!in) return false;                                     // 无区间内收盘
   entry = k1.high;                                           // 缺口下沿
   sl = k1.low * (1 - SLBuffer);
   return true;
}

//+------------------------------------------------------------------+
//| 空头 FVG: K3 收盘 < 区间低, 缺口 = K3.high ~ K1.low             |
//+------------------------------------------------------------------+
bool IsBearFVG(MqlRates &k1, MqlRates &k2, MqlRates &k3, double &entry, double &sl)
{
   if (k3.close >= g_rangeLow) return false;
   if (k3.high >= k1.low) return false;                       // 无缺口
   double body2 = MathAbs(k2.close - k2.open);
   if (body2 < MinBodyPct * k3.close) return false;
   bool in = (k1.close <= g_rangeHigh && k1.close >= g_rangeLow) ||
             (k2.close <= g_rangeHigh && k2.close >= g_rangeLow) ||
             (k3.close <= g_rangeHigh && k3.close >= g_rangeLow);
   if (!in) return false;
   entry = k1.low;                                            // 缺口上沿
   sl = k1.high * (1 + SLBuffer);
   return true;
}

//+------------------------------------------------------------------+
//| 收工: 19:00 后删未成交挂单; 挂单成交后计数                      |
//+------------------------------------------------------------------+
void CleanupPending(datetime now, int hm)
{
   if (g_pendingTicket != 0)
   {
      // 挂单已成交 → 计数, 清 ticket
      if (OrderSelect(g_pendingTicket, SELECT_BY_TICKET))
      {
         if (OrderType() == OP_BUY || OrderType() == OP_SELL)
         {
            g_filledDay++;
            Log("FILLED ticket=" + IntegerToString(g_pendingTicket));
            g_pendingTicket = 0;
         }
      }
      else
      {
         g_pendingTicket = 0;  // 挂单已消失 (成交或删除)
         g_filledDay++;
      }
      // 到点撤单
      if (g_pendingTicket != 0 && hm >= DeadlineHour * 60)
      {
         if (OrderSelect(g_pendingTicket, SELECT_BY_TICKET) &&
             (OrderType() == OP_BUYLIMIT || OrderType() == OP_SELLLIMIT))
         {
            if (OrderDelete(g_pendingTicket))
               Log("DELETED pending at " + TimeToString(now, TIME_MINUTES));
         }
         g_pendingTicket = 0;
      }
   }
}

//+------------------------------------------------------------------+
//| 日志 (追加)                                                     |
//+------------------------------------------------------------------+
void Log(string s)
{
   if (!EnableLog) return;
   int h = FileOpen("fvg_ea_log.txt", FILE_WRITE | FILE_TXT | FILE_READ);
   if (h != INVALID_HANDLE)
   {
      FileSeek(h, 0, SEEK_END);
      FileWrite(h, TimeToString(TimeCurrent(), TIME_DATE|TIME_MINUTES) + " " + s);
      FileClose(h);
   }
}
