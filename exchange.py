"""
=============================================================================
exchange.py - Módulo de Conexión y Autenticación con Bybit API v5
=============================================================================
Maneja toda la comunicación con la API de Bybit:
  - Autenticación y creación del cliente pybit
  - Reconexión automática ante fallos de red
  - Control de Rate Limits y corrección automática de timestamp
  - Obtención de datos de mercado (Klines)
  - Consulta de balance y posiciones abiertas
=============================================================================
"""

import time
import logging
from typing import Optional

import pandas as pd
from pybit.unified_trading import HTTP

from config import BotConfig
from logger import setup_logger


class ExchangeClient:
    """
    Cliente de exchange que encapsula todas las llamadas a la API de Bybit.
    Implementa reconexión automática y manejo robusto de errores.
    """

    # Pausa entre reintentos (segundos, con backoff exponencial)
    _RETRY_BASE_DELAY = 2.0
    _MAX_RETRIES = 5

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.logger = setup_logger(
            __name__, config.log_file, config.get_log_level()
        )
        self.session: Optional[HTTP] = None
        self._time_offset_ms: int = 0  # offset entre reloj local y servidor Bybit
        self._connect()

    # =========================================================================
    # SECCIÓN 1: CONEXIÓN Y AUTENTICACIÓN
    # =========================================================================

    def _sync_time(self) -> None:
        """
        Mide el offset entre el reloj local y el servidor de Bybit,
        luego parchea pybit._helpers.generate_timestamp() para que
        use el tiempo CORREGIDO en cada firma de request.

        generate_timestamp() es la funcion que pybit llama en CADA request
        autenticado para generar el X-BAPI-TIMESTAMP del header.
        Parcheandola a nivel de modulo, todos los futuros requests usaran
        el timestamp correcto sin necesidad de sincronizar el SO.

        Resolucion definitiva de ErrCode 10002.
        """
        try:
            import pybit._helpers as _pybit_helpers

            local_ms  = int(time.time() * 1000)
            response  = self.session.get_server_time()
            server_ms = int(response["result"]["timeNano"]) // 1_000_000
            self._time_offset_ms = server_ms - local_ms

            if abs(self._time_offset_ms) > 500:
                self.logger.warning(
                    f"Reloj local desincronizado {self._time_offset_ms:+d}ms "
                    f"respecto a Bybit. Parcheando pybit._helpers.generate_timestamp..."
                )
                offset_ms = self._time_offset_ms

                # Monkey-patch: reemplazar generate_timestamp en el modulo de pybit
                # para que todos los requests firmados usen el tiempo corregido
                def generate_timestamp_corrected():
                    return int(time.time() * 1000) + offset_ms

                _pybit_helpers.generate_timestamp = generate_timestamp_corrected
                self.logger.info(
                    f"Patch aplicado: pybit.generate_timestamp ahora retorna "
                    f"time.time() + ({self._time_offset_ms:+d}ms)"
                )
            else:
                self.logger.info(
                    f"Reloj sincronizado OK. Offset: {self._time_offset_ms:+d}ms. "
                    f"Sin necesidad de patch."
                )

        except Exception as e:
            self.logger.warning(
                f"Error en sincronizacion de tiempo: {e}. "
                f"El bot intentara continuar de todas formas."
            )
            self._time_offset_ms = 0

    def _connect(self) -> None:
        """
        Crea la sesion autenticada con Bybit API v5.
        Incluye correccion automatica de timestamp para evitar ErrCode 10002.
        En caso de error, reintenta con backoff exponencial.
        """
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                self.session = HTTP(
                    testnet=self.config.testnet,
                    api_key=self.config.api_key,
                    api_secret=self.config.api_secret,
                    recv_window=20000,
                )
                # Verificar conectividad y aplicar correccion de timestamp
                server_time = self.session.get_server_time()
                ts = server_time.get("result", {}).get("timeSecond", "N/A")
                self._sync_time()
                self.logger.info(
                    f"Conectado a Bybit {'TESTNET' if self.config.testnet else 'MAINNET'} "
                    f"| Hora servidor: {ts} | Offset reloj: {self._time_offset_ms:+d}ms"
                )
                return

            except Exception as e:
                delay = self._RETRY_BASE_DELAY ** attempt
                self.logger.warning(
                    f"Error de conexion (intento {attempt}/{self._MAX_RETRIES}): {e}. "
                    f"Reintentando en {delay:.1f}s..."
                )
                time.sleep(delay)

        raise ConnectionError(
            "❌ No se pudo conectar a Bybit después de múltiples intentos. "
            "Verifica tu API Key, Secret y conexión a internet."
        )

    def reconnect(self) -> None:
        """Fuerza una reconexión completa al exchange."""
        self.logger.warning("🔄 Iniciando reconexión a Bybit...")
        self.session = None
        self._connect()

    def _safe_request(self, func, *args, **kwargs):
        """
        Wrapper para llamadas a la API con manejo de errores y reconexión automática.
        Aplica Rate Limit respetando 10 req/s máximo de Bybit.

        Args:
            func: Función de la API de pybit a ejecutar.
            *args, **kwargs: Argumentos para la función.

        Returns:
            dict: Respuesta de la API o None en caso de fallo permanente.
        """
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                # Pequeña pausa para respetar Rate Limits (máx ~10 req/s en Bybit)
                time.sleep(0.15)
                response = func(*args, **kwargs)

                # Bybit retorna retCode=0 si todo fue exitoso
                ret_code = response.get("retCode", -1)
                if ret_code != 0:
                    raise ValueError(
                        f"Bybit API Error {ret_code}: {response.get('retMsg', 'Sin mensaje')}"
                    )
                return response

            except (ConnectionError, TimeoutError, OSError) as e:
                # Error de red → reconectar
                self.logger.error(f"🌐 Error de red (intento {attempt}): {e}")
                if attempt < self._MAX_RETRIES:
                    self.reconnect()
                    time.sleep(self._RETRY_BASE_DELAY * attempt)

            except ValueError as e:
                # Error de API (no reintentar si es un error de parámetros)
                self.logger.error(f"❌ Error de API: {e}")
                if "10006" in str(e) or "Rate Limit" in str(e):
                    # Rate limit exceeded → esperar más tiempo
                    self.logger.warning("⏳ Rate limit alcanzado. Esperando 60s...")
                    time.sleep(60)
                else:
                    return None

            except Exception as e:
                err_str = str(e)
                if "110043" in err_str:
                    # El apalancamiento ya está configurado exactamente igual en Bybit
                    self.logger.debug("Apalancamiento ya estaba configurado correctamente.")
                    return {"retCode": 0, "result": {}}

                # Errores determinísticos: no tiene sentido reintentar
                # 110007 = balance insuficiente (se resuelve ajustando qty, no reintentando)
                # 110017 = qty por debajo del mínimo
                # 110040 = posición ya cerrada
                # 110052 = reduce-only orden rechazada (sin posición que reducir)
                NON_RETRYABLE = {"110007", "110017", "110040", "110052"}
                if any(code in err_str for code in NON_RETRYABLE):
                    self.logger.error(
                        f"❌ Error no recuperable (ErrCode detectado): {e}. "
                        f"Abortando reintentos."
                    )
                    return None

                delay = self._RETRY_BASE_DELAY * attempt
                self.logger.error(
                    f"Error inesperado (intento {attempt}/{self._MAX_RETRIES}): {e}. "
                    f"Esperando {delay:.1f}s..."
                )
                time.sleep(delay)

        self.logger.error("Se agotaron los reintentos. Operacion fallida.")
        return None

    # =========================================================================
    # SECCIÓN 2: OBTENCIÓN DE DATOS HISTÓRICOS (KLINES)
    # =========================================================================

    def get_klines(self) -> Optional[pd.DataFrame]:
        """
        Obtiene las velas (OHLCV) del par configurado en la temporalidad de 5m.

        Returns:
            pd.DataFrame con columnas: [open_time, open, high, low, close, volume]
            ordenado de más antiguo a más reciente.
            Retorna None si ocurre un error.
        """
        self.logger.debug(
            f"📊 Obteniendo {self.config.klines_limit} klines de "
            f"{self.config.symbol} @ {self.config.timeframe}m"
        )

        response = self._safe_request(
            self.session.get_kline,
            category=self.config.category,
            symbol=self.config.symbol,
            interval=self.config.timeframe,
            limit=self.config.klines_limit,
        )

        if not response:
            return None

        try:
            raw_list = response["result"]["list"]
            if not raw_list:
                self.logger.error("La API retornó una lista de klines vacía.")
                return None

            # La API de Bybit retorna las velas en orden DESCENDENTE (más reciente primero)
            # Columnas: [startTime, open, high, low, close, volume, turnover]
            df = pd.DataFrame(
                raw_list,
                columns=["open_time", "open", "high", "low", "close", "volume", "turnover"],
            )

            # Convertir tipos de datos
            df["open_time"] = pd.to_datetime(df["open_time"].astype(int), unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            # Ordenar de más antiguo a más reciente (orden cronológico)
            df = df.sort_values("open_time").reset_index(drop=True)
            df = df.drop(columns=["turnover"])

            self.logger.debug(
                f"✅ {len(df)} klines cargados. "
                f"Última vela: {df['open_time'].iloc[-1]} | "
                f"Close: {df['close'].iloc[-1]:.2f}"
            )
            return df

        except (KeyError, ValueError) as e:
            self.logger.error(f"❌ Error procesando klines: {e}")
            return None

    # =========================================================================
    # SECCIÓN 4 (parte): CONSULTA DE BALANCE Y POSICIONES
    # =========================================================================

    def get_wallet_balance(self) -> Optional[float]:
        """
        Obtiene el balance disponible en USDT de la cuenta Unified.

        Returns:
            float: Balance disponible en USDT, o None si hay error.
        """
        response = self._safe_request(
            self.session.get_wallet_balance,
            accountType="UNIFIED",
            coin="USDT",
        )
        if not response:
            return None

        try:
            coins = response["result"]["list"][0]["coin"]
            for coin_data in coins:
                if coin_data["coin"] == "USDT":
                    # F3 FIX: Usar availableToWithdraw para reflejar el capital real
                    # disponible, excluyendo márgenes comprometidos y PnL no realizado.
                    # Fallback a walletBalance solo si availableToWithdraw viene vacío.
                    avail = coin_data.get("availableToWithdraw", "")
                    if avail and float(avail) > 0:
                        balance = float(avail)
                    else:
                        wallet_bal = coin_data.get("walletBalance", "0")
                        balance = float(wallet_bal) if wallet_bal else 0.0
                    self.logger.debug(f"Balance disponible (availableToWithdraw): {balance:.2f} USDT")
                    return balance
            return 0.0
        except (KeyError, IndexError, ValueError) as e:
            self.logger.error(f"Error obteniendo balance: {e}")
            return 0.0

    def get_open_position(self) -> Optional[dict]:
        """
        Verifica si existe una posición abierta en el símbolo configurado.

        Returns:
            dict con datos de la posición si existe, None si no hay posición.
            El dict incluye: 'side' ('Buy'/'Sell'), 'size', 'entryPrice', 'unrealisedPnl'
        """
        response = self._safe_request(
            self.session.get_positions,
            category=self.config.category,
            symbol=self.config.symbol,
        )
        if not response:
            return None

        try:
            positions = response["result"]["list"]
            for pos in positions:
                # Bybit retorna size="0" si no hay posición
                size = float(pos.get("size", 0))
                if size > 0:
                    self.logger.info(
                        f"📌 Posición abierta encontrada: "
                        f"Lado={pos['side']} | "
                        f"Tamaño={size} | "
                        f"Entrada={pos['avgPrice']} | "
                        f"PnL no realizado={pos['unrealisedPnl']}"
                    )
                    return {
                        "side": pos["side"],
                        "size": size,
                        "entry_price": float(pos["avgPrice"]),
                        "unrealised_pnl": float(pos["unrealisedPnl"]),
                        "leverage": pos.get("leverage", self.config.leverage),
                    }
            # No hay posición abierta
            return None

        except (KeyError, IndexError, ValueError) as e:
            self.logger.error(f"❌ Error verificando posiciones: {e}")
            return None

    def set_leverage(self) -> bool:
        """
        Configura el apalancamiento en el exchange para el símbolo actual.

        Returns:
            bool: True si se configuró correctamente, False si hubo error.
        """
        self.logger.info(
            f"⚙️  Configurando apalancamiento a {self.config.leverage}x "
            f"para {self.config.symbol}..."
        )
        response = self._safe_request(
            self.session.set_leverage,
            category=self.config.category,
            symbol=self.config.symbol,
            buyLeverage=str(self.config.leverage),
            sellLeverage=str(self.config.leverage),
        )
        if response:
            self.logger.info(f"✅ Apalancamiento configurado a {self.config.leverage}x")
            return True
        return False

    def get_instrument_info(self) -> Optional[dict]:
        """
        Obtiene información del instrumento (precisión de precio, tamaño mínimo, etc.).
        Esencial para calcular correctamente el tamaño de las órdenes.

        Returns:
            dict con 'price_scale', 'min_order_qty', 'qty_step'
        """
        response = self._safe_request(
            self.session.get_instruments_info,
            category=self.config.category,
            symbol=self.config.symbol,
        )
        if not response:
            return None

        try:
            info = response["result"]["list"][0]
            lot_filter = info["lotSizeFilter"]
            price_filter = info["priceFilter"]
            return {
                "price_scale": int(info.get("priceScale", 2)),
                "min_order_qty": float(lot_filter["minOrderQty"]),
                "qty_step": float(lot_filter["qtyStep"]),
                "tick_size": float(price_filter["tickSize"]),
            }
        except (KeyError, IndexError, ValueError) as e:
            self.logger.error(f"❌ Error obteniendo info del instrumento: {e}")
            return None

    def place_order(
        self,
        side: str,
        qty: float,
        stop_loss: float,
        take_profit: float,
        reduce_only: bool = False,
        tick_size: float = 0.5,
    ) -> Optional[dict]:
        """
        Ejecuta una orden de mercado con SL y TP integrados.

        Args:
            side: 'Buy' para Long, 'Sell' para Short/Cierre.
            qty: Cantidad de contratos/coins a operar.
            stop_loss: Precio del Stop Loss.
            take_profit: Precio del Take Profit.
            reduce_only: True para órdenes de cierre de posición.
            tick_size: Tamaño de tick del instrumento para redondeo correcto de precios.
        """
        # F4 FIX: Redondear SL/TP al tick_size real del instrumento
        # en lugar de 4 decimales hardcoded, que puede ser inválido en algunos pares.
        import math
        def _round_tick(price: float, tick: float) -> str:
            if tick <= 0:
                return str(round(price, 4))
            decimals = max(0, -int(math.floor(math.log10(tick))))
            return str(round(round(price / tick) * tick, decimals))

        sl_str = _round_tick(stop_loss, tick_size)
        tp_str = _round_tick(take_profit, tick_size)
        order_type = "APERTURA" if not reduce_only else "CIERRE"
        self.logger.info(
            f"📤 Enviando orden {order_type}: {side} | "
            f"Qty={qty} | SL={sl_str} | TP={tp_str}"
        )

        response = self._safe_request(
            self.session.place_order,
            category=self.config.category,
            symbol=self.config.symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            stopLoss=sl_str,
            takeProfit=tp_str,
            reduceOnly=reduce_only,
            timeInForce="IOC",  # Immediate or Cancel para órdenes de mercado
        )

        if response:
            order_id = response.get("result", {}).get("orderId", "N/A")
            self.logger.info(f"✅ Orden ejecutada exitosamente | OrderID: {order_id}")
            return response["result"]

        self.logger.error("❌ Falló la ejecución de la orden.")
        return None

    def close_position(self, current_side: str, size: float) -> Optional[dict]:
        """
        Cierra completamente una posición existente con una orden de mercado.

        Args:
            current_side: Lado de la posición actual ('Buy' para long, 'Sell' para short).
            size: Tamaño de la posición a cerrar.

        Returns:
            dict con info de la orden de cierre, o None si falló.
        """
        # Para cerrar un Long se vende, para cerrar un Short se compra
        close_side = "Sell" if current_side == "Buy" else "Buy"
        self.logger.info(
            f"🔒 Cerrando posición {current_side}: enviando {close_side} | Size={size}"
        )

        response = self._safe_request(
            self.session.place_order,
            category=self.config.category,
            symbol=self.config.symbol,
            side=close_side,
            orderType="Market",
            qty=str(size),
            reduceOnly=True,
            timeInForce="IOC",
        )

        if response:
            order_id = response.get("result", {}).get("orderId", "N/A")
            self.logger.info(f"✅ Posición cerrada | OrderID: {order_id}")
            return response["result"]

        self.logger.error("❌ Error cerrando la posición.")
        return None

    def get_last_closed_pnl(self, limit: int = 3) -> list:
        """
        Consulta el historial de PnL cerrado para detectar cierres por SL/TP.

        Bybit registra aquí cada posición cerrada automáticamente (SL, TP o liquidación),
        lo que permite reconciliar el estado interno del bot con la realidad del exchange.

        Args:
            limit: Número máximo de registros a traer (default=3 para capturar el más reciente).

        Returns:
            Lista de dicts con los campos relevantes de cada cierre:
              - 'side': 'Buy' (era Long) o 'Sell' (era Short)
              - 'qty': cantidad cerrada
              - 'avg_entry_price': precio promedio de entrada
              - 'avg_exit_price': precio promedio de salida
              - 'closed_pnl': PnL realizado en USDT (positivo=ganancia, negativo=pérdida)
              - 'created_time_ms': timestamp UTC del cierre en milisegundos
        """
        response = self._safe_request(
            self.session.get_closed_pnl,
            category=self.config.category,
            symbol=self.config.symbol,
            limit=limit,
        )
        if not response:
            return []

        try:
            results = []
            for entry in response["result"]["list"]:
                pnl  = float(entry.get("closedPnl", 0) or 0)
                qty  = float(entry.get("qty", 0) or 0)
                ep   = float(entry.get("avgEntryPrice", 0) or 0)
                xp   = float(entry.get("avgExitPrice", 0) or 0)
                ts   = int(entry.get("createdTime", 0) or 0)
                side = entry.get("side", "")
                results.append({
                    "side":             side,
                    "qty":              qty,
                    "avg_entry_price":  ep,
                    "avg_exit_price":   xp,
                    "closed_pnl":       pnl,
                    "created_time_ms":  ts,
                })
            return results
        except (KeyError, IndexError, ValueError) as e:
            self.logger.error(f"❌ Error obteniendo closed PnL: {e}")
            return []
