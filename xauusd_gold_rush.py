"""
╔══════════════════════════════════════════════════════════════╗
║           XAUUSD GOLD RUSH BOT v1.0                         ║
║    7 trades tegelijk · Snelle close op winst                 ║
║    SL voor veiligheid · Lot groeit met balans                ║
║    MetaAPI Cloud SDK · Railway Deploy                        ║
╚══════════════════════════════════════════════════════════════╝

Strategie:
- Houd ALTIJD 7 trades open op XAUUSD
- Direction: EMA 20 trend (uptrend = buys, downtrend = sells)
- TP: sluit elke trade direct bij winst ≥ $0.50
- SL: -$3 hard SL per trade (veiligheidsnet)
- Lot scaling: 0.01 op $5K → 0.02 op $10K → 0.05 op $25K
- Max lot: 0.10 (cap)
- Max daily loss: 3% van balans
- 10-seconde cycle voor snelle close
"""

import os, sys, asyncio, logging, time, signal as sig_module
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional, List, Dict
from enum import Enum

try:
    from metaapi_cloud_sdk import MetaApi
except ImportError:
    print("pip install metaapi-cloud-sdk"); sys.exit(1)
try:
    import aiohttp
except ImportError:
    print("pip install aiohttp"); sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RushConfig:
    META_API_TOKEN: str = os.getenv("METAAPI_TOKEN", "")
    ACCOUNT_ID: str = os.getenv("ACCOUNT_ID", "")
    TELEGRAM_TOKEN: str = os.getenv("TG_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TG_CHAT", "")

    SYMBOL: str = "XAUUSD"

    # Rush settings
    NUM_TRADES: int = 7              # 7 trades altijd open
    BASE_LOT: float = 0.01           # start lot
    LOT_SCALE_PER: float = 5000.0    # +0.01 lot per $5000 balance
    MAX_LOT: float = 0.10            # cap

    # Profit / Loss
    TP_PROFIT: float = 0.50          # sluit bij +$0.50 (per trade)
    SL_LOSS: float = 3.0             # -$3 hard SL (per trade)

    # Timing
    CYCLE_SECONDS: int = 10          # check elke 10 sec
    OPEN_DELAY_SECONDS: float = 0.5  # delay tussen 7 opens
    HEARTBEAT_INTERVAL: int = 600    # heartbeat elke 10 min

    # Safety
    MAX_DAILY_LOSS_PERCENT: float = 3.0
    MAX_DRAWDOWN_PERCENT: float = 10.0
    MIN_BALANCE: float = 100.0       # stop als balans hier komt

    # Trend filter
    EMA_PERIOD: int = 20
    USE_TREND_FILTER: bool = True    # alleen mee met trend

    # Sessions (UTC) — alleen actief in volatiele uren
    LONDON_START: int = 7
    NY_END: int = 17
    USE_SESSION_FILTER: bool = True


# ═══════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════

def setup_logging():
    log = logging.getLogger("rush")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%H:%M:%S")
    h = logging.StreamHandler(); h.setFormatter(fmt); log.addHandler(h)
    fh = logging.FileHandler("gold_rush.log", encoding="utf-8")
    fh.setFormatter(fmt); log.addHandler(fh)
    return log

log = setup_logging()


# ═══════════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════════

class Telegram:
    def __init__(self, cfg: RushConfig):
        self.cfg = cfg
        self.last_send = 0.0
        self.last_heartbeat = 0.0

    async def send(self, text: str, silent: bool = False):
        if not self.cfg.TELEGRAM_TOKEN: return
        now = time.time()
        if now - self.last_send < 1.5:
            await asyncio.sleep(1.5)
        try:
            url = f"https://api.telegram.org/bot{self.cfg.TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": self.cfg.TELEGRAM_CHAT_ID,
                "text": text, "parse_mode": "HTML",
                "disable_notification": silent
            }
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10))
            self.last_send = time.time()
        except Exception as e:
            log.warning(f"TG error: {e}")


# ═══════════════════════════════════════════════════════════════════
#  RUSH BOT
# ═══════════════════════════════════════════════════════════════════

