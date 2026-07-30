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
                # N2: Registrar la operación en el Trade Journal
                # Recalculamos SL/TP tal como lo hizo el risk_manager para el log
                side = "Buy" if consensus.final_signal == "LONG" else "Sell"
                info = self.risk_manager._get_instrument_info()
                tick_size = info["tick_size"] if info else 0.5
                sl_price, tp_price = self.risk_manager.calculate_sl_tp(
                    consensus.current_price, consensus.atr, side
                )
                balance = self.exchange.get_wallet_balance() or 0.0
                _, risk_usdt = self.risk_manager.calculate_position_size(
                    balance, consensus.current_price, sl_price
                )
                # Obtener qty del último cálculo (aproximado — el tamaño real lo confirmó el exchange)
                qty_raw = (risk_usdt * self.config.leverage) / max(
                    abs(consensus.current_price - sl_price), 0.0001
                )
                from risk_manager import RiskManager
                qty = self.risk_manager._round_qty(qty_raw, info["qty_step"] if info else 0.001)
                self.trade_journal.log_trade(
                    consensus=consensus,
                    side=side,
                    qty=qty,
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    risk_usdt=risk_usdt,
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
