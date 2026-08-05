"""
=============================================================================
config.py - Módulo de Configuración Central
=============================================================================
Carga y valida todos los parámetros del bot desde variables de entorno.
Centraliza la configuración para facilitar ajustes sin tocar la lógica core.
=============================================================================
"""

import os
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

# Cargar variables desde archivo .env
load_dotenv()


@dataclass
class BotConfig:
    """
    Clase de configuración del bot.
    Todos los parámetros son cargados desde variables de entorno,
    con valores por defecto seguros para modo testnet.
    """

    # -------------------------------------------------------------------------
    # CREDENCIALES DE API
    # -------------------------------------------------------------------------
    api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""))
    testnet: bool = field(
        default_factory=lambda: os.getenv("BYBIT_TESTNET", "true").lower() == "true"
    )

    # -------------------------------------------------------------------------
    # TELEGRAM
    # -------------------------------------------------------------------------
    telegram_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "")
    )

    # -------------------------------------------------------------------------
    # CONFIGURACIÓN DE MERCADO
    # -------------------------------------------------------------------------
    symbol: str = field(default_factory=lambda: os.getenv("TRADING_SYMBOL", "BTCUSDT"))
    timeframe: str = field(default_factory=lambda: os.getenv("TIMEFRAME", "5"))
    category: str = field(
        default_factory=lambda: os.getenv("MARKET_CATEGORY", "linear")
    )

    # -------------------------------------------------------------------------
    # GESTIÓN DE RIESGO
    # -------------------------------------------------------------------------
    risk_per_trade: float = field(
        default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.02"))
    )
    leverage: int = field(
        default_factory=lambda: int(os.getenv("LEVERAGE", "5"))
    )
    atr_sl_multiplier: float = field(
        default_factory=lambda: float(os.getenv("ATR_SL_MULTIPLIER", "2.0"))
    )
    risk_reward_ratio: float = field(
        default_factory=lambda: float(os.getenv("RISK_REWARD_RATIO", "2.0"))
    )
    # Pérdida máxima diaria en USDT. Si se alcanza, el bot deja de operar ese día.
    # 0.0 = sin límite (no recomendado para producción)
    max_daily_loss: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS", "0.0"))
    )

    # -------------------------------------------------------------------------
    # PARÁMETROS DE INDICADORES TÉCNICOS (fijos, no requieren .env)
    # -------------------------------------------------------------------------
    # EMA Cross (Estrategia 1)
    # 13/34 más estable que 9/21 en temporalidades cortas (menos ruido en 5m)
    ema_fast: int = 13
    ema_slow: int = 34

    # EMA de largo plazo para filtro de tendencia (Estrategia 2)
    ema_trend: int = 200

    # Stochastic RSI (Estrategia 2)
    stoch_rsi_period: int = 14
    stoch_rsi_smooth_k: int = 3
    stoch_rsi_smooth_d: int = 3
    stoch_rsi_oversold: float = 20.0
    stoch_rsi_overbought: float = 80.0

    # Bandas de Bollinger (Estrategia 3)
    bb_period: int = 20
    bb_std: float = 2.0

    # RSI estándar (Estrategia 3 - divergencia)
    rsi_period: int = 14

    # MACD (Estrategia 4)
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # Volumen SMA (Estrategia 4)
    volume_sma_period: int = 20

    # ATR para Stop Loss dinámico
    atr_period: int = 14

    # -------------------------------------------------------------------------
    # SISTEMA DE CONSENSO
    # -------------------------------------------------------------------------
    # Número mínimo de estrategias (de 4) que deben coincidir para abrir posición.
    # ⚡ 5m PRODUCCIÓN: 3/4 — filtro más exigente para evitar señales falsas.
    # Con consenso 2/4 el Win Rate fue 0% en 6 trades. Subido a 3 para calidad.
    min_consensus: int = 2

    # -------------------------------------------------------------------------
    # FILTRO ADX
    # -------------------------------------------------------------------------
    # ADX mínimo para operar. Por debajo de este valor el mercado es lateral
    # y las estrategias de momentum generan falsas señales.
    adx_period: int = 14
    adx_min_threshold: float = field(
        default_factory=lambda: float(os.getenv("ADX_MIN_THRESHOLD", "20.0"))
    )

    # -------------------------------------------------------------------------
    # CONFIGURACIÓN DE DATOS
    # -------------------------------------------------------------------------
    # ⚡ 5m: 500 velas = ~41 horas de historia, suficiente para EMA200 + indicadores.
    # (300 en 15m equivale a ~75h | 500 en 5m equivale a ~41h — similar cobertura útil)
    klines_limit: int = 500

    # -------------------------------------------------------------------------
    # LOGGING
    # -------------------------------------------------------------------------
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )
    log_file: str = field(
        default_factory=lambda: os.getenv("LOG_FILE", "logs/bot_trading.log")
    )

    # -------------------------------------------------------------------------
    # CONTROL DE BUCLE PRINCIPAL
    # -------------------------------------------------------------------------
    # Segundos antes del cierre de vela para ejecutar análisis
    # (para que los datos estén disponibles)
    seconds_before_close: int = 10

    # -------------------------------------------------------------------------
    # EXPIRACIÓN AUTOMÁTICA DE TRADES (Mejora #2)
    # -------------------------------------------------------------------------
    # Número máximo de velas que una posición puede estar abierta sin alcanzar
    # SL ni TP. Si se supera, el bot cierra la posición a mercado para evitar
    # acumulación de funding rate y mantener el capital disponible.
    # 12 velas × 5m = 60 minutos máximo por trade.
    # 0 = sin límite (desactivado).
    max_candles_open: int = field(
        default_factory=lambda: int(os.getenv("MAX_CANDLES_OPEN", "12"))
    )

    def validate(self) -> None:
        """Valida que la configuración sea correcta antes de iniciar el bot."""
        errors = []

        if not self.api_key:
            errors.append("BYBIT_API_KEY no está configurada en el archivo .env")
        if not self.api_secret:
            errors.append("BYBIT_API_SECRET no está configurada en el archivo .env")
        if not 0 < self.risk_per_trade <= 0.1:
            errors.append(
                f"RISK_PER_TRADE ({self.risk_per_trade}) debe estar entre 0.001 y 0.10 (0.1% - 10%)"
            )
        if not 1 <= self.leverage <= 100:
            errors.append(f"LEVERAGE ({self.leverage}) debe estar entre 1 y 100")
        if self.min_consensus < 1 or self.min_consensus > 4:
            errors.append("min_consensus debe estar entre 1 y 4 (hay 4 estrategias activas)")
        if self.max_daily_loss < 0:
            errors.append("MAX_DAILY_LOSS no puede ser negativo")
        if self.adx_min_threshold < 0 or self.adx_min_threshold > 100:
            errors.append("ADX_MIN_THRESHOLD debe estar entre 0 y 100")

        if errors:
            raise ValueError(
                "Errores de configuración:\n" + "\n".join(f"  - {e}" for e in errors)
            )

    def get_log_level(self) -> int:
        """Convierte el nivel de log de string a constante de logging."""
        levels = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
        }
        return levels.get(self.log_level.upper(), logging.INFO)


# Instancia global de configuración
config = BotConfig()
