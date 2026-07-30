# 🤖 Bot de Trading BTC/USDT — Bybit API v5

Bot de trading algorítmico profesional para **Bitcoin (BTCUSDT)** en la plataforma **Bybit**, operando en temporalidad de **5 minutos** con un sistema de consenso de **4 estrategias técnicas independientes**. Usa la librería `ta` (compatible con Python 3.10–3.14+).

---

## 📁 Estructura del Proyecto

```
bot-bybit/
├── bot.py            # 🚀 Punto de entrada / Bucle principal
├── config.py         # ⚙️  Configuración centralizada (carga .env)
├── exchange.py       # 🔌 Conexión a Bybit API v5 + reconexión automática
├── strategies.py     # 🧠 Motor de indicadores y sistema de consenso (4 estrategias)
├── risk_manager.py   # 🛡️  Gestión de riesgo (SL/TP dinámico, tamaño posición)
├── logger.py         # 📝 Sistema de logging (consola + archivo rotativo)
├── .env.example      # 🔑 Plantilla de variables de entorno
├── .env              # 🔑 Variables de entorno REALES (no subir a git)
├── .gitignore        # 🔒 Protege credenciales y archivos sensibles
├── requirements.txt  # 📦 Dependencias Python
└── logs/             # 📊 Logs generados por el bot (creado automáticamente)
```

---

## 🧠 Estrategias Implementadas (Sistema de Consenso 3/4)

Una señal de trading se genera **solo si al menos 3 de 4 estrategias coinciden**:

| # | Estrategia | Indicadores | Señal LONG | Señal SHORT |
|---|-----------|------------|-----------|------------|
| 1 | **EMA Cross Adaptativo** | EMA 9, EMA 21 | EMA 9 cruza al alza EMA 21 | EMA 9 cruza a la baja EMA 21 |
| 2 | **Tendencia + StochRSI** | EMA 200, StochRSI (14,3,3) | Precio > EMA 200 + K cruza D desde <20 | Precio < EMA 200 + K cruza D desde >80 |
| 3 | **Bollinger Reversal** | BB (20, 2σ), RSI (14) | Vela previa bajo BB_low + reversión dentro | Vela previa sobre BB_high + reversión dentro |
| 4 | **MACD + Volumen** | MACD (12,26,9), Vol SMA 20 | Histograma neg→pos + volumen > SMA | Histograma pos→neg + volumen > SMA |

---

## ⚙️ Instalación

### 1. Clonar y preparar el entorno

```bash
# Crear entorno virtual (recomendado)
python -m venv .venv

# Activar entorno virtual
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt
```

### 2. Configurar las credenciales

```bash
# Copiar la plantilla
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux

# Editar .env con tus datos reales
notepad .env
```

Edita el archivo `.env` con tus credenciales:

```env
# Credenciales de Bybit (obtener en: Bybit > Account > API Management)
BYBIT_API_KEY=tu_api_key_aqui
BYBIT_API_SECRET=tu_api_secret_aqui

# IMPORTANTE: Empieza siempre en Testnet
BYBIT_TESTNET=true

# Configuración del bot
TRADING_SYMBOL=BTCUSDT
TIMEFRAME=5
MARKET_CATEGORY=linear

# Gestión de riesgo (ajustar según tu perfil)
RISK_PER_TRADE=0.02       # 2% del balance por operación
LEVERAGE=5                # 5x apalancamiento
ATR_SL_MULTIPLIER=1.5    # Stop Loss = 1.5 * ATR
RISK_REWARD_RATIO=2.0    # Take Profit = 2 * distancia del SL
```

> ⚠️ **NUNCA** subas el archivo `.env` a un repositorio público. Ya está protegido por `.gitignore`.

### 3. Obtener tus API Keys en Bybit

