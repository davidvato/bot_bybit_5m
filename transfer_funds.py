"""
transfer_funds.py - Transfiere USDT de Fund/Spot a Unified Trading en Bybit Testnet.
Ejecutar UNA SOLA VEZ para mover los fondos de prueba.
"""
import time
import os
import sys
import io
import uuid

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from pybit.unified_trading import HTTP
from dotenv import load_dotenv
import pybit._helpers as _helpers

load_dotenv()

# ---- Patch de timestamp ----
_anon = HTTP(testnet=True)
srv = _anon.get_server_time()
srv_ms = int(srv['result']['timeNano']) // 1_000_000
offset = srv_ms - int(time.time() * 1000)
print(f"Offset del reloj: {offset:+d}ms")
_helpers.generate_timestamp = lambda: int(time.time() * 1000) + offset

# ---- Sesion autenticada ----
session = HTTP(
    testnet=True,
    api_key=os.getenv('BYBIT_API_KEY'),
    api_secret=os.getenv('BYBIT_API_SECRET'),
    recv_window=20000,
)

print("\nIntentando transferir USDT a cuenta Unified...\n")

# Probar desde los diferentes tipos de cuenta origen posibles
origins = ["FUND", "SPOT"]
amount = "10000"  # USDT a transferir

for origin in origins:
    try:
        transfer_id = str(uuid.uuid4())
        result = session.create_internal_transfer(
            transferId=transfer_id,
            coin="USDT",
            amount=amount,
            fromAccountType=origin,
            toAccountType="UNIFIED",
        )
        ret_code = result.get("retCode", -1)
        ret_msg  = result.get("retMsg", "")
        if ret_code == 0:
            print(f"OK: Transferencia exitosa desde {origin} -> UNIFIED | {amount} USDT")
            break
        else:
            print(f"[{origin}] Sin fondos o error: {ret_msg} (code={ret_code})")
    except Exception as e:
        err_str = str(e)
        if "not enough" in err_str.lower() or "insufficient" in err_str.lower():
            print(f"[{origin}] Sin saldo suficiente.")
        elif "10001" in err_str or "params" in err_str.lower():
            print(f"[{origin}] Tipo de cuenta no soportado: {err_str[:100]}")
        else:
            print(f"[{origin}] Error: {err_str[:150]}")

print("\n--- Balance Unified despues de transferencia ---")
try:
    r = session.get_wallet_balance(accountType='UNIFIED')
    acc = r['result']['list'][0]
    print(f"  totalEquity           : {acc.get('totalEquity','N/A')}")
    print(f"  totalWalletBalance    : {acc.get('totalWalletBalance','N/A')}")
    print(f"  totalAvailableBalance : {acc.get('totalAvailableBalance','N/A')}")
    for c in acc.get('coin', []):
        if c.get('coin') == 'USDT':
            print(f"  USDT walletBalance    : {c.get('walletBalance','N/A')}")
            print(f"  USDT available        : {c.get('availableToWithdraw','N/A')}")
except Exception as e:
    print(f"Error leyendo balance: {e}")
