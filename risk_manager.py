"""
=============================================================================
risk_manager.py - Módulo de Gestión de Riesgo y Ejecución de Órdenes
=============================================================================
Implementa:
  - Cálculo dinámico del tamaño de posición (% del balance)
  - Stop Loss dinámico basado en ATR (1.5 * ATR)
  - Take Profit con ratio R/R mínimo de 1:2
  - Validación de posiciones existentes (anti-duplicado)
  - Lógica de cierre anticipado en caso de señal contraria
=============================================================================
"""

import logging
import math
from typing import Optional, Tuple

from config import BotConfig
from exchange import ExchangeClient
from logger import setup_logger
from notifier import TelegramNotifier
from strategies import ConsensusResult


class RiskManager:
    """
    Gestor de riesgo que controla el tamaño de posición, SL/TP y
    protección contra operaciones duplicadas.
    """

    def __init__(self, config: BotConfig, exchange: ExchangeClient) -> None:
        self.config   = config
        self.exchange = exchange
        self.logger   = setup_logger(__name__, config.log_file, config.get_log_level())
        self.notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)
        self._instrument_info: Optional[dict] = None

        # --- Rastreador de drawdown diario (R1) ---
        # Resetea cada día calendario (UTC). Registra el PnL de cada trade cerrado.
        from datetime import date
        self._daily_loss_date: date = date.today()
        self._daily_loss_usdt: float = 0.0  # acumula solo las pérdidas (valor positivo)

    def _get_instrument_info(self) -> Optional[dict]:
        """Obtiene y cachea la información del instrumento (precisión, mín qty, etc.)."""
        if self._instrument_info is None:
            self._instrument_info = self.exchange.get_instrument_info()
        return self._instrument_info

    def _check_daily_drawdown(self, potential_risk_usdt: float) -> bool:
        """
        Verifica si la pérdida acumulada del día supera el límite configurado.
        Resetea el contador al inicio de cada día calendario (UTC).

        Args:
            potential_risk_usdt: Pérdida máxima de la operación que se intenta abrir.

        Returns:
            bool: True si se puede operar, False si el límite diario fue alcanzado.
        """
        from datetime import date

        # Reset diario
        today = date.today()
        if today != self._daily_loss_date:
            self.logger.info(
                f"🗓️  Nuevo día: reseteando drawdown diario. "
                f"Pérdida del día anterior: -{self._daily_loss_usdt:.2f} USDT"
            )
            self._daily_loss_date = today
            self._daily_loss_usdt = 0.0

        # Sin límite configurado (0.0)
        if self.config.max_daily_loss <= 0:
            return True

        projected_loss = self._daily_loss_usdt + potential_risk_usdt
        if projected_loss > self.config.max_daily_loss:
            self.logger.warning(
                f"⛔ DRAWDOWN DIARIO ALCANZADO: Pérdida acumulada={self._daily_loss_usdt:.2f} USDT | "
                f"Riesgo operación={potential_risk_usdt:.2f} USDT | "
                f"Límite={self.config.max_daily_loss:.2f} USDT. "
                f"No se abrirán nuevas posiciones hoy."
            )
            self.notifier.notify_daily_drawdown(
                accumulated_loss=self._daily_loss_usdt,
                limit=self.config.max_daily_loss,
                risk_attempted=potential_risk_usdt,
            )
            return False

        self.logger.info(
            f"📊 Drawdown diario: {self._daily_loss_usdt:.2f}/{self.config.max_daily_loss:.2f} USDT "
            f"(+{potential_risk_usdt:.2f} potencial). Dentro del límite."
        )
        return True

    def register_trade_loss(self, loss_usdt: float) -> None:
        """
        Registra una pérdida realizada en el rastreador diario.
        Llamar después de confirmar que un SL fue tocado.

        Args:
            loss_usdt: Monto de la pérdida en USDT (valor positivo).
        """
        if loss_usdt > 0:
            self._daily_loss_usdt += loss_usdt
            self.logger.info(
                f"📉 Pérdida registrada: -{loss_usdt:.2f} USDT | "
                f"Total diario: -{self._daily_loss_usdt:.2f} USDT"
            )

    def _round_qty(self, qty: float, qty_step: float) -> float:
        """
        Redondea la cantidad al step permitido por Bybit.
        Bybit no acepta cantidades arbitrarias; deben ser múltiplos del qty_step.

        Ejemplo: qty=0.00372, qty_step=0.001 → resultado=0.003
        """
        if qty_step <= 0:
            return qty
        # Calcular cuántos decimales tiene el step
        decimals = max(0, -int(math.floor(math.log10(qty_step))))
        rounded = math.floor(qty / qty_step) * qty_step
        return round(rounded, decimals)

    def _round_price(self, price: float, tick_size: float) -> float:
        """Redondea el precio al tick size del instrumento."""
        if tick_size <= 0:
            return price
        decimals = max(0, -int(math.floor(math.log10(tick_size))))
        rounded = round(round(price / tick_size) * tick_size, decimals)
        return rounded

    def calculate_position_size(
        self,
        balance: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> Tuple[float, float]:
        """
        Calcula el tamaño de posición basado en el riesgo por operación.

        Fórmula:
          Riesgo_USDT    = balance * risk_per_trade
          Distancia_SL   = |entry_price - stop_loss_price|
          Qty_contratos  = (Riesgo_USDT * leverage) / Distancia_SL

        Args:
            balance: Balance disponible en USDT.
            entry_price: Precio estimado de entrada (precio actual de mercado).
            stop_loss_price: Precio del Stop Loss calculado con ATR.

        Returns:
            Tuple (qty_redondeada, riesgo_usdt)
              qty_redondeada: Cantidad de contratos a operar (ya redondeada al step).
              riesgo_usdt: Monto en USDT que se está arriesgando en esta operación.
        """
        info = self._get_instrument_info()
        if not info:
            self.logger.error("❌ No se pudo obtener info del instrumento para calcular qty.")
            return 0.0, 0.0

        # Distancia al SL en USDT por contrato
        sl_distance = abs(entry_price - stop_loss_price)
        if sl_distance <= 0:
            self.logger.error("❌ Distancia SL es 0 o negativa. No se puede calcular qty.")
            return 0.0, 0.0

        # Monto a arriesgar en USDT
        risk_usdt = balance * self.config.risk_per_trade

        # Cantidad de contratos con apalancamiento:
        # (riesgo_usdt * leverage) / sl_distance da los contratos que
        # permiten perder exactamente risk_usdt si se activa el SL
        qty_raw = (risk_usdt * self.config.leverage) / sl_distance

        # --- MARGIN CAP: Verificar que el margen requerido no supere el balance ---
        # Margen requerido = (qty * entry_price) / leverage
        # Si supera el balance disponible, Bybit retornará ErrCode 110007.
        # Se limita al 95% del balance para dejar margen para comisiones y funding.
        max_margin_usdt = balance * 0.95
        max_qty_by_margin = (max_margin_usdt * self.config.leverage) / entry_price

        if qty_raw > max_qty_by_margin:
            self.logger.warning(
                f"⚠️  MARGIN CAP APLICADO: Qty por riesgo ({qty_raw:.4f}) supera el "
                f"máximo permitido por margen disponible ({max_qty_by_margin:.4f}). "
                f"Margen requerido original: "
                f"{(qty_raw * entry_price / self.config.leverage):.2f} USDT | "
                f"Balance disponible: {balance:.2f} USDT. "
                f"Se ajusta qty para no exceder el margen."
            )
            qty_raw = max_qty_by_margin

        # Redondear al step mínimo del instrumento
        qty_rounded = self._round_qty(qty_raw, info["qty_step"])

        # Validar que supera el mínimo de Bybit
        if qty_rounded < info["min_order_qty"]:
            self.logger.warning(
                f"⚠️  Qty calculada ({qty_rounded}) es menor que el mínimo "
                f"({info['min_order_qty']}). Se usará el mínimo."
            )
            qty_rounded = info["min_order_qty"]

        # Verificación final: confirmar que el margen requerido es viable
        margin_required = (qty_rounded * entry_price) / self.config.leverage
        self.logger.info(
            f"📐 Tamaño de posición calculado: "
            f"Balance={balance:.2f} USDT | "
            f"Riesgo={risk_usdt:.2f} USDT ({self.config.risk_per_trade*100:.1f}%) | "
            f"SL Distance={sl_distance:.4f} | "
            f"Qty={qty_rounded} | "
            f"Margen requerido={margin_required:.2f} USDT ({margin_required/balance*100:.1f}% del balance)"
        )

        return qty_rounded, risk_usdt

    def calculate_sl_tp(
        self,
        entry_price: float,
        atr: float,
        side: str,
    ) -> Tuple[float, float]:
        """
        Calcula Stop Loss y Take Profit dinámicos basados en ATR.

        Fórmulas:
          SL_distance = ATR * atr_sl_multiplier    (ej: ATR * 1.5)
          TP_distance = SL_distance * risk_reward   (ej: SL * 2.0 = 3 * ATR)

          Para LONG:
            SL = entry_price - SL_distance
            TP = entry_price + TP_distance

          Para SHORT:
            SL = entry_price + SL_distance
            TP = entry_price - TP_distance

        Args:
            entry_price: Precio de entrada a la posición.
            atr: Valor actual del ATR (14 periodos).
            side: 'Buy' para Long, 'Sell' para Short.

        Returns:
            Tuple (stop_loss_price, take_profit_price)
        """
        info = self._get_instrument_info()
        tick_size = info["tick_size"] if info else 0.5

        sl_distance = atr * self.config.atr_sl_multiplier
        tp_distance = sl_distance * self.config.risk_reward_ratio

        if side == "Buy":  # Long
            sl_price = entry_price - sl_distance
            tp_price = entry_price + tp_distance
        else:  # Short
            sl_price = entry_price + sl_distance
            tp_price = entry_price - tp_distance

        # Redondear al tick size del instrumento
        sl_price = self._round_price(sl_price, tick_size)
        tp_price = self._round_price(tp_price, tick_size)

        self.logger.info(
            f"🎯 SL/TP calculados ({side}): "
            f"Entrada={entry_price:.4f} | "
            f"ATR={atr:.4f} | "
            f"SL={sl_price:.4f} (-{sl_distance:.4f}) | "
            f"TP={tp_price:.4f} (+{tp_distance:.4f}) | "
            f"R/R=1:{self.config.risk_reward_ratio}"
        )

        return sl_price, tp_price

    def execute_signal(self, consensus: ConsensusResult) -> bool:
        """
        Punto de entrada principal para la ejecución de señales.
        Coordina la verificación de posiciones, cálculo de riesgo y envío de órdenes.

        Lógica de ejecución:
          1. Verificar si ya hay posición abierta
          2. Si hay posición con señal CONTRARIA → cerrar y abrir nueva
          3. Si hay posición con misma dirección → ignorar (anti-duplicado)
          4. Verificar límite de drawdown diario (R1)
          5. Verificar confidence score mínimo del consenso (R4)
          6. Si no hay posición → calcular SL/TP y abrir nueva posición

        Args:
            consensus: Resultado del sistema de consenso con señal validada.

        Returns:
            bool: True si se ejecutó alguna acción, False si no.
        """
        signal = consensus.final_signal

        # Señal de espera → no hacer nada
        if signal == "HOLD":
            self.logger.info("⏸️  Señal HOLD. Sin acción.")
            return False

        # --- R4: Verificar confidence score del consenso ---
        # Calcular confianza promedio de las estrategias que votaron en la dirección de la señal
        direction_signals = [
            s for s in consensus.signals
            if s.signal == signal
        ]
        if direction_signals:
            avg_confidence = sum(s.confidence for s in direction_signals) / len(direction_signals)
            self.logger.info(
                f"📊 Confidence promedio de señales {signal}: {avg_confidence:.2f}"
            )
            if avg_confidence < 0.3:
                self.logger.warning(
                    f"⚠️  Confidence muy baja ({avg_confidence:.2f} < 0.30). "
                    f"Señal {signal} ignorada para evitar entrada de baja calidad."
                )
                return False

        # --- Paso 1: Verificar posición existente (ANTI-DUPLICADO) ---
        open_position = self.exchange.get_open_position()

        if open_position:
            existing_side = open_position["side"]  # 'Buy' o 'Sell'
            expected_side = "Buy" if signal == "LONG" else "Sell"

            if existing_side == expected_side:
                # Ya tenemos una posición en la misma dirección → no duplicar
                self.logger.info(
                    f"🚫 POSICIÓN DUPLICADA EVITADA: Ya existe posición {existing_side}. "
                    f"Señal {signal} ignorada."
                )
                return False
            else:
                # Señal contraria a la posición existente → cerrar primero
                self.logger.info(
                    f"🔄 Señal contraria detectada. Cerrando posición {existing_side} "
                    f"antes de abrir {signal}..."
                )
                close_result = self.exchange.close_position(
                    existing_side, open_position["size"]
                )
                if not close_result:
                    self.logger.error(
                        "❌ No se pudo cerrar la posición existente. "
                        "Abortando nueva entrada para evitar sobreexposición."
                    )
                    return False
                # Notificar cierre por señal contraria
                self.notifier.notify_position_closed(
                    reason="CONTRARIA",
                    symbol=self.config.symbol,
                    side=existing_side,
                    pnl=open_position.get("unrealised_pnl"),
                )

        # --- Paso 2: Obtener balance disponible ---
        balance = self.exchange.get_wallet_balance()
        if not balance or balance <= 0:
            self.logger.error("❌ Balance no disponible o es 0. No se puede operar.")
            return False

        # --- Paso 3: Determinar lado de la orden y obtener info del instrumento ---
        side = "Buy" if signal == "LONG" else "Sell"
        entry_price = consensus.current_price
        atr = consensus.atr

        if atr <= 0:
            self.logger.error(
                f"❌ ATR inválido ({atr}). No se puede calcular SL/TP dinámico."
            )
            return False

        # Obtener tick_size para redondeo correcto de precios en la orden
        info = self._get_instrument_info()
        tick_size = info["tick_size"] if info else 0.5

        # --- Paso 4: Calcular SL y TP dinámicos con ATR ---
        sl_price, tp_price = self.calculate_sl_tp(entry_price, atr, side)

        # Validación de seguridad: SL y TP deben ser precios positivos
        if sl_price <= 0 or tp_price <= 0:
            self.logger.error(
                f"❌ Precios SL/TP inválidos: SL={sl_price}, TP={tp_price}."
            )
            return False

        # --- Paso 5: Calcular tamaño de posición basado en riesgo ---
        qty, risk_usdt = self.calculate_position_size(balance, entry_price, sl_price)

        if qty <= 0:
            self.logger.error("❌ Cantidad calculada es 0. No se puede enviar la orden.")
            return False

        # --- R1: Verificar límite de drawdown diario ANTES de abrir la orden ---
        if not self._check_daily_drawdown(risk_usdt):
            return False

        # --- Paso 6: Ejecutar la orden en el exchange ---
        self.logger.info(
            f"🚀 EJECUTANDO ORDEN {signal}: "
            f"Side={side} | Qty={qty} | Entry~{entry_price:.4f} | "
            f"SL={sl_price:.4f} | TP={tp_price:.4f} | "
            f"Riesgo={risk_usdt:.2f} USDT"
        )

        result = self.exchange.place_order(
            side=side,
            qty=qty,
            stop_loss=sl_price,
            take_profit=tp_price,
            tick_size=tick_size,
        )

        if result:
            self.logger.info(
                f"✅ ¡ORDEN {signal} EJECUTADA EXITOSAMENTE! "
                f"OrderID: {result.get('orderId', 'N/A')}"
            )
            self.notifier.notify_order_executed(
                signal=signal,
                symbol=self.config.symbol,
                entry_price=entry_price,
                qty=qty,
                stop_loss=sl_price,
                take_profit=tp_price,
                risk_usdt=risk_usdt,
                balance=balance,
            )
            return True
        else:
            self.logger.error(f"❌ FALLÓ la ejecución de la orden {signal}.")
            self.notifier.notify_order_failed(
                signal=signal,
                symbol=self.config.symbol,
                qty=qty,
                error_msg=f"Exchange rechazó la orden tras {self._MAX_RETRIES if hasattr(self, '_MAX_RETRIES') else 5} intentos",
            )
            return False
