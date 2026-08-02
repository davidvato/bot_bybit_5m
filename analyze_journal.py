import csv
import os
from collections import defaultdict
from config import BotConfig
from logger import setup_logger

def analyze_journal(csv_path="logs/trade_journal.csv"):
    if not os.path.exists(csv_path):
        print(f"Error: No se encontró el archivo {csv_path}")
        return

    print("=" * 60)
    print("  📊 ANÁLISIS DE TRADE JOURNAL")
    print("=" * 60)

    trades = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    except Exception as e:
        print(f"Error leyendo {csv_path}: {e}")
        return

    if not trades:
        print("El trade journal está vacío.")
        return

    total_trades = len(trades)
    closed_trades = [t for t in trades if t.get("status") in ("WIN", "LOSS")]
    
    print(f"Total trades registrados: {total_trades}")
    print(f"Trades cerrados (con PnL): {len(closed_trades)}\n")

    if not closed_trades:
        print("No hay suficientes datos cerrados para calcular métricas de rendimiento.")
        return

    # --- Métricas Generales ---
    wins = [t for t in closed_trades if t.get("status") == "WIN"]
    losses = [t for t in closed_trades if t.get("status") == "LOSS"]
    
    win_rate = (len(wins) / len(closed_trades)) * 100
    
    total_profit = sum(float(t.get("pnl_usdt", 0)) for t in wins)
    total_loss_amount = sum(abs(float(t.get("pnl_usdt", 0))) for t in losses)
    
    profit_factor = (total_profit / total_loss_amount) if total_loss_amount > 0 else float('inf')
    net_pnl = total_profit - total_loss_amount

    print("--- RENDIMIENTO GLOBAL ---")
    print(f"Win Rate:      {win_rate:.1f}% ({len(wins)}W - {len(losses)}L)")
    print(f"Net PnL:       {net_pnl:+.2f} USDT")
    print(f"Profit Factor: {profit_factor:.2f}")
    
    if wins:
        avg_win = total_profit / len(wins)
        print(f"Prom. Ganancia:+{avg_win:.2f} USDT")
    if losses:
        avg_loss = total_loss_amount / len(losses)
        print(f"Prom. Pérdida: -{avg_loss:.2f} USDT")

    # --- Análisis por Dirección (LONG vs SHORT) ---
    longs = [t for t in closed_trades if t.get("side").upper() == "BUY"]
    shorts = [t for t in closed_trades if t.get("side").upper() == "SELL"]
    
    print("\n--- RENDIMIENTO POR DIRECCIÓN ---")
    for name, subset in [("LONG (Buy)", longs), ("SHORT (Sell)", shorts)]:
        if not subset:
            print(f"{name}: 0 trades")
            continue
        sub_wins = [t for t in subset if t.get("status") == "WIN"]
        sub_wr = (len(sub_wins) / len(subset)) * 100
        sub_pnl = sum(float(t.get("pnl_usdt", 0)) for t in subset)
        print(f"{name:12}: {len(subset):2} trades | WR: {sub_wr:5.1f}% | PnL: {sub_pnl:+.2f} USDT")

    # --- Análisis por Nivel de Consenso (Votos) ---
    print("\n--- RENDIMIENTO POR NIVEL DE CONSENSO ---")
    consensus_groups = defaultdict(list)
    for t in closed_trades:
        # Sumar long_votes y short_votes para obtener el total de votos a favor de la dirección tomada
        votes = max(int(t.get("long_votes", 0)), int(t.get("short_votes", 0)))
        consensus_groups[votes].append(t)
        
    for votes in sorted(consensus_groups.keys()):
        subset = consensus_groups[votes]
        sub_wins = [t for t in subset if t.get("status") == "WIN"]
        sub_wr = (len(sub_wins) / len(subset)) * 100
        sub_pnl = sum(float(t.get("pnl_usdt", 0)) for t in subset)
        print(f"Consenso {votes}/4: {len(subset):2} trades | WR: {sub_wr:5.1f}% | PnL: {sub_pnl:+.2f} USDT")

    # --- Análisis de Slippage ---
    slippage_values = []
    for t in closed_trades:
        slip = t.get("slippage_usdt")
        if slip and slip.strip():
            try:
                slippage_values.append(float(slip))
            except ValueError:
                pass
                
    if slippage_values:
        avg_slippage = sum(slippage_values) / len(slippage_values)
        print("\n--- SLIPPAGE ---")
        print(f"Slippage promedio: {avg_slippage:+.2f} USDT (negativo = peor al esperado)")
        
    print("=" * 60)

if __name__ == "__main__":
    analyze_journal()
