import os, time, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
import pybit._helpers as _helpers

load_dotenv()
_anon = HTTP(testnet=True)
srv = _anon.get_server_time()
srv_ms = int(srv['result']['timeNano']) // 1_000_000
offset = srv_ms - int(time.time() * 1000)
_helpers.generate_timestamp = lambda: int(time.time() * 1000) + offset

session = HTTP(
    testnet=True,
    api_key=os.getenv('BYBIT_API_KEY'),
    api_secret=os.getenv('BYBIT_API_SECRET'),
    recv_window=20000,
)
print("Buscando balances en UNIFIED...")
try:
    r = session.get_wallet_balance(accountType='UNIFIED')
    import json
    print(json.dumps(r, indent=2))
except Exception as e:
    print("Error:", e)
