"""
=============================================================================
trade_journal.py - Registro Persistente de Operaciones (Trade Journal)
=============================================================================
Registra cada operacion ejecutada en un archivo CSV separado del log principal.
Proporciona metricas de sesion: Win Rate, Profit Factor, Max Drawdown, PnL.

COLUMNAS DEL CSV:
  timestamp       - Fecha y hora UTC de la entrada
  symbol          - Par de trading
  side            - Buy (Long) o Sell (Short)
  entry_price     - Precio de entrada estimado
  stop_loss       - Precio del Stop Loss
  take_profit     - Precio del Take Profit
  qty             - Cantidad de contratos
  risk_usdt       - Monto en riesgo (perdida maxima estimada)
  tp_usdt         - Ganancia potencial maxima estimada
  confidence_avg  - Confianza promedio de las estrategias que votaron
  long_votes      - Cuantas estrategias votaron LONG
  short_votes     - Cuantas estrategias votaron SHORT
  strategies      - Resumen de las estrategias activas
  status          - 'OPEN', 'WIN', 'LOSS', 'UNKNOWN' (actualizable)
  pnl_usdt        - PnL realizado (actualizable, 0.0 si aun abierto)
=============================================================================
"""

import csv
import os
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict

from config import BotConfig
from logger import setup_logger
from strategies import ConsensusResult