class GoldRushBot:
    def __init__(self):
        self.cfg = RushConfig()
        self.tg = Telegram(self.cfg)
        self.api = None
        self.account = None
        self.conn = None
        self.balance = 0.0
        self.start_balance = 0.0
        self.peak_balance = 0.0
        self.trade_date = ""
        self.daily_pnl = 0.0
        self.daily_wins = 0
        self.daily_losses = 0
        self.total_trades = 0
        self.total_wins = 0
        self.total_losses = 0
        self.running = True
        self.heartbeat = time.time()
        self.candles = []

    # ─── EMA ──────────────────────────────────────────────────
    def calc_ema(self, candles, period):
        if len(candles) < period: return 0.0
        closes = [c.get("close", 0) for c in candles]
        mult = 2 / (period + 1)
        val = sum(closes[:period]) / period
        for c in closes[period:]:
            val = (c - val) * mult + val
        return val

    # ─── Determine direction ──────────────────────────────────
    def get_direction(self, price):
        if not self.cfg.USE_TREND_FILTER:
            return "buy"
        if not self.candles or len(self.candles) < self.cfg.EMA_PERIOD:
            return "buy"
        ema = self.calc_ema(self.candles, self.cfg.EMA_PERIOD)
        if ema <= 0: return "buy"
        return "buy" if price >= ema else "sell"

    # ─── Calculate lot size based on balance ─────────────────
    def calc_lot_size(self):
        if self.balance <= 0: return self.cfg.BASE_LOT
        scale = self.balance / self.cfg.LOT_SCALE_PER
        lot = round(self.cfg.BASE_LOT * scale, 2)
        lot = max(self.cfg.BASE_LOT, min(lot, self.cfg.MAX_LOT))
        return lot

    # ─── Check session ───────────────────────────────────────
    def is_active_session(self):
        if not self.cfg.USE_SESSION_FILTER: return True
        h = datetime.now(timezone.utc).hour
        return self.cfg.LONDON_START <= h < self.cfg.NY_END

    # ─── Connect ─────────────────────────────────────────────
    async def connect(self):
        log.info("Connecting to MetaAPI...")
        if not self.cfg.META_API_TOKEN or not self.cfg.ACCOUNT_ID:
            log.error("Set METAAPI_TOKEN and ACCOUNT_ID!"); sys.exit(1)

        self.api = MetaApi(self.cfg.META_API_TOKEN)
        self.account = await self.api.metatrader_account_api.get_account(self.cfg.ACCOUNT_ID)

        if self.account.state != "DEPLOYED":
            await self.account.deploy()
        await self.account.wait_connected()

        self.conn = self.account.get_rpc_connection()
        await self.conn.connect()
        await self.conn.wait_synchronized()

        info = await self.conn.get_account_information()
        self.start_balance = info.get("balance", 0)
        self.balance = self.start_balance
        self.peak_balance = self.start_balance
        self.trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        log.info(f"Connected! Balance: ${self.balance:.2f}")
        await self.tg.send(
            f"🚀 <b>Gold Rush Bot v1.0</b>\n"
            f"Balance: ${self.balance:.2f}\n"
            f"Trades: {self.cfg.NUM_TRADES} tegelijk\n"
            f"Lot: {self.calc_lot_size()} | TP: +${self.cfg.TP_PROFIT} | SL: -${self.cfg.SL_LOSS}\n"
            f"Max DD: {self.cfg.MAX_DRAWDOWN_PERCENT}%"
        )

    # ─── Open one trade ──────────────────────────────────────
    async def open_one(self, direction, lot, price):
        try:
            if direction == "buy":
                sl_price = price - (self.cfg.SL_LOSS / (lot * 100))
                tp_price = price + (self.cfg.TP_PROFIT * 5 / (lot * 100))  # wide TP, we close manually
                order = await asyncio.wait_for(
                    self.conn.create_market_buy_order(
                        self.cfg.SYMBOL, lot, sl_price, tp_price,
                        {"comment": "rush"}
                    ), timeout=10
                )
            else:
                sl_price = price + (self.cfg.SL_LOSS / (lot * 100))
                tp_price = price - (self.cfg.TP_PROFIT * 5 / (lot * 100))
                order = await asyncio.wait_for(
                    self.conn.create_market_sell_order(
                        self.cfg.SYMBOL, lot, sl_price, tp_price,
                        {"comment": "rush"}
                    ), timeout=10
                )
            return order.get("positionId") or order.get("orderId")
        except Exception as e:
            log.warning(f"Open failed: {e}")
            return None

    # ─── Top up trades to NUM_TRADES ─────────────────────────
    async def maintain_trades(self, price):
        try:
            positions = await asyncio.wait_for(
                self.conn.get_positions(), timeout=10
            )
            rush_positions = [p for p in positions if p.get("symbol") == self.cfg.SYMBOL]
            current_count = len(rush_positions)
            need = self.cfg.NUM_TRADES - current_count

            if need <= 0: return

            direction = self.get_direction(price)
            lot = self.calc_lot_size()

            opened = 0
            for _ in range(need):
                tid = await self.open_one(direction, lot, price)
                if tid:
                    opened += 1
                    self.total_trades += 1
                await asyncio.sleep(self.cfg.OPEN_DELAY_SECONDS)

            if opened > 0:
                log.info(f"📈 Opened {opened} {direction.upper()} @ ${price:.2f} ({lot} lot)")
                await self.tg.send(
                    f"📈 <b>{opened}× {direction.upper()}</b> @ ${price:.2f}\n"
                    f"Lot: {lot} | Open: {current_count + opened}/{self.cfg.NUM_TRADES}",
                    silent=True
                )
        except Exception as e:
            log.error(f"Maintain error: {e}")

    # ─── Close any profitable trades ─────────────────────────
    async def close_profitable(self):
        try:
            positions = await asyncio.wait_for(
                self.conn.get_positions(), timeout=10
            )
            rush_positions = [p for p in positions if p.get("symbol") == self.cfg.SYMBOL]

            closed = 0
            for p in rush_positions:
                profit = p.get("profit", 0) + p.get("swap", 0)
                pid = p.get("id")
                if profit >= self.cfg.TP_PROFIT and pid:
                    try:
                        await asyncio.wait_for(
                            self.conn.close_position(pid), timeout=10
                        )
                        closed += 1
                        self.total_wins += 1
                        self.daily_wins += 1
                        self.daily_pnl += profit
                        log.info(f"✅ Closed +${profit:.2f}")
                    except Exception as e:
                        log.warning(f"Close failed for {pid}: {e}")

            if closed > 0:
                await self.tg.send(
                    f"✅ <b>{closed}× CLOSED</b>\n"
                    f"Daily: ${self.daily_pnl:+.2f} | "
                    f"Total: {self.total_wins}W/{self.total_losses}L",
                    silent=True
                )
        except Exception as e:
            log.error(f"Close error: {e}")

    # ─── Detect SL hits ──────────────────────────────────────
    async def check_sl_hits(self):
        """Track positions that closed at SL."""
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            start = now - timedelta(minutes=2)
            history = await asyncio.wait_for(
                self.conn.get_deals_by_time_range(start, now), timeout=10
            )
            if not history: return

            for deal in history:
                if deal.get("symbol") != self.cfg.SYMBOL: continue
                profit = deal.get("profit", 0)
                if profit < -self.cfg.SL_LOSS * 0.8:  # SL hit
                    self.total_losses += 1
                    self.daily_losses += 1
                    self.daily_pnl += profit
        except Exception:
            pass

    # ─── Update balance ──────────────────────────────────────
    async def update_balance(self):
        try:
            info = await asyncio.wait_for(
                self.conn.get_account_information(), timeout=10
            )
            self.balance = info.get("balance", self.balance)
            if self.balance > self.peak_balance:
                self.peak_balance = self.balance
        except Exception:
            pass

    # ─── Fetch candles ───────────────────────────────────────
    async def fetch_candles(self):
        try:
            candles = await asyncio.wait_for(
                self.account.get_historical_candles(
                    self.cfg.SYMBOL, "1m",
                    datetime.now(timezone.utc), 30
                ), timeout=10
            )
            if candles: self.candles = candles
        except Exception:
            pass

    # ─── Daily reset ─────────────────────────────────────────
    async def daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.trade_date:
            wr = self.daily_wins / max(self.daily_wins + self.daily_losses, 1) * 100
            await self.tg.send(
                f"📊 <b>DAILY REPORT</b>\n"
                f"Wins: {self.daily_wins} | Losses: {self.daily_losses}\n"
                f"WR: {wr:.0f}% | PnL: ${self.daily_pnl:+.2f}\n"
                f"Balance: ${self.balance:.2f}"
            )
            self.trade_date = today
            self.daily_pnl = 0.0
            self.daily_wins = 0
            self.daily_losses = 0

    # ─── Heartbeat ───────────────────────────────────────────
    async def send_heartbeat(self, price, open_count):
        now = time.time()
        if now - self.tg.last_heartbeat < self.cfg.HEARTBEAT_INTERVAL:
            return
        self.tg.last_heartbeat = now
        wr = self.total_wins / max(self.total_wins + self.total_losses, 1) * 100
        direction = self.get_direction(price)
        await self.tg.send(
            f"💓 <b>RUSH BOT</b>\n"
            f"${price:.2f} | Trend: {direction.upper()}\n"
            f"Open: {open_count}/{self.cfg.NUM_TRADES}\n"
            f"Lot: {self.calc_lot_size()}\n"
            f"Today: {self.daily_wins}W/{self.daily_losses}L (${self.daily_pnl:+.2f})\n"
            f"Total: {self.total_wins}W/{self.total_losses}L ({wr:.0f}% WR)\n"
            f"Balance: ${self.balance:.2f}",
            silent=True
        )

    # ─── Safety checks ───────────────────────────────────────
    def safety_ok(self):
        if self.balance < self.cfg.MIN_BALANCE:
            log.error("Balance too low — stopping")
            return False
        if self.start_balance > 0:
            dd = (self.peak_balance - self.balance) / self.peak_balance * 100
            if dd >= self.cfg.MAX_DRAWDOWN_PERCENT:
                log.error(f"Max drawdown {dd:.1f}% — stopping")
                return False
            daily_loss_max = self.start_balance * (self.cfg.MAX_DAILY_LOSS_PERCENT / 100)
            if self.daily_pnl <= -daily_loss_max:
                log.warning(f"Daily loss limit reached")
                return False
        return True

    # ─── Reconnect ───────────────────────────────────────────
    async def reconnect(self):
        log.warning("Reconnecting...")
        await self.tg.send("⚠️ <b>RECONNECTING...</b>")
        for attempt in range(3):
            try:
                try: await self.conn.close()
                except: pass
                await asyncio.sleep(5 * (attempt + 1))
                self.conn = self.account.get_rpc_connection()
                await self.conn.connect()
                await asyncio.wait_for(self.conn.wait_synchronized(), timeout=30)
                test = await asyncio.wait_for(self.conn.get_account_information(), timeout=10)
                if test:
                    log.info("Reconnected!")
                    await self.tg.send("✅ <b>RECONNECTED</b>")
                    return True
            except Exception as e:
                log.warning(f"Reconnect {attempt+1}/3 failed: {e}")
        try:
            await self.account.undeploy()
            await asyncio.sleep(15)
            await self.account.deploy()
            await asyncio.sleep(30)
            self.conn = self.account.get_rpc_connection()
            await self.conn.connect()
            await asyncio.wait_for(self.conn.wait_synchronized(), timeout=60)
            log.info("Hard reconnect success!")
            await self.tg.send("✅ <b>RECONNECTED via REDEPLOY</b>")
            return True
        except Exception as e:
            log.error(f"Hard reconnect failed: {e}")
            return False

    # ─── Main cycle ──────────────────────────────────────────
    async def cycle(self):
        self.heartbeat = time.time()
        await self.daily_reset()
        await self.update_balance()
        await self.fetch_candles()

        try:
            tick = await asyncio.wait_for(
                self.conn.get_symbol_price(self.cfg.SYMBOL), timeout=10
            )
            bid = tick.get("bid", 0)
            ask = tick.get("ask", 0)
            price = (bid + ask) / 2
        except Exception:
            return

        if price <= 0: return

        # Check SL hits
        await self.check_sl_hits()

        # Close any profitable
        await self.close_profitable()

        # Get current open count
        try:
            positions = await self.conn.get_positions()
            open_count = len([p for p in positions if p.get("symbol") == self.cfg.SYMBOL])
        except Exception:
            open_count = 0

        # Heartbeat
        await self.send_heartbeat(price, open_count)

        # Safety
        if not self.safety_ok(): return

        # Session filter
        if not self.is_active_session(): return

        # Maintain 7 trades
        await self.maintain_trades(price)

    # ─── Main loop ───────────────────────────────────────────
    async def run(self):
        log.info(f"Rush loop started (cycle: {self.cfg.CYCLE_SECONDS}s)")
        consecutive_errors = 0
        while self.running:
            try:
                await self.cycle()
                consecutive_errors = 0
            except asyncio.CancelledError:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    if await self.reconnect(): consecutive_errors = 0
                else:
                    await asyncio.sleep(10)
            except Exception as e:
                consecutive_errors += 1
                err = str(e).lower()
                if any(x in err for x in ["not connected", "socket", "timed out"]):
                    if consecutive_errors >= 3:
                        if await self.reconnect(): consecutive_errors = 0
                    else:
                        await asyncio.sleep(15)
                else:
                    log.error(f"Cycle error: {e}")
                    if consecutive_errors >= 10:
                        await self.reconnect()
                        consecutive_errors = 0
            await asyncio.sleep(self.cfg.CYCLE_SECONDS)

    # ─── Start ───────────────────────────────────────────────
    async def start(self):
        try:
            await self.connect()
            async def watchdog():
                while self.running:
                    await asyncio.sleep(60)
                    if time.time() - self.heartbeat > 600:
                        log.critical("Watchdog timeout!")
                        os._exit(1)
            asyncio.create_task(watchdog())
            await self.run()
        except KeyboardInterrupt:
            log.info("Shutting down...")
        except Exception as e:
            log.error(f"Fatal: {e}", exc_info=True)
            await self.tg.send(f"❌ <b>FATAL:</b> {str(e)[:200]}")
        finally:
            self.running = False


def main():
    def handler(s, f): sys.exit(0)
    sig_module.signal(sig_module.SIGINT, handler)
    sig_module.signal(sig_module.SIGTERM, handler)
    bot = GoldRushBot()
    asyncio.run(bot.start())


if __name__ == "__main__":
    main()
