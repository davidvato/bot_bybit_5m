"""
=============================================================================
strategies.py - Motor de Cálculo de Indicadores y Sistema de Consenso
=============================================================================
Usa la librería 'ta' (compatible con Python 3.14+) en lugar de pandas-ta.

ESTRATEGIAS IMPLEMENTADAS:
  1. EMA Cross Adaptativo (EMA 9 vs EMA 21)          — Momentum de corto plazo
  2. Filtro de Tendencia EMA 200 + StochRSI (14,3,3) — Trend-following macro
  3. Ruptura de Bandas de Bollinger (BB 20,2) + RSI  — Reversión a la media
  4. Confirmación de Momento y Volumen (MACD + Vol)  — Confirmación de impulso

NOTA SOBRE EL DISEÑO DEL CONSENSO:
  Las estrategias 2 (trend) y 3 (reversión) son antagónicas por naturaleza.
  Esto es INTENCIONAL: la Estrategia 3 actúa como filtro de sobreextensión.
  Si el mercado está en tendencia fuerte con precio sobre BB superior (S3=SHORT)
  pero EMA200+StochRSI dicen LONG, la señal contradictoria de S3 fuerza un
  consenso más exigente (3/4), evitando entradas en zonas de sobrecompra.
  El resultado es un sistema que combina momentum con gestión de sobreextensión.

SISTEMA DE CONSENSO:
  - LONG:  ≥3 de 4 estrategias generan señal de compra
  - SHORT: ≥3 de 4 estrategias generan señal de venta
  - HOLD:  <3 estrategias coinciden → no operar
=============================================================================
"""

import logging
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

# Librería 'ta': compatible con Python 3.14+, sin dependencia de numba
import ta
from ta.trend import EMAIndicator, MACD, SMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

from config import BotConfig
from logger import setup_logger


# Tipo de señal posible
SignalType = Literal["LONG", "SHORT", "HOLD"]


@dataclass
class SignalResult:
    """
    Resultado del análisis de una estrategia individual.

    Attributes:
        name:       Nombre descriptivo de la estrategia.
        signal:     'LONG', 'SHORT' o 'HOLD'.
        reason:     Explicación del por qué se generó la señal.
        confidence: Valor 0.0–1.0 que indica la fuerza de la señal.
    """
    name: str
    signal: SignalType
    reason: str
    confidence: float = 1.0


@dataclass
class ConsensusResult:
    """
    Resultado del sistema de consenso agregado.

    Attributes:
        final_signal:  Señal final validada ('LONG', 'SHORT' o 'HOLD').
        long_count:    Cuántas estrategias votaron LONG.
        short_count:   Cuántas estrategias votaron SHORT.
        hold_count:    Cuántas estrategias están en HOLD.
        signals:       Lista de resultados individuales por estrategia.
        atr:           Valor actual del ATR (para cálculo de SL/TP).
        current_price: Precio de cierre de la última vela.
    """
    final_signal: SignalType
    long_count: int
    short_count: int
    hold_count: int
    signals: list
    atr: float
    current_price: float


