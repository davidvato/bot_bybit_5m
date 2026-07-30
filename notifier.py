"""
=============================================================================
notifier.py - Módulo de Notificaciones por Telegram
=============================================================================
Envía alertas del bot de trading al chat de Telegram configurado.

Usa la Bot API de Telegram directamente con `requests` (sin librerías extra).
Si no hay token/chat_id configurados, todas las llamadas son silenciosas (no-op).

Tipos de notificaciones implementadas:
  - 🤖 Bot iniciado / detenido
  - 🟢 Orden LONG ejecutada
  - 🔴 Orden SHORT ejecutada
  - ✅ Take Profit alcanzado (señal contraria detectada)
  - 🛑 Stop Loss tocado (señal contraria detectada)
  - ❌ Orden fallida (error del exchange)
  - ⛔ Drawdown diario alcanzado
  - 🌐 Error crítico en el bucle principal
=============================================================================
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Cliente de notificaciones Telegram.
    Todas las operaciones son silenciosas si el token o chat_id no están configurados.
    Los errores de red nunca detienen el bot (fail-safe).
    """

    _API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    _TIMEOUT  = 15  # segundos

    def __init__(self, token: str, chat_id: str) -> None:
        self._token   = token.strip() if token else ""
        self._chat_id = chat_id.strip() if chat_id else ""
        self._enabled = bool(self._token and self._chat_id)

        # Proxy opcional: define TELEGRAM_PROXY en .env para superar bloqueos
        # Ejemplos: http://user:pass@host:port  |  socks5://host:port
        proxy_url = os.getenv("TELEGRAM_PROXY", "").strip()
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        if self._enabled:
            proxy_info = f" | Proxy: {proxy_url}" if proxy_url else ""
            logger.info(
                f"📲 Telegram habilitado | Chat ID: {self._chat_id}{proxy_info}"
            )
        else:
            logger.info(
                "📵 Telegram deshabilitado (TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados)"
            )

    # =========================================================================
    # MÉTODO CENTRAL DE ENVÍO
    # =========================================================================

    def send(self, message: str) -> bool:
        """
        Envía un mensaje de texto al chat configurado.
        Usa parse_mode HTML para formato enriquecido.

        Args:
            message: Texto del mensaje. Soporta HTML básico (<b>, <i>, <code>).

        Returns:
            bool: True si el mensaje fue enviado, False en caso de error o si está deshabilitado.
        """
        if not self._enabled:
            return False

        url = self._API_URL.format(token=self._token)
        payload = {
            "chat_id":    self._chat_id,
            "text":       message,
            "parse_mode": "HTML",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=self._TIMEOUT,
                proxies=self._proxies,
            )
            if response.status_code == 200:
                return True
            else:
                logger.warning(
                    f"⚠️  Telegram: respuesta inesperada {response.status_code}: {response.text[:200]}"
                )
                return False
        except requests.exceptions.Timeout:
            logger.warning(
                "⚠️  Telegram: timeout al enviar notificacion. "
                "Verifica tu conexion o configura TELEGRAM_PROXY en .env"
            )
            return False
        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️  Telegram: error de red al enviar notificacion: {e}")
            return False

    # =========================================================================
    # NOTIFICACIONES ESPECÍFICAS
    # =========================================================================

    def _now_utc(self) -> str:
        """Devuelve la hora UTC actual formateada."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def notify_bot_started(
        self,
        symbol: str,
        timeframe: str,
        leverage: int,
        balance: float,
        testnet: bool,
    ) -> None:
        """Notifica que el bot fue iniciado exitosamente."""
        mode = "⚠️ TESTNET" if testnet else "🔴 MAINNET REAL"
        msg = (
            f"🤖 <b>BOT INICIADO</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {self._now_utc()}\n"
            f"💱 Par:          <code>{symbol}</code>\n"
            f"⏱️  Temporalidad: <code>{timeframe}m</code>\n"
            f"⚡ Apalancamiento: <code>{leverage}x</code>\n"
            f"💰 Balance:      <code>{balance:.2f} USDT</code>\n"
            f"🌐 Modo:         {mode}"
        )
        self.send(msg)

    def notify_bot_stopped(self, cycles: int, orders: int, duration_str: str) -> None:
        """Notifica que el bot fue detenido."""
        msg = (
            f"🔴 <b>BOT DETENIDO</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {self._now_utc()}\n"
            f"🔄 Ciclos ejecutados: <code>{cycles}</code>\n"
            f"📋 Órdenes enviadas:  <code>{orders}</code>\n"
            f"⏱️  Duración sesión:   <code>{duration_str}</code>"
        )
        self.send(msg)

    def notify_order_executed(
        self,
        signal: str,
        symbol: str,
        entry_price: float,
        qty: float,
        stop_loss: float,
        take_profit: float,
        risk_usdt: float,
        balance: float,
    ) -> None:
        """Notifica una orden ejecutada exitosamente (LONG o SHORT)."""
        emoji  = "🟢" if signal == "LONG" else "🔴"
        direction = "LONG ↑" if signal == "LONG" else "SHORT ↓"
        rr     = abs(take_profit - entry_price) / abs(entry_price - stop_loss)
        msg = (
            f"{emoji} <b>ORDEN {direction} EJECUTADA</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {self._now_utc()}\n"
            f"💱 Par:     <code>{symbol}</code>\n"
            f"💲 Entrada: <code>{entry_price:,.2f} USDT</code>\n"
            f"📦 Qty:     <code>{qty}</code>\n"
            f"🛑 SL:      <code>{stop_loss:,.2f} USDT</code>\n"
            f"✅ TP:      <code>{take_profit:,.2f} USDT</code>\n"
            f"⚖️  R/R:     <code>1:{rr:.1f}</code>\n"
            f"⚠️  Riesgo:  <code>{risk_usdt:.2f} USDT ({risk_usdt/balance*100:.1f}% balance)</code>"
        )
        self.send(msg)

    def notify_order_failed(
        self,
        signal: str,
        symbol: str,
        qty: float,
        error_msg: str,
    ) -> None:
        """Notifica que una orden falló al enviarse al exchange."""
        emoji = "🟢" if signal == "LONG" else "🔴"
        msg = (
            f"❌ <b>ORDEN FALLIDA — {signal}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {self._now_utc()}\n"
            f"💱 Par:   <code>{symbol}</code>\n"
            f"{emoji} Dir:  <code>{signal}</code>\n"
            f"📦 Qty:   <code>{qty}</code>\n"
            f"🔎 Error: <code>{error_msg[:300]}</code>"
        )
        self.send(msg)

    def notify_position_closed(
        self,
        reason: str,
        symbol: str,
        side: str,
        pnl: Optional[float] = None,
    ) -> None:
        """
        Notifica el cierre de una posición existente.

        Args:
            reason: 'CONTRARIA' (señal opuesta detectada) o 'MANUAL'.
            symbol: Par de trading.
            side: 'Buy' (Long cerrado) o 'Sell' (Short cerrado).
            pnl: PnL estimado en USDT si está disponible.
        """
        dir_str  = "LONG" if side == "Buy" else "SHORT"
        pnl_str  = f"\n💵 PnL est.: <code>{pnl:+.2f} USDT</code>" if pnl is not None else ""
        msg = (
            f"🔄 <b>POSICIÓN {dir_str} CERRADA</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {self._now_utc()}\n"
            f"💱 Par:    <code>{symbol}</code>\n"
            f"📌 Motivo: <code>Señal contraria detectada</code>"
            f"{pnl_str}"
        )
        self.send(msg)

    def notify_daily_drawdown(
        self,
        accumulated_loss: float,
        limit: float,
        risk_attempted: float,
    ) -> None:
        """Notifica que se alcanzó el límite de pérdida diaria."""
        msg = (
            f"⛔ <b>DRAWDOWN DIARIO ALCANZADO</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {self._now_utc()}\n"
            f"📉 Pérdida acumulada: <code>-{accumulated_loss:.2f} USDT</code>\n"
            f"🚫 Límite diario:     <code>-{limit:.2f} USDT</code>\n"
            f"💢 Riesgo intentado:  <code>{risk_attempted:.2f} USDT</code>\n"
            f"⏸️  <b>No se abrirán nuevas posiciones hoy.</b>"
        )
        self.send(msg)

    def notify_critical_error(self, error_msg: str) -> None:
        """Notifica un error crítico inesperado en el bucle principal."""
        msg = (
            f"🚨 <b>ERROR CRÍTICO EN EL BOT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {self._now_utc()}\n"
            f"🔎 <code>{error_msg[:500]}</code>\n\n"
            f"⚠️ El bot intentará recuperarse automáticamente."
        )
        self.send(msg)