class TradeJournal:
    """
    Registra cada operacion ejecutada en un archivo CSV para analisis post-sesion.
    Permite calcular metricas de rendimiento: Win Rate, Profit Factor, Max Drawdown.
    """

    FIELDNAMES = [
        "timestamp", "symbol", "side", "entry_price",
        "stop_loss", "take_profit", "qty", "risk_usdt", "tp_usdt",
        "confidence_avg", "long_votes", "short_votes", "strategies",
        "status", "pnl_usdt",
    ]

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.logger = setup_logger(__name__, config.log_file, config.get_log_level())

        log_dir = os.path.dirname(config.log_file) or "logs"
        os.makedirs(log_dir, exist_ok=True)
        self.csv_path = os.path.join(log_dir, "trade_journal.csv")
        self._init_csv()
        self._session_trades: List[Dict] = []

    def _init_csv(self) -> None:
        """Crea el archivo CSV con encabezados si no existe todavia."""
        if not os.path.exists(self.csv_path):
            try:
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
                    writer.writeheader()
                self.logger.info(f"Trade Journal creado: {self.csv_path}")
            except Exception as e:
                self.logger.error(f"Error creando Trade Journal: {e}")

    def log_trade(
        self,
        consensus: ConsensusResult,
        side: str,
        qty: float,
        stop_loss: float,
        take_profit: float,
        risk_usdt: float,
    ) -> None:
        """
        Registra una operacion ejecutada en el CSV.

        Args:
            consensus:   Resultado del sistema de consenso.
            side:        'Buy' o 'Sell'.
            qty:         Cantidad de contratos.
            stop_loss:   Precio del Stop Loss.
            take_profit: Precio del Take Profit.
            risk_usdt:   Monto en riesgo en USDT.
        """
        signal = consensus.final_signal
        direction_signals = [s for s in consensus.signals if s.signal == signal]
        avg_confidence = (
            sum(s.confidence for s in direction_signals) / len(direction_signals)
            if direction_signals else 0.0
        )

        sl_distance = abs(consensus.current_price - stop_loss)
        tp_usdt = sl_distance * self.config.risk_reward_ratio * qty * self.config.leverage

        strategies_summary = " | ".join(
            f"{s.name}={s.signal}" for s in consensus.signals
        ) if consensus.signals else "N/A (filtrado por ADX)"

        row = {
            "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":         self.config.symbol,
            "side":           side,
            "entry_price":    f"{consensus.current_price:.4f}",
            "stop_loss":      f"{stop_loss:.4f}",
            "take_profit":    f"{take_profit:.4f}",
            "qty":            qty,
            "risk_usdt":      f"{risk_usdt:.4f}",
            "tp_usdt":        f"{tp_usdt:.4f}",
            "confidence_avg": f"{avg_confidence:.4f}",
            "long_votes":     consensus.long_count,
            "short_votes":    consensus.short_count,
            "strategies":     strategies_summary,
            "status":         "OPEN",
            "pnl_usdt":       "0.0",
        }

        try:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
                writer.writerow(row)
            self._session_trades.append(row)
            self.logger.info(
                f"Trade registrado en journal: {side} {qty} {self.config.symbol} "
                f"@ {consensus.current_price:.4f} | Riesgo={risk_usdt:.2f} USDT"
            )
        except Exception as e:
            self.logger.error(f"Error escribiendo en Trade Journal: {e}")

    def get_session_metrics(self) -> Dict:
        """Calcula metricas de la sesion actual basadas en trades con PnL conocido."""
        closed_trades = [
            t for t in self._session_trades
            if t.get("status") in ("WIN", "LOSS")
        ]

        if not closed_trades:
            return {
                "total_trades": len(self._session_trades),
                "closed_trades": 0,
                "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "profit_factor": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0,
                "max_drawdown_usdt": 0.0,
            }

        wins   = [float(t["pnl_usdt"]) for t in closed_trades if t["status"] == "WIN"]
        losses = [abs(float(t["pnl_usdt"])) for t in closed_trades if t["status"] == "LOSS"]

        total_win  = sum(wins)
        total_loss = sum(losses)
        win_rate   = len(wins) / len(closed_trades) * 100 if closed_trades else 0.0
        profit_factor = total_win / total_loss if total_loss > 0 else float("inf")

        running_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in closed_trades:
            running_pnl += float(t["pnl_usdt"])
            peak = max(peak, running_pnl)
            dd = peak - running_pnl
            max_dd = max(max_dd, dd)

        return {
            "total_trades":      len(self._session_trades),
            "closed_trades":     len(closed_trades),
            "wins":              len(wins),
            "losses":            len(losses),
            "win_rate":          win_rate,
            "total_pnl":         total_win - total_loss,
            "profit_factor":     profit_factor,
            "avg_win":           total_win / len(wins) if wins else 0.0,
            "avg_loss":          total_loss / len(losses) if losses else 0.0,
            "max_drawdown_usdt": max_dd,
        }

    def print_session_metrics(self) -> None:
        """Imprime un resumen de metricas de la sesion al finalizar."""
        m = self.get_session_metrics()
        self.logger.info("=" * 62)
        self.logger.info("  TRADE JOURNAL --- METRICAS DE SESION")
        self.logger.info("=" * 62)
        self.logger.info(f"  CSV guardado en:    {self.csv_path}")
        self.logger.info(f"  Total operaciones:  {m['total_trades']}")
        self.logger.info(f"  Cerradas con PnL:   {m['closed_trades']}")
        self.logger.info(f"  Ganadoras:          {m['wins']}")
        self.logger.info(f"  Perdedoras:         {m['losses']}")
        self.logger.info(f"  Win Rate:           {m['win_rate']:.1f}%")
        self.logger.info(f"  PnL Total:          {m['total_pnl']:+.2f} USDT")
        self.logger.info(f"  Profit Factor:      {m['profit_factor']:.2f}")
        self.logger.info(f"  Prom. Ganadora:     +{m['avg_win']:.2f} USDT")
        self.logger.info(f"  Prom. Perdedora:    -{m['avg_loss']:.2f} USDT")
        self.logger.info(f"  Max Drawdown:       -{m['max_drawdown_usdt']:.2f} USDT")
        self.logger.info("=" * 62)

    # =========================================================================
    # RECONCILIACIÓN DE POSICIONES (detección de cierres por SL/TP)
    # =========================================================================

    def get_open_trades(self) -> List[Dict]:
        """
        Retorna todas las filas del CSV que tienen status='OPEN'.

        Usado por el mecanismo de reconciliación en cada ciclo para detectar
        si alguna posición fue cerrada por SL/TP en el exchange.

        Returns:
            Lista de dicts con los campos del CSV para las filas abiertas.
        """
        open_trades: List[Dict] = []
        if not os.path.exists(self.csv_path):
            return open_trades
        try:
            with open(self.csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("status") == "OPEN":
                        open_trades.append(dict(row))
        except Exception as e:
            self.logger.error(f"Error leyendo trade_journal.csv en get_open_trades: {e}")
        return open_trades

    def close_trade(
        self,
        timestamp: str,
        status: str,
        pnl_usdt: float,
        avg_exit_price: float = 0.0,
    ) -> bool:
        """
        Actualiza una fila del CSV marcándola como cerrada con su PnL real.

        Reescribe todo el archivo para garantizar consistencia. Diseñado para
        ser llamado por el mecanismo de reconciliación cuando detecta que Bybit
        cerró la posición automáticamente (SL/TP alcanzado).

        Args:
            timestamp:      Timestamp de la entrada (identifica unívocamente la fila).
            status:         'WIN' si el PnL es positivo, 'LOSS' si es negativo.
            pnl_usdt:       PnL realizado en USDT (puede ser negativo).
            avg_exit_price: Precio de salida real reportado por Bybit (opcional, informativo).

        Returns:
            bool: True si se encontró y actualizó la fila, False si no se encontró.
        """
        if not os.path.exists(self.csv_path):
            self.logger.warning("close_trade: CSV no encontrado.")
            return False

        updated = False
        rows: List[Dict] = []

        try:
            with open(self.csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("timestamp") == timestamp and row.get("status") == "OPEN":
                        row["status"]   = status
                        row["pnl_usdt"] = f"{pnl_usdt:.4f}"
                        updated = True
                        self.logger.info(
                            f"📝 Trade cerrado en journal: {timestamp} | "
                            f"Status={status} | PnL={pnl_usdt:+.2f} USDT"
                            + (f" | Exit={avg_exit_price:,.2f}" if avg_exit_price else "")
                        )
                    rows.append(row)

            if updated:
                # Reescribir el CSV completo con la fila actualizada
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
                    writer.writeheader()
                    writer.writerows(rows)

                # Actualizar también la copia en memoria de la sesión
                for trade in self._session_trades:
                    if trade.get("timestamp") == timestamp and trade.get("status") == "OPEN":
                        trade["status"]   = status
                        trade["pnl_usdt"] = f"{pnl_usdt:.4f}"
            else:
                self.logger.warning(
                    f"close_trade: No se encontró trade OPEN con timestamp={timestamp}"
                )

        except Exception as e:
            self.logger.error(f"Error actualizando trade_journal.csv: {e}")
            return False

        return updated