class IndicatorEngine:
    """
    Motor de cálculo de indicadores técnicos usando la librería 'ta'.
    Agrega todas las columnas de indicadores al DataFrame de klines.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.logger = setup_logger(__name__, config.log_file, config.get_log_level())

    def calculate_all(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        Calcula todos los indicadores técnicos sobre el DataFrame de klines
        y retorna una copia del DataFrame con las columnas de indicadores añadidas.

        Args:
            df: DataFrame con columnas OHLCV (open, high, low, close, volume).

        Returns:
            DataFrame enriquecido con indicadores, o None si hay error de datos.
        """
        min_candles = self.config.ema_trend + 10
        if df is None or len(df) < min_candles:
            self.logger.error(
                f"❌ Datos insuficientes: se tienen {len(df) if df is not None else 0} velas, "
                f"se necesitan al menos {min_candles}."
            )
            return None

        df = df.copy()
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]

        try:
            # -----------------------------------------------------------------
            # ESTRATEGIA 1: EMAs de Cruce (EMA 9 y EMA 21)
            # -----------------------------------------------------------------
            df[f"ema_{self.config.ema_fast}"] = EMAIndicator(
                close=close, window=self.config.ema_fast, fillna=False
            ).ema_indicator()

            df[f"ema_{self.config.ema_slow}"] = EMAIndicator(
                close=close, window=self.config.ema_slow, fillna=False
            ).ema_indicator()

            # -----------------------------------------------------------------
            # ESTRATEGIA 2: EMA de Largo Plazo (filtro macro) + StochRSI
            # -----------------------------------------------------------------
            df[f"ema_{self.config.ema_trend}"] = EMAIndicator(
                close=close, window=self.config.ema_trend, fillna=False
            ).ema_indicator()

            stoch_rsi = StochRSIIndicator(
                close=close,
                window=self.config.stoch_rsi_period,
                smooth1=self.config.stoch_rsi_smooth_k,
                smooth2=self.config.stoch_rsi_smooth_d,
                fillna=False,
            )
            df["stochrsi_k"] = stoch_rsi.stochrsi_k()
            df["stochrsi_d"] = stoch_rsi.stochrsi_d()

            # -----------------------------------------------------------------
            # ESTRATEGIA 3: Bandas de Bollinger (BB 20, 2σ) + RSI estándar
            # -----------------------------------------------------------------
            bb = BollingerBands(
                close=close,
                window=self.config.bb_period,
                window_dev=self.config.bb_std,
                fillna=False,
            )
            df["bb_lower"] = bb.bollinger_lband()
            df["bb_mid"]   = bb.bollinger_mavg()
            df["bb_upper"] = bb.bollinger_hband()

            df["rsi"] = RSIIndicator(
                close=close, window=self.config.rsi_period, fillna=False
            ).rsi()

            # -----------------------------------------------------------------
            # ESTRATEGIA 4: MACD (12,26,9) + Volumen SMA 20
            # -----------------------------------------------------------------
            macd_indicator = MACD(
                close=close,
                window_fast=self.config.macd_fast,
                window_slow=self.config.macd_slow,
                window_sign=self.config.macd_signal,
                fillna=False,
            )
            df["macd"]        = macd_indicator.macd()
            df["macd_signal"] = macd_indicator.macd_signal()
            df["macd_hist"]   = macd_indicator.macd_diff()  # histograma = MACD - Signal

            df["volume_sma"] = SMAIndicator(
                close=volume, window=self.config.volume_sma_period, fillna=False
            ).sma_indicator()

            # -----------------------------------------------------------------
            # ATR (14 periodos) — Stop Loss dinámico
            # -----------------------------------------------------------------
            df["atr"] = AverageTrueRange(
                high=high,
                low=low,
                close=close,
                window=self.config.atr_period,
                fillna=False,
            ).average_true_range()

            # -----------------------------------------------------------------
            # ADX (N1) — Filtro de régimen de mercado
            # ADX > adx_min_threshold = mercado en tendencia (apto para operar)
            # ADX < adx_min_threshold = mercado lateral (evitar señales falsas)
            # -----------------------------------------------------------------
            adx_indicator = ADXIndicator(
                high=high,
                low=low,
                close=close,
                window=self.config.adx_period,
                fillna=False,
            )
            df["adx"]     = adx_indicator.adx()
            df["adx_pos"] = adx_indicator.adx_pos()  # DI+ (dirección alcista)
            df["adx_neg"] = adx_indicator.adx_neg()  # DI- (dirección bajista)

            self.logger.debug("✅ Todos los indicadores calculados correctamente.")
            return df

        except Exception as e:
            self.logger.error(f"❌ Error calculando indicadores: {e}", exc_info=True)
            return None


