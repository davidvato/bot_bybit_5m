"""
=============================================================================
bot.py - Bucle Principal del Bot de Trading BTC/USDT para Bybit
=============================================================================
Coordina todos los módulos y ejecuta el ciclo principal del bot.

FLUJO DE EJECUCIÓN POR CICLO (cada vela de 5m):
  1. Calcular tiempo exacto hasta el cierre de la siguiente vela
  2. Dormir hasta N segundos antes del cierre de vela
  3. Obtener klines actualizados
  4. Calcular todos los indicadores técnicos
  5. Evaluar las 4 estrategias y el sistema de consenso
  6. Si hay consenso (≥3 estrategias) → ejecutar mediante RiskManager
  7. Registrar resultado y volver al paso 1

SINCRONIZACIÓN CON EL CIERRE DE VELA:
  El bot se sincroniza con el tiempo del servidor de Bybit para ejecutarse
  exactamente al cierre de cada vela de 5m (usando el timestamp del servidor,
  no el reloj local del sistema, para evitar desincronización).
=============================================================================
"""

import sys
import time
import signal
import traceback
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import BotConfig, config
from exchange import ExchangeClient
from logger import setup_logger
from notifier import TelegramNotifier
from risk_manager import RiskManager
from strategies import ConsensusResult, IndicatorEngine, StrategyEngine
from trade_journal import TradeJournal