1. Inicia sesión en [Bybit](https://www.bybit.com/) o [Bybit Testnet](https://testnet.bybit.com/)
2. Ve a **Account** → **API Management** → **Create New Key**
3. Selecciona tipo: **System-generated API Keys**
4. Permisos requeridos:
   - ✅ **Contract - Read & Write** (para operar futuros)
   - ✅ **Unified Trading - Read & Write**
5. Copia el API Key y API Secret (el secret solo se muestra una vez)

---

## 🚀 Ejecución

```bash
# Asegúrate de que el entorno virtual está activado
python bot.py
```

El bot mostrará:
```
============================================================
  🤖 BOT DE TRADING BTC/USDT BYBIT - INICIANDO
============================================================
  Símbolo:       BTCUSDT
  Temporalidad:  5m
  Modo:          ⚠️  TESTNET
  Apalancamiento:5x
  Riesgo/trade:  2.0%
  Consenso mín:  3/4 estrategias
...
⏳ Próxima ejecución en 247.3s (18:45:00 UTC)
```

Para detener el bot de forma segura: **Ctrl+C**

---

## 🛡️ Gestión de Riesgo

| Parámetro | Valor por defecto | Descripción |
|-----------|------------------|-------------|
| `RISK_PER_TRADE` | 2% | % del balance arriesgado por operación |
| `LEVERAGE` | 5x | Apalancamiento fijo |
| `ATR_SL_MULTIPLIER` | 1.5 | Multiplicador ATR para el Stop Loss |
| `RISK_REWARD_RATIO` | 2.0 | Ratio TP/SL (siempre ≥ 2:1) |
| Consenso mínimo | 3/4 | Estrategias necesarias para operar |
| Anti-duplicado | ✅ | Verifica posición existente antes de operar |

---

## 📊 Flujo del Bucle Principal

```
┌─────────────────────────────────────────────────────────┐
│  CADA CIERRE DE VELA DE 5 MINUTOS                       │
│                                                         │
│  1. 📡 Obtener 300 klines históricos (Bybit API v5)     │
│  2. 🔢 Calcular indicadores (EMA, StochRSI, BB, MACD)  │
│  3. 🧠 Evaluar 4 estrategias independientemente         │
│  4. 📊 Sistema de Consenso: ¿≥3 estrategias acuerdan?  │
│     ├── ✅ SÍ → 5. Calcular SL/TP con ATR dinámico     │
│     │           6. Calcular qty por % de balance        │
│     │           7. Verificar posición existente         │
│     │           8. Enviar orden a Bybit API v5          │
│     └── ⏸️  NO → Esperar próxima vela                   │
└─────────────────────────────────────────────────────────┘
```

---

## 🔧 Personalización Avanzada

Todos los parámetros de indicadores están centralizados en [`config.py`](config.py) y pueden ajustarse directamente en el código:

```python
# Períodos de indicadores
ema_fast: int = 9          # EMA rápida (estrategia 1)
ema_slow: int = 21         # EMA lenta (estrategia 1)
ema_trend: int = 200       # EMA macro (estrategia 2)
stoch_rsi_period: int = 14 # StochRSI período (estrategia 2)
bb_period: int = 20        # Bollinger Bands (estrategia 3)
macd_fast: int = 12        # MACD fast (estrategia 4)
macd_slow: int = 26        # MACD slow (estrategia 4)
macd_signal: int = 9       # MACD signal (estrategia 4)
min_consensus: int = 3     # Votos mínimos para operar (1-4)
```

---

## ⚠️ Advertencias Importantes

> **RIESGO REAL**: Este bot opera con dinero real cuando `BYBIT_TESTNET=false`. Siempre prueba exhaustivamente en Testnet antes.

> **NO ES ASESORÍA FINANCIERA**: Este código es solo educativo/técnico. El trading algorítmico conlleva riesgos significativos de pérdida de capital.

> **BACKTEST PRIMERO**: Antes de operar en producción, realiza backtesting histórico de las estrategias con tus parámetros.

---

## 📝 Logs

Los logs se guardan automáticamente en `logs/bot_trading.log` con rotación automática (máx 5MB, 3 backups). Nivel configurable con `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR`.