class StrategyEngine:
    """
    Motor de evaluación de las 4 estrategias con sistema de consenso.
    Cada estrategia opera INDEPENDIENTEMENTE y retorna su propia señal.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.logger = setup_logger(__name__, config.log_file, config.get_log_level())

    # =========================================================================
    # ESTRATEGIA 1: CRUCE DE EMAs ADAPTATIVO
    # =========================================================================

    def strategy_ema_cross(self, df: pd.DataFrame) -> SignalResult:
        """
        Estrategia 1: Cruce de Medias Móviles Exponenciales (EMA 9 vs EMA 21).

        LÓGICA:
          LONG:  EMA_9 cruza AL ALZA  EMA_21
                 (EMA_9[-2] < EMA_21[-2] y EMA_9[-1] > EMA_21[-1])
          SHORT: EMA_9 cruza A LA BAJA EMA_21
                 (EMA_9[-2] > EMA_21[-2] y EMA_9[-1] < EMA_21[-1])
          HOLD:  Sin cruce confirmado en la última vela cerrada.
        """
        name = "EMA Cross (9/21)"
        fast_col = f"ema_{self.config.ema_fast}"
        slow_col = f"ema_{self.config.ema_slow}"

        try:
            fast_curr = df[fast_col].iloc[-1]
            fast_prev = df[fast_col].iloc[-2]
            slow_curr = df[slow_col].iloc[-1]
            slow_prev = df[slow_col].iloc[-2]

            if any(pd.isna([fast_curr, fast_prev, slow_curr, slow_prev])):
                return SignalResult(name=name, signal="HOLD", reason="Datos insuficientes (NaN)", confidence=0)

            # LONG: cruce alcista
            if fast_prev < slow_prev and fast_curr > slow_curr:
                spread_pct = (fast_curr - slow_curr) / slow_curr * 100
                return SignalResult(
                    name=name, signal="LONG",
                    reason=(
                        f"EMA{self.config.ema_fast} ({fast_curr:.2f}) cruzó ↑ "
                        f"EMA{self.config.ema_slow} ({slow_curr:.2f}) | "
                        f"Spread: +{spread_pct:.4f}%"
                    ),
                    confidence=min(1.0, spread_pct * 10),
                )

            # SHORT: cruce bajista
            if fast_prev > slow_prev and fast_curr < slow_curr:
                spread_pct = (slow_curr - fast_curr) / slow_curr * 100
                return SignalResult(
                    name=name, signal="SHORT",
                    reason=(
                        f"EMA{self.config.ema_fast} ({fast_curr:.2f}) cruzó ↓ "
                        f"EMA{self.config.ema_slow} ({slow_curr:.2f}) | "
                        f"Spread: -{spread_pct:.4f}%"
                    ),
                    confidence=min(1.0, spread_pct * 10),
                )

            # Sin cruce
            diff_pct = (fast_curr - slow_curr) / slow_curr * 100
            return SignalResult(
                name=name, signal="HOLD",
                reason=f"Sin cruce. EMA diff: {diff_pct:+.4f}%",
            )

        except Exception as e:
            self.logger.error(f"❌ Error en {name}: {e}")
            return SignalResult(name=name, signal="HOLD", reason=f"Error: {e}", confidence=0)

    # =========================================================================
    # ESTRATEGIA 2: FILTRO DE TENDENCIA EMA 200 + STOCHASTIC RSI
    # =========================================================================

    def strategy_trend_stochrsi(self, df: pd.DataFrame) -> SignalResult:
        """
        Estrategia 2: Filtro de Tendencia EMA 200 + Stochastic RSI (14, 3, 3).

        LÓGICA:
          LONG:  Precio > EMA_200 (macro-tendencia alcista)
                 AND StochRSI_K cruza AL ALZA StochRSI_D
                 AND StochRSI_K anterior estaba < 20 (sobreventa)

          SHORT: Precio < EMA_200 (macro-tendencia bajista)
                 AND StochRSI_K cruza A LA BAJA StochRSI_D
                 AND StochRSI_K anterior estaba > 80 (sobrecompra)

        La EMA 200 filtra entradas en contra de la tendencia principal.
        El StochRSI identifica el mejor punto de entrada dentro de la tendencia.
        """
        name = "Trend EMA200 + StochRSI"
        trend_col = f"ema_{self.config.ema_trend}"

        try:
            price      = df["close"].iloc[-1]
            ema_trend  = df[trend_col].iloc[-1]
            k_curr     = df["stochrsi_k"].iloc[-1]
            k_prev     = df["stochrsi_k"].iloc[-2]
            d_curr     = df["stochrsi_d"].iloc[-1]
            d_prev     = df["stochrsi_d"].iloc[-2]

            if any(pd.isna([ema_trend, k_curr, k_prev, d_curr, d_prev])):
                return SignalResult(name=name, signal="HOLD", reason="Datos insuficientes (NaN)", confidence=0)

            above_ema  = price > ema_trend
            below_ema  = price < ema_trend
            k_up       = k_prev < d_prev and k_curr > d_curr   # cruce alcista
            k_down     = k_prev > d_prev and k_curr < d_curr   # cruce bajista

            # LONG: tendencia alcista + entrada desde sobreventa
            if above_ema and k_up and k_prev < self.config.stoch_rsi_oversold:
                return SignalResult(
                    name=name, signal="LONG",
                    reason=(
                        f"Precio ({price:.2f}) > EMA200 ({ema_trend:.2f}) ✓ | "
                        f"StochRSI K ({k_curr:.1f}) cruzó ↑ D ({d_curr:.1f}) desde sobreventa"
                    ),
                )

            # SHORT: tendencia bajista + entrada desde sobrecompra
            if below_ema and k_down and k_prev > self.config.stoch_rsi_overbought:
                return SignalResult(
                    name=name, signal="SHORT",
                    reason=(
                        f"Precio ({price:.2f}) < EMA200 ({ema_trend:.2f}) ✓ | "
                        f"StochRSI K ({k_curr:.1f}) cruzó ↓ D ({d_curr:.1f}) desde sobrecompra"
                    ),
                )

            trend_str = "ALCISTA ↑" if above_ema else "BAJISTA ↓"
            return SignalResult(
                name=name, signal="HOLD",
                reason=(
                    f"Sin señal. Tendencia macro: {trend_str} | "
                    f"StochRSI K={k_curr:.1f}, D={d_curr:.1f}"
                ),
            )

        except Exception as e:
            self.logger.error(f"❌ Error en {name}: {e}")
            return SignalResult(name=name, signal="HOLD", reason=f"Error: {e}", confidence=0)

    # =========================================================================
    # ESTRATEGIA 3: REVERSIÓN EN BANDAS DE BOLLINGER
    # =========================================================================

    def strategy_bollinger_reversal(self, df: pd.DataFrame) -> SignalResult:
        """
        Estrategia 3: Patrón de Reversión en Bandas de Bollinger (BB 20, 2σ).

        LÓGICA:
          LONG:  Vela anterior cerró por DEBAJO de la banda inferior (BB_low)
                 AND vela actual cierra dentro de las bandas (reversión confirmada)
                 Confirmación adicional: RSI < 35 (condición de sobreventa)

          SHORT: Vela anterior cerró por ENCIMA de la banda superior (BB_up)
                 AND vela actual cierra dentro de las bandas (reversión confirmada)
                 Confirmación adicional: RSI > 65 (condición de sobrecompra)

        Este patrón de "toque y rebote" identifica reversiones a la media
        (mean reversion), efectivo en rangos y consolidaciones.
        """
        name = "Bollinger Bands Reversal"

        try:
            close_curr    = df["close"].iloc[-1]
            close_prev    = df["close"].iloc[-2]
            bb_low_curr   = df["bb_lower"].iloc[-1]
            bb_up_curr    = df["bb_upper"].iloc[-1]
            bb_low_prev   = df["bb_lower"].iloc[-2]
            bb_up_prev    = df["bb_upper"].iloc[-2]
            rsi_curr      = df["rsi"].iloc[-1]

            if any(pd.isna([close_curr, close_prev, bb_low_curr, bb_up_curr,
                             bb_low_prev, bb_up_prev, rsi_curr])):
                return SignalResult(name=name, signal="HOLD", reason="Datos insuficientes (NaN)", confidence=0)

            inside_curr      = bb_low_curr < close_curr < bb_up_curr
            prev_below_lower = close_prev < bb_low_prev
            prev_above_upper = close_prev > bb_up_prev
            rsi_oversold     = rsi_curr < 35
            rsi_overbought   = rsi_curr > 65

            # LONG: vela previa tocó / cerró bajo BB inferior, vela actual regresó dentro
            if prev_below_lower and inside_curr:
                confidence = 0.7 + (0.3 if rsi_oversold else 0.0)
                return SignalResult(
                    name=name, signal="LONG",
                    reason=(
                        f"Reversión alcista ↑ desde BB inferior | "
                        f"Close_prev={close_prev:.2f} < BB_low_prev={bb_low_prev:.2f} | "
                        f"Close_curr={close_curr:.2f} (dentro) | "
                        f"RSI={rsi_curr:.1f} {'✓ sobreventa' if rsi_oversold else ''}"
                    ),
                    confidence=confidence,
                )

            # SHORT: vela previa tocó / cerró sobre BB superior, vela actual regresó dentro
            if prev_above_upper and inside_curr:
                confidence = 0.7 + (0.3 if rsi_overbought else 0.0)
                return SignalResult(
                    name=name, signal="SHORT",
                    reason=(
                        f"Reversión bajista ↓ desde BB superior | "
                        f"Close_prev={close_prev:.2f} > BB_up_prev={bb_up_prev:.2f} | "
                        f"Close_curr={close_curr:.2f} (dentro) | "
                        f"RSI={rsi_curr:.1f} {'✓ sobrecompra' if rsi_overbought else ''}"
                    ),
                    confidence=confidence,
                )

            dist_low_pct  = (close_curr - bb_low_curr) / bb_low_curr * 100
            dist_high_pct = (bb_up_curr - close_curr) / close_curr * 100
            return SignalResult(
                name=name, signal="HOLD",
                reason=(
                    f"Sin reversión. Precio dentro de bandas | "
                    f"↓ a BB_low: {dist_low_pct:.2f}% | ↑ a BB_up: {dist_high_pct:.2f}%"
                ),
            )

        except Exception as e:
            self.logger.error(f"❌ Error en {name}: {e}")
            return SignalResult(name=name, signal="HOLD", reason=f"Error: {e}", confidence=0)

    # =========================================================================
    # ESTRATEGIA 4: CONFIRMACIÓN DE MOMENTO Y VOLUMEN (MACD + VOL SMA)
    # =========================================================================

    def strategy_macd_volume(self, df: pd.DataFrame) -> SignalResult:
        """
        Estrategia 4: MACD (12,26,9) + Confirmación de Volumen (SMA 20).

        LÓGICA:
          LONG:  Histograma MACD pasa de negativo a positivo
                 O línea MACD cruza al alza la línea de señal
                 AND volumen actual > SMA_20 del volumen

          SHORT: Histograma MACD pasa de positivo a negativo
                 O línea MACD cruza a la baja la línea de señal
                 AND volumen actual > SMA_20 del volumen

        El filtro de volumen es OBLIGATORIO para confirmar que el movimiento
        tiene participación real del mercado (evita falsos breakouts).
        """
        name = "MACD + Volume Confirmation"

        try:
            macd_curr   = df["macd"].iloc[-1]
            macd_prev   = df["macd"].iloc[-2]
            sig_curr    = df["macd_signal"].iloc[-1]
            sig_prev    = df["macd_signal"].iloc[-2]
            hist_curr   = df["macd_hist"].iloc[-1]
            hist_prev   = df["macd_hist"].iloc[-2]
            vol_curr    = df["volume"].iloc[-1]
            vol_sma     = df["volume_sma"].iloc[-1]

            if any(pd.isna([macd_curr, macd_prev, sig_curr, sig_prev,
                             hist_curr, hist_prev, vol_curr, vol_sma])):
                return SignalResult(name=name, signal="HOLD", reason="Datos insuficientes (NaN)", confidence=0)

            high_volume   = vol_curr > vol_sma
            vol_ratio     = vol_curr / vol_sma if vol_sma > 0 else 1.0

            # Cruce alcista del MACD / histograma positivo
            macd_cross_up   = macd_prev < sig_prev and macd_curr > sig_curr
            hist_pos_flip   = hist_prev < 0 and hist_curr >= 0
            bullish_macd    = macd_cross_up or hist_pos_flip

            # Cruce bajista del MACD / histograma negativo
            macd_cross_down = macd_prev > sig_prev and macd_curr < sig_curr
            hist_neg_flip   = hist_prev > 0 and hist_curr <= 0
            bearish_macd    = macd_cross_down or hist_neg_flip

            # LONG: momentum alcista + volumen confirmado
            if bullish_macd and high_volume:
                trigger = "cruce MACD↑Signal" if macd_cross_up else "histograma neg→pos"
                return SignalResult(
                    name=name, signal="LONG",
                    reason=(
                        f"Momentum alcista ({trigger}) | "
                        f"Vol {vol_ratio:.2f}x sobre la media "
                        f"(Vol={vol_curr:.0f}, SMA={vol_sma:.0f})"
                    ),
                    confidence=min(1.0, vol_ratio / 2),
                )

            # SHORT: momentum bajista + volumen confirmado
            if bearish_macd and high_volume:
                trigger = "cruce MACD↓Signal" if macd_cross_down else "histograma pos→neg"
                return SignalResult(
                    name=name, signal="SHORT",
                    reason=(
                        f"Momentum bajista ({trigger}) | "
                        f"Vol {vol_ratio:.2f}x sobre la media "
                        f"(Vol={vol_curr:.0f}, SMA={vol_sma:.0f})"
                    ),
                    confidence=min(1.0, vol_ratio / 2),
                )

            # Sin señal
            momentum_str = "ALCISTA" if hist_curr > 0 else "BAJISTA"
            vol_str      = f"ALTO ✓ ({vol_ratio:.2f}x)" if high_volume else f"BAJO ({vol_ratio:.2f}x)"
            return SignalResult(
                name=name, signal="HOLD",
                reason=f"Sin cruce MACD. Momentum: {momentum_str} | Volumen: {vol_str}",
            )

        except Exception as e:
            self.logger.error(f"❌ Error en {name}: {e}")
            return SignalResult(name=name, signal="HOLD", reason=f"Error: {e}", confidence=0)

    # =========================================================================
    # SISTEMA DE CONSENSO — NÚCLEO DEL BOT
    # =========================================================================

    def evaluate_consensus(self, df: pd.DataFrame) -> ConsensusResult:
        """
        Evalúa las 4 estrategias de forma independiente y agrega sus señales
        usando el sistema de consenso (requiere mínimo min_consensus votos).

        Retorna ConsensusResult con:
          - final_signal: la señal validada (LONG, SHORT o HOLD)
          - Conteos por dirección y lista de resultados individuales
          - ATR y precio actual para la gestión de riesgo posterior
        """
        self.logger.info("=" * 62)
        self.logger.info("  🧠 EVALUANDO SISTEMA DE CONSENSO — 4 ESTRATEGIAS")
        self.logger.info("=" * 62)

        # --- N1: FILTRO ADX — Régimen de mercado ---
        # Si ADX < umbral mínimo, el mercado está en rango lateral.
        # Las estrategias de momentum generan falsas señales en rangos.
        # Forzamos HOLD para evitar entradas de baja probabilidad.
        current_price = float(df["close"].iloc[-1])
        atr_val = df["atr"].iloc[-1]
        atr = float(atr_val) if not pd.isna(atr_val) else 0.0

        adx_val = df["adx"].iloc[-1] if "adx" in df.columns else float("nan")
        if not pd.isna(adx_val):
            adx_pos = float(df["adx_pos"].iloc[-1]) if not pd.isna(df["adx_pos"].iloc[-1]) else 0.0
            adx_neg = float(df["adx_neg"].iloc[-1]) if not pd.isna(df["adx_neg"].iloc[-1]) else 0.0
            adx_direction = "alcista ↑" if adx_pos > adx_neg else "bajista ↓"
            self.logger.info(
                f"  📶 ADX={adx_val:.1f} (DI+={adx_pos:.1f} / DI-={adx_neg:.1f}) "
                f"| Dirección: {adx_direction} "
                f"| Umbral mínimo: {self.config.adx_min_threshold:.0f}"
            )
            if adx_val < self.config.adx_min_threshold:
                self.logger.info(
                    f"  🟡 MERCADO LATERAL DETECTADO — ADX={adx_val:.1f} < {self.config.adx_min_threshold:.0f}. "
                    f"Forzando HOLD para evitar señales de baja calidad."
                )
                self.logger.info("=" * 62)
                return ConsensusResult(
                    final_signal="HOLD",
                    long_count=0, short_count=0, hold_count=4,
                    signals=[],
                    atr=atr,
                    current_price=current_price,
                )
        else:
            self.logger.warning("  ⚠️  ADX no disponible (NaN). Continuando sin filtro de régimen.")

        # Ejecutar las 4 estrategias de forma independiente.
        # NOTA: S1 (EMA Cross) y S2 (EMA200+StochRSI) siguen tendencia;
        #       S3 (Bollinger) actúa como filtro de sobreextensión (reversión);
        #       S4 (MACD+Vol) confirma el impulso con volumen real.
        # El consenso exige 3/4 votos para filtrar señales de baja calidad.
        signals = [
            self.strategy_ema_cross(df),              # Estrategia 1 — EMA 9/21 momentum
            self.strategy_trend_stochrsi(df),         # Estrategia 2 — EMA200 + StochRSI
            self.strategy_bollinger_reversal(df),     # Estrategia 3 — BB Reversal (filtro)
            self.strategy_macd_volume(df),            # Estrategia 4 — MACD + Volumen
        ]

        # Contabilizar votos
        long_count  = sum(1 for s in signals if s.signal == "LONG")
        short_count = sum(1 for s in signals if s.signal == "SHORT")
        hold_count  = sum(1 for s in signals if s.signal == "HOLD")

        # Loguear cada resultado individual
        icons = {"LONG": "🟢", "SHORT": "🔴", "HOLD": "⚪"}
        for i, s in enumerate(signals, start=1):
            icon = icons.get(s.signal, "❓")
            self.logger.info(f"  [{i}] {icon} {s.signal:5s} | {s.name}")
            self.logger.info(f"        → {s.reason}")

        self.logger.info(
            f"\n  📊 RESULTADO → 🟢 LONG: {long_count}  "
            f"🔴 SHORT: {short_count}  ⚪ HOLD: {hold_count}  "
            f"(mínimo requerido: {self.config.min_consensus}/4)"
        )

        # --- Determinar señal final por consenso ---
        final_signal: SignalType = "HOLD"
        min_votes = self.config.min_consensus

        if long_count >= min_votes and long_count > short_count:
            final_signal = "LONG"
            self.logger.info(
                f"\n  ✅ CONSENSO ALCANZADO → LONG "
                f"({long_count}/{len(signals)} estrategias)"
            )
        elif short_count >= min_votes and short_count > long_count:
            final_signal = "SHORT"
            self.logger.info(
                f"\n  ✅ CONSENSO ALCANZADO → SHORT "
                f"({short_count}/{len(signals)} estrategias)"
            )
        else:
            self.logger.info(
                f"\n  ⏸️  SIN CONSENSO — HOLD. "
                f"Mínimo requerido: {min_votes}/4"
            )

        self.logger.info("=" * 62)

        return ConsensusResult(
            final_signal=final_signal,
            long_count=long_count,
            short_count=short_count,
            hold_count=hold_count,
            signals=signals,
            atr=atr,
            current_price=current_price,
        )