class TradingBot:
    """
    Bot de trading principal que orquesta todos los componentes.
    Implementa el bucle de ejecución sincronizado al cierre de velas de 5m.
    """

    def __init__(self, cfg: BotConfig) -> None:
        self.config = cfg
        self.logger = setup_logger(
            __name__, cfg.log_file, cfg.get_log_level()
        )
        self.running = False

        # Inicializar módulos
        self.logger.info("🔧 Inicializando módulos del bot...")
        self.exchange         = ExchangeClient(cfg)
        self.indicator_engine = IndicatorEngine(cfg)
        self.strategy_engine  = StrategyEngine(cfg)
        self.risk_manager     = RiskManager(cfg, self.exchange)
        self.trade_journal    = TradeJournal(cfg)
        self.notifier         = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)

        # Configurar captura de señales del sistema (Ctrl+C, kill)
        signal.signal(signal.SIGINT,  self._graceful_shutdown)
        signal.signal(signal.SIGTERM, self._graceful_shutdown)

        # Estadísticas de sesión
        self._cycles_run     = 0
        self._signals_long   = 0
        self._signals_short  = 0
        self._signals_hold   = 0
        self._orders_placed  = 0
        self._session_start  = datetime.now(timezone.utc)

        # Rastreador de posición activa en memoria.
        # Se rellena al ejecutar una orden y se limpia al detectar el cierre.
        # Campos: timestamp, side, entry_price, stop_loss, take_profit, risk_usdt, candles_open
        self._pending_trade: Optional[dict] = None

    # =========================================================================
    # SECCIÓN: TEMPORIZADOR Y SINCRONIZACIÓN CON VELAS
    # =========================================================================

    def _get_server_timestamp_ms(self) -> Optional[int]:
        """
        Obtiene el timestamp actual del servidor de Bybit en milisegundos.
        Preferimos el tiempo del servidor para evitar desfases del reloj local.

        Returns:
            int: Timestamp en milisegundos, o None si hay error.
        """
        try:
            response = self.exchange.session.get_server_time()
            return int(response["result"]["timeNano"]) // 1_000_000
        except Exception:
            # Fallback al tiempo local del sistema
            return int(time.time() * 1000)

    def _seconds_until_next_candle(self) -> float:
        """
        Calcula los segundos restantes hasta el cierre de la próxima vela de 5m.

        La estrategia es ejecutar el análisis N segundos ANTES del cierre
        para que la orden quede lista justo cuando abre la nueva vela.

        Returns:
            float: Segundos a esperar (mínimo 0).
        """
        timeframe_seconds = int(self.config.timeframe) * 60  # 5m = 300 segundos
        now_ms = self._get_server_timestamp_ms()
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        # Calcular inicio de la vela actual y tiempo hasta el cierre
        current_time_s  = now_ms / 1000.0
        candle_start    = (current_time_s // timeframe_seconds) * timeframe_seconds
        candle_close    = candle_start + timeframe_seconds

        # Ejecutar N segundos antes del cierre para que la señal esté lista
        target_time     = candle_close - self.config.seconds_before_close
        wait_seconds    = target_time - current_time_s

        # Si ya pasó el tiempo objetivo (o estamos muy cerca), esperar hasta el siguiente cierre
        if wait_seconds < 1:
            wait_seconds = timeframe_seconds + wait_seconds

        return max(0.0, wait_seconds)

    # =========================================================================
    # SECCIÓN: RECONCILIACIÓN DE POSICIONES
    # =========================================================================

    def _reconcile_position(self) -> None:
        """
        Paso 0 de cada ciclo: reconcilia el estado interno con el exchange.

        Si el bot registró una posición abierta (_pending_trade) pero Bybit
        ya no la muestra (fue cerrada por SL/TP automáticamente), este método:
          1. Detecta el cierre consultando la posición en Bybit.
          2. Obtiene el PnL real desde get_last_closed_pnl().
          3. Actualiza el CSV del journal con status WIN/LOSS y pnl_usdt real.
          4. Registra la pérdida en el risk_manager (drawdown diario).
          5. Envía notificación de Telegram con el resultado.
          6. Limpia _pending_trade para permitir la próxima entrada.

        [Mejora #2] Si la posición lleva más de max_candles_open velas sin
        cerrar, se cierra a mercado para evitar acumulación de funding rate.
        """
        if self._pending_trade is None:
            return  # Nada que reconciliar

        open_pos = self.exchange.get_open_position()

        if open_pos is not None:
            # Incrementar el contador de velas abiertas [Mejora #2]
            self._pending_trade["candles_open"] = (
                self._pending_trade.get("candles_open", 0) + 1
            )
            candles_open = self._pending_trade["candles_open"]
            upnl = open_pos.get("unrealised_pnl", 0.0)
            self.logger.info(
                f"📌 Posición {open_pos['side']} sigue ACTIVA | "
                f"Vela #{candles_open} | Unrealized PnL: {upnl:+.2f} USDT"
            )

            # --- Mejora #2: Expiración automática de trades ---
            max_candles = self.config.max_candles_open
            if max_candles > 0 and candles_open >= max_candles:
                self.logger.warning(
                    f"⏰ EXPIRACIÓN DE TRADE: La posición lleva {candles_open} velas abierta "
                    f"(máximo configurado: {max_candles} velas = {max_candles * int(self.config.timeframe)} min). "
                    f"PnL no realizado: {upnl:+.2f} USDT. Cerrando a mercado..."
                )
                existing_side = open_pos["side"]
                close_result  = self.exchange.close_position(
                    existing_side, open_pos["size"]
                )
                if close_result:
                    self.logger.info("✅ Posición cerrada por expiración de tiempo.")
                    # Obtener PnL real post-cierre
                    time.sleep(1)  # Pequeña pausa para que Bybit registre el cierre
                    closed_records = self.exchange.get_last_closed_pnl(limit=3)
                    pnl_usdt   = 0.0
                    exit_price = 0.0
                    if closed_records:
                        record     = closed_records[0]
                        pnl_usdt   = record["closed_pnl"]
                        exit_price = record["avg_exit_price"]
                    else:
                        # Estimación: usar el PnL no realizado en el momento del cierre
                        pnl_usdt = float(upnl)

                    status = "WIN" if pnl_usdt > 0 else "LOSS"
                    ts = self._pending_trade.get("timestamp", "")
                    risk_usdt = self._pending_trade.get("risk_usdt", 0.0)
                    if ts:
                        self.trade_journal.close_trade(
                            timestamp=ts,
                            status="EXPIRED",
                            pnl_usdt=pnl_usdt,
                            avg_exit_price=exit_price,
                            candles_open=candles_open,
                            close_reason="EXPIRACION",
                            risk_usdt=risk_usdt,
                        )
                    if pnl_usdt < 0:
                        self.risk_manager.register_trade_loss(abs(pnl_usdt))
                    side = self._pending_trade.get("side", "?")
                    self.logger.info(
                        f"{'✅' if status == 'WIN' else '❌'} POSICIÓN EXPIRADA {side} | "
                        f"PnL={pnl_usdt:+.2f} USDT | Velas={candles_open}"
                    )
                    self.notifier.notify_position_closed(
                        reason="EXPIRACION",
                        symbol=self.config.symbol,
                        side=side,
                        pnl=pnl_usdt,
                    )
                    self._pending_trade = None
                else:
                    self.logger.error(
                        "❌ No se pudo cerrar la posición expirada. Se reintentara en el siguiente ciclo."
                    )
            return

        # ── Posición cerrada detectada ────────────────────────────────────
        candles_open = self._pending_trade.get("candles_open", 0)
        self.logger.info(
            "🔔 RECONCILIACIÓN: Posición cerrada detectada en Bybit "
            "(SL/TP tocado o liquidación). Consultando PnL real..."
        )

        # Obtener PnL real del exchange
        closed_records = self.exchange.get_last_closed_pnl(limit=3)
        pnl_usdt    = 0.0
        exit_price  = 0.0

        if closed_records:
            # El registro más reciente corresponde a esta posición
            record      = closed_records[0]
            pnl_usdt    = record["closed_pnl"]
            exit_price  = record["avg_exit_price"]
            entry_price = record["avg_entry_price"]
            self.logger.info(
                f"💵 PnL real Bybit: {pnl_usdt:+.4f} USDT | "
                f"Entry={entry_price:,.2f} | Exit={exit_price:,.2f}"
            )
        else:
            self.logger.warning(
                "⚠️  No se pudo obtener el PnL cerrado de Bybit. "
                "Estimando resultado desde SL configurado."
            )
            # Estimación conservadora: asumir que tocó el SL
            pnl_usdt = -abs(self._pending_trade.get("risk_usdt", 0.0))

        # Determinar si fue ganadora o perdedora
        status = "WIN" if pnl_usdt > 0 else "LOSS"
        emoji  = "✅" if status == "WIN" else "❌"

        # Actualizar el journal CSV con todos los campos [Mejoras #2 y #3]
        ts = self._pending_trade.get("timestamp", "")
        risk_usdt = self._pending_trade.get("risk_usdt", 0.0)
        if ts:
            self.trade_journal.close_trade(
                timestamp=ts,
                status=status,
                pnl_usdt=pnl_usdt,
                avg_exit_price=exit_price,
                candles_open=candles_open,
                close_reason="SL/TP",
                risk_usdt=risk_usdt,
            )

        # Registrar pérdida en el drawdown diario
        if pnl_usdt < 0:
            self.risk_manager.register_trade_loss(abs(pnl_usdt))

        # Notificación Telegram
        side = self._pending_trade.get("side", "?")
        self.logger.info(
            f"{emoji} POSICIÓN {side} CERRADA POR SL/TP | "
            f"PnL={pnl_usdt:+.2f} USDT | Status={status} | Velas={candles_open}"
        )
        self.notifier.notify_position_closed(
            reason="SL/TP",
            symbol=self.config.symbol,
            side=side,
            pnl=pnl_usdt,
        )

        # Limpiar el rastreador para la próxima operación
        self._pending_trade = None

    # =========================================================================
    # SECCIÓN: CICLO PRINCIPAL DE ANÁLISIS
    # =========================================================================

    def _run_analysis_cycle(self) -> None:
        """
        Ejecuta un ciclo completo de análisis y toma de decisiones.
        Este método se llama en cada cierre de vela de 5m.
        """
        self._cycles_run += 1
        cycle_start = time.time()

        self.logger.info(
            f"\n{'='*60}\n"
            f"  🕐 CICLO #{self._cycles_run} | "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"{'='*60}"
        )

        # --- Paso 0: Reconciliar posición con el exchange (detecta cierres por SL/TP) ---
        self._reconcile_position()

        # --- Paso 1: Obtener datos de mercado ---
        self.logger.info("📡 Obteniendo datos de mercado...")
        df = self.exchange.get_klines()
        if df is None or df.empty:
            self.logger.error("❌ No se pudieron obtener datos de mercado. Saltando ciclo.")
            return

        # --- Paso 2: Calcular indicadores técnicos ---
        self.logger.info("🔢 Calculando indicadores técnicos...")
        df_with_indicators = self.indicator_engine.calculate_all(df)
        if df_with_indicators is None:
            self.logger.error("❌ Error calculando indicadores. Saltando ciclo.")
            return

        # --- Paso 3: Evaluar consenso de las 4 estrategias ---
        consensus = self.strategy_engine.evaluate_consensus(df_with_indicators)

        # Actualizar estadísticas
        if consensus.final_signal == "LONG":
            self._signals_long += 1
        elif consensus.final_signal == "SHORT":
            self._signals_short += 1
        else:
            self._signals_hold += 1

        # --- Paso 4: Ejecutar señal si hay consenso ---
        if consensus.final_signal != "HOLD":
            executed = self.risk_manager.execute_signal(consensus)
            if executed:
                self._orders_placed += 1
                # Recalcular SL/TP tal como lo hizo el risk_manager
                side = "Buy" if consensus.final_signal == "LONG" else "Sell"
                info = self.risk_manager._get_instrument_info()
                sl_price, tp_price = self.risk_manager.calculate_sl_tp(
                    consensus.current_price, consensus.atr, side
                )
                balance = self.exchange.get_wallet_balance() or 0.0
                _, risk_usdt = self.risk_manager.calculate_position_size(
                    balance, consensus.current_price, sl_price
                )
                qty_raw = (risk_usdt * self.config.leverage) / max(
                    abs(consensus.current_price - sl_price), 0.0001
                )
                qty = self.risk_manager._round_qty(
                    qty_raw, info["qty_step"] if info else 0.001
                )
                # Timestamp usado para identificar este trade en el journal
                trade_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

                self.trade_journal.log_trade(
                    consensus=consensus,
                    side=side,
                    qty=qty,
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    risk_usdt=risk_usdt,
                )

                # Registrar en el rastreador de posición activa para reconciliación
                self._pending_trade = {
                    "timestamp":   trade_ts,
                    "side":        side,
                    "entry_price": consensus.current_price,
                    "stop_loss":   sl_price,
                    "take_profit": tp_price,
                    "risk_usdt":   risk_usdt,
                    "candles_open": 0,  # Contador de velas abiertas [Mejora #2]
                }
                self.logger.info(
                    f"🔖 Posición registrada en rastreador interno: "
                    f"{side} @ {consensus.current_price:,.2f} | "
                    f"SL={sl_price:,.2f} | TP={tp_price:,.2f}"
                )
        else:
            self.logger.info("⏸️  Sin señal de consenso. Esperando próxima vela.")

        # --- Log de performance del ciclo ---
        elapsed = time.time() - cycle_start
        self.logger.info(
            f"⏱️  Ciclo #{self._cycles_run} completado en {elapsed:.2f}s | "
            f"Stats: LONG={self._signals_long}, SHORT={self._signals_short}, "
            f"HOLD={self._signals_hold}, Órdenes={self._orders_placed}"
        )

    # =========================================================================
    # SECCIÓN: INICIO Y CONTROL DEL BOT
    # =========================================================================

    def _initialize(self) -> bool:
        """
        Realiza las configuraciones iniciales antes de comenzar el bucle.
        Configura el apalancamiento en el exchange.

        Returns:
            bool: True si la inicialización fue exitosa.
        """
        self.logger.info("=" * 60)
        self.logger.info("  🤖 BOT DE TRADING BTC/USDT BYBIT - INICIANDO")
        self.logger.info("=" * 60)
        self.logger.info(f"  Símbolo:       {self.config.symbol}")
        self.logger.info(f"  Temporalidad:  {self.config.timeframe}m")
        self.logger.info(f"  Modo:          {'⚠️  TESTNET' if self.config.testnet else '🔴 MAINNET REAL'}")
        self.logger.info(f"  Apalancamiento:{self.config.leverage}x")
        self.logger.info(f"  Riesgo/trade:  {self.config.risk_per_trade*100:.1f}%")
        self.logger.info(f"  Consenso mín:  {self.config.min_consensus}/3 estrategias")
        self.logger.info(f"  ATR SL mult:   {self.config.atr_sl_multiplier}x ATR")
        self.logger.info(f"  R/R ratio:     1:{self.config.risk_reward_ratio}")
        self.logger.info("=" * 60)

        # Configurar apalancamiento en el exchange
        if not self.exchange.set_leverage():
            self.logger.error(
                "❌ No se pudo configurar el apalancamiento. "
                "Verifica que el modo sea 'Cross' o 'Isolated' en tu cuenta."
            )
            # No es fatal si ya está configurado correctamente
            # return False

        # Verificar balance inicial
        balance = self.exchange.get_wallet_balance()
        if balance is not None:
            self.logger.info(f"💰 Balance inicial: {balance:.2f} USDT")
        else:
            self.logger.warning("⚠️  No se pudo verificar el balance inicial.")

        # Notificar inicio por Telegram
        self.notifier.notify_bot_started(
            symbol=self.config.symbol,
            timeframe=self.config.timeframe,
            leverage=self.config.leverage,
            balance=balance or 0.0,
            testnet=self.config.testnet,
        )

        return True

    def run(self) -> None:
        """
        Inicia el bucle principal del bot.
        El bot corre indefinidamente hasta recibir señal de parada (Ctrl+C).
        """
        # Validar configuración antes de iniciar
        try:
            self.config.validate()
        except ValueError as e:
            self.logger.error(f"❌ Configuración inválida:\n{e}")
            sys.exit(1)

        # Inicializar exchange y configuraciones
        if not self._initialize():
            self.logger.error("❌ Falló la inicialización. Abortando.")
            sys.exit(1)

        self.running = True
        self.logger.info("🟢 Bot iniciado. Esperando al cierre de la primera vela de 5m...\n")

        # =====================================================================
        # BUCLE PRINCIPAL
        # Sincronizado exactamente con el cierre de cada vela de 5m
        # =====================================================================
        while self.running:
            try:
                # Calcular tiempo hasta el próximo cierre de vela
                wait_secs = self._seconds_until_next_candle()
                next_close_dt = datetime.now(timezone.utc) + timedelta(seconds=wait_secs)

                self.logger.info(
                    f"⏳ Próxima ejecución en {wait_secs:.1f}s "
                    f"({next_close_dt.strftime('%H:%M:%S UTC')})"
                )

                # Dormir hasta el momento de ejecución
                # Usamos intervalos cortos para responder a señales de parada
                sleep_interval = min(wait_secs, 10.0)
                elapsed_sleep  = 0.0
                while elapsed_sleep < wait_secs and self.running:
                    time.sleep(sleep_interval)
                    elapsed_sleep  += sleep_interval
                    sleep_interval  = min(wait_secs - elapsed_sleep, 10.0)
                    if sleep_interval < 0:
                        break

                if not self.running:
                    break

                # Ejecutar ciclo de análisis
                self._run_analysis_cycle()

            except KeyboardInterrupt:
                # Ctrl+C manejado por _graceful_shutdown
                break

            except Exception as e:
                self.logger.error(
                    f"❌ Error inesperado en el bucle principal:\n"
                    f"{traceback.format_exc()}"
                )
                self.notifier.notify_critical_error(traceback.format_exc())
                self.logger.info("⏳ Esperando 30s antes de reintentar...")
                time.sleep(30)

                # Intentar reconectar si el error pudo ser de red
                try:
                    self.exchange.reconnect()
                except Exception as reconnect_err:
                    self.logger.error(f"❌ Reconexión fallida: {reconnect_err}")

        self._print_session_summary()

    def _graceful_shutdown(self, signum, frame) -> None:
        """
        Maneja el apagado gracioso del bot (Ctrl+C / kill).
        Detiene el bucle de forma limpia sin cortar operaciones en curso.
        """
        self.logger.info(
            f"\n⛔ Señal de parada recibida (signal {signum}). "
            "Deteniendo el bot de forma segura..."
        )
        self.running = False

    def _print_session_summary(self) -> None:
        """Imprime un resumen de la sesión al finalizar."""
        duration = datetime.now(timezone.utc) - self._session_start
        hours, remainder = divmod(int(duration.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{hours:02d}h {minutes:02d}m {seconds:02d}s"

        self.logger.info("\n" + "=" * 60)
        self.logger.info("  📊 RESUMEN DE SESIÓN")
        self.logger.info("=" * 60)
        self.logger.info(f"  Duración:         {duration_str}")
        self.logger.info(f"  Ciclos ejecutados:{self._cycles_run}")
        self.logger.info(f"  Señales LONG:     {self._signals_long}")
        self.logger.info(f"  Señales SHORT:    {self._signals_short}")
        self.logger.info(f"  Señales HOLD:     {self._signals_hold}")
        self.logger.info(f"  Órdenes enviadas: {self._orders_placed}")
        self.logger.info("=" * 60)
        self.logger.info("🔴 Bot detenido.")
        # N3: Mostrar métricas detalladas del trade journal
        self.trade_journal.print_session_metrics()
        # Notificar parada por Telegram
        self.notifier.notify_bot_stopped(
            cycles=self._cycles_run,
            orders=self._orders_placed,
            duration_str=duration_str,
        )


# =============================================================================
# PUNTO DE ENTRADA PRINCIPAL
# =============================================================================

if __name__ == "__main__":
    # Crear el bot con la configuración cargada desde .env
    bot = TradingBot(config)

    # Iniciar el bucle principal
    bot.run()
