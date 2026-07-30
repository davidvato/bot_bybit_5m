"""
check_orders.py
---------------
Consulta el estado actual de posiciones, órdenes y PnL cerrado
directamente en la API de Bybit Testnet.
Ejecutar: python check_orders.py
"""
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

load_dotenv()

session = HTTP(
    testnet=True,
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET"),
)

SYMBOL   = os.getenv("TRADING_SYMBOL", "BTCUSDT")
CATEGORY = os.getenv("MARKET_CATEGORY", "linear")

def ts_to_str(ts_ms):
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts_ms)

sep = "=" * 65

# ── BALANCE ──────────────────────────────────────────────────────
print(sep)
print("  BALANCE CUENTA (UNIFIED)")
print(sep)
bal = session.get_wallet_balance(accountType="UNIFIED")
for coin in bal["result"]["list"][0]["coin"]:
    if coin["coin"] == "USDT":
        wallet   = float(coin.get("walletBalance", 0) or 0)
        avail    = float(coin.get("availableToWithdraw", 0) or 0)
        upnl     = float(coin.get("unrealisedPnl", 0) or 0)
        print(f"  Wallet Balance    : {wallet:,.2f} USDT")
        print(f"  Disponible        : {avail:,.2f} USDT")
        print(f"  Unrealized PnL    : {upnl:+,.2f} USDT")

# ── POSICIONES ABIERTAS ───────────────────────────────────────────
print()
print(sep)
print(f"  POSICIONES ABIERTAS — {SYMBOL}")
print(sep)
pos_resp = session.get_positions(category=CATEGORY, symbol=SYMBOL)
positions = pos_resp["result"]["list"]
open_pos  = [p for p in positions if float(p.get("size", "0")) > 0]
if not open_pos:
    print("  Sin posiciones abiertas.")
else:
    for p in open_pos:
        upnl = float(p.get("unrealisedPnl", 0))
        print(f"  Side              : {p['side']}")
        print(f"  Qty               : {p['size']} BTC")
        print(f"  Precio Entrada    : {float(p['avgPrice']):,.2f} USDT")
        print(f"  Precio Marca      : {float(p['markPrice']):,.2f} USDT")
        print(f"  Liq. Price        : {float(p.get('liqPrice','0') or 0):,.2f} USDT")
        print(f"  SL                : {p.get('stopLoss', 'N/A')}")
        print(f"  TP                : {p.get('takeProfit', 'N/A')}")
        print(f"  Unrealized PnL    : {upnl:+,.2f} USDT")
        print(f"  Leverage          : {p.get('leverage', '?')}x")

# ── ÓRDENES ACTIVAS ───────────────────────────────────────────────
print()
print(sep)
print(f"  ORDENES ACTIVAS — {SYMBOL}")
print(sep)
open_orders = session.get_open_orders(category=CATEGORY, symbol=SYMBOL)
orders = open_orders["result"]["list"]
if not orders:
    print("  Sin ordenes activas.")
else:
    for o in orders:
        print(f"  {ts_to_str(o['createdTime'])} | {o['side']:5s} | Qty={o['qty']} | Price={o['price']} | Status={o['orderStatus']} | Type={o['orderType']}")

# ── HISTORIAL DE ÓRDENES (últimas 20) ────────────────────────────
print()
print(sep)
print(f"  HISTORIAL DE ORDENES (ultimas 20) — {SYMBOL}")
print(sep)
hist = session.get_order_history(category=CATEGORY, symbol=SYMBOL, limit=20)
hist_orders = hist["result"]["list"]
if not hist_orders:
    print("  Sin historial.")
else:
    for o in hist_orders:
        created = ts_to_str(o["createdTime"])
        avg     = o.get("avgPrice", "0") or "0"
        print(f"  {created} | {o['side']:5s} | Qty={o['qty']:>8s} | "
              f"AvgPrice={float(avg):>10,.2f} | Status={o['orderStatus']:15s} | "
              f"Type={o['orderType']}")

# ── TRADES EJECUTADOS (últimos 20) ───────────────────────────────
print()
print(sep)
print(f"  TRADES EJECUTADOS (ultimos 20) — {SYMBOL}")
print(sep)
try:
    execs = session.get_executions(category=CATEGORY, symbol=SYMBOL, limit=20)
    exec_list = execs["result"]["list"]
    if not exec_list:
        print("  Sin ejecuciones registradas.")
    else:
        for t in exec_list:
            ts    = ts_to_str(t["execTime"])
            price = float(t["execPrice"])
            qty   = t["execQty"]
            fee   = float(t["execFee"])
            etype = t["execType"]
            side  = t["side"]
            print(f"  {ts} | {side:5s} | Qty={qty:>8s} | Price={price:>10,.2f} | Fee={fee:.4f} | Type={etype}")
except Exception as e:
    print(f"  Error: {e}")

# ── PnL CERRADO (últimos 10) ──────────────────────────────────────
print()
print(sep)
print(f"  PnL CERRADO (ultimos 10) — {SYMBOL}")
print(sep)
try:
    pnl_resp = session.get_closed_pnl(category=CATEGORY, symbol=SYMBOL, limit=10)
    pnl_list = pnl_resp["result"]["list"]
    if not pnl_list:
        print("  Sin PnL cerrado registrado.")
    else:
        total_pnl = 0.0
        for e in pnl_list:
            ts       = ts_to_str(e.get("createdTime", "0"))
            side     = e.get("side", "?")
            qty      = e.get("qty", "?")
            entry    = float(e.get("avgEntryPrice", 0))
            exit_p   = float(e.get("avgExitPrice", 0))
            pnl      = float(e.get("closedPnl", 0))
            total_pnl += pnl
            emoji = "✅" if pnl >= 0 else "❌"
            print(f"  {emoji} {ts} | {side:5s} | Qty={qty:>8s} | "
                  f"Entry={entry:>10,.2f} | Exit={exit_p:>10,.2f} | PnL={pnl:>+,.4f} USDT")
        print(f"  {'─'*55}")
        print(f"  PnL TOTAL ACUMULADO: {total_pnl:+,.4f} USDT")
except Exception as e:
    print(f"  Error: {e}")

print()
print(sep)
print("  FIN DEL REPORTE")
print(sep)
