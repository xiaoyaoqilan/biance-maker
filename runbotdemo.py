#!/usr/bin/env python3
"""
Binance Volume Farming Bot - V4.8 (Pure Maker Mode)
核心改进：
1. 强制 Maker：保留 GTX (Post-Only)，确保绝不支付 Taker 手续费。
2. 智能重试：捕捉 -5022 错误后，自动增加 Price Offset 重新挂单。
3. 动态价位：避免与盘口直接碰撞，提高挂单成功率。
"""

import os
import time
import threading
import asyncio
from datetime import datetime
from collections import deque
from dataclasses import dataclass
from typing import Optional

from binance.um_futures import UMFutures
import dotenv
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn

# --- 1. 配置加载 ---
dotenv.load_dotenv("1.env")

@dataclass
class StrategyConfig:
    symbol: str = os.getenv("SYMBOL", "ETHUSDC")
    quantity: float = float(os.getenv("QUANTITY", 0.01)) 
    offset_dist: float = 0.20         # 基础偏移（略微调大以减少碰撞）
    take_profit_dist: float = 0.15    # 止盈距离
    interval: float = 2.0            
    leverage: int = 20               

class StrategyState:
    def __init__(self, name, open_side, pos_side):
        self.name = name
        self.open_side = open_side    
        self.pos_side = pos_side      
        self.close_side = "SELL" if open_side == "BUY" else "BUY"
        self.running = False
        self.pos_amt = 0.0
        self.last_exec_time = 0.0

class GlobalManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.current_price = 0.0
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.wallet_balance = 0.0
        self.total_wallet_balance = 0.0
        self.unrealized_pnl = 0.0
        self.logs = deque(maxlen=50)
        self.is_connected = False
        self.cfg = StrategyConfig()
        self.long = StrategyState("多单刷量", "BUY", "LONG")
        self.short = StrategyState("空单刷量", "SELL", "SHORT")

    def add_log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.appendleft({"time": ts, "type": level, "msg": msg})

gm = GlobalManager()

# --- 2. 核心交易引擎 ---
class TradingEngine:
    def __init__(self):
        self.api_key = os.getenv("API_KEY", "").strip()
        self.api_secret = os.getenv("API_SECRET", "").strip()
        self.client: Optional[UMFutures] = None
        self.stop_evt = threading.Event()
        self.init_client()

    def init_client(self):
        proxy_url = os.getenv("HTTPS_PROXY", "http://127.0.0.1:7890")
        proxies = {'http': proxy_url, 'https': proxy_url}
        self.client = UMFutures(key=self.api_key, secret=self.api_secret, proxies=proxies)
        
        try:
            self.client.change_leverage(symbol=gm.cfg.symbol, leverage=gm.cfg.leverage)
            gm.add_log(f"系统：强制 20x 杠杆 & 纯 Maker 模式启动", "SYSTEM")
        except: pass

    def account_sync_loop(self):
        while not self.stop_evt.is_set():
            try:
                ticker = self.client.book_ticker(gm.cfg.symbol)
                acc = self.client.account(recvWindow=5000)
                with gm.lock:
                    gm.best_bid = float(ticker['bidPrice'])
                    gm.best_ask = float(ticker['askPrice'])
                    gm.current_price = (gm.best_bid + gm.best_ask) / 2
                    gm.wallet_balance = float(acc.get('availableBalance', 0)) 
                    gm.total_wallet_balance = float(acc.get('totalWalletBalance', 0))
                    gm.unrealized_pnl = float(acc.get('totalUnrealizedProfit', 0))
                    
                    for p in acc.get('positions', []):
                        if p['symbol'] == gm.cfg.symbol:
                            if p['positionSide'] == 'LONG': gm.long.pos_amt = float(p['positionAmt'])
                            elif p['positionSide'] == 'SHORT': gm.short.pos_amt = float(p['positionAmt'])
                    gm.is_connected = True
            except: gm.is_connected = False
            time.sleep(1.0)

    def cancel_all_orders(self):
        try:
            self.client.cancel_open_orders(symbol=gm.cfg.symbol, recvWindow=5000)
        except: pass

    def execute_logic(self, state: StrategyState, retry_count: int = 0):
        """
        retry_count: 记录由于 Maker 保护触发的重试次数，次数越多，挂单价越保守
        """
        if gm.wallet_balance < 1.0: 
            return

        try:
            # 核心计算：每一轮重试，价格都会向远离盘口的方向挪动 0.05 刀
            # 这样能确保即使在波动行情，也能在第二次或第三次尝试中成功挂入 Maker 单
            retry_offset = retry_count * 0.05
            
            if state.pos_side == "LONG":
                # 多单：买入价要低于卖一价。如果重试，买入价更低
                open_price = gm.best_ask - (gm.cfg.offset_dist + retry_offset)
                tp_price = open_price + gm.cfg.take_profit_dist
            else:
                # 空单：卖出价要高于买一价。如果重试，卖出价更高
                open_price = gm.best_bid + (gm.cfg.offset_dist + retry_offset)
                tp_price = open_price - gm.cfg.take_profit_dist

            # 强制 GTX (Post-Only)
            self.client.new_order(
                symbol=gm.cfg.symbol, side=state.open_side, positionSide=state.pos_side,
                type='LIMIT', timeInForce='GTX', quantity=gm.cfg.quantity, price="{:.2f}".format(open_price)
            )
            
            # 止盈挂单 (GTC 即可，因为止盈单本来就在远处挂着等成交)
            try:
                self.client.new_order(
                    symbol=gm.cfg.symbol, side=state.close_side, positionSide=state.pos_side,
                    type='LIMIT', timeInForce='GTC', quantity=gm.cfg.quantity, price="{:.2f}".format(tp_price)
                )
            except: pass
            
            if retry_count > 0:
                gm.add_log(f"[{state.pos_side}] 保护性挂单成功 (修正次数:{retry_count})", "SYSTEM")
            else:
                gm.add_log(f"[{state.pos_side}] Maker 开仓挂单成功: {open_price:.2f}", "ACTION")

        except Exception as e:
            msg = str(e)
            if "-5022" in msg:
                # 价格太接近，如果直接下单会变成 Taker。机器人自动退后 0.05 再次尝试。
                if retry_count < 5: # 最多退后 5 次，防止离谱挂单
                    self.execute_logic(state, retry_count + 1)
            elif "-2019" in msg:
                gm.add_log(f"保证金不足，暂缓操作", "ERROR")
            else:
                gm.add_log(f"接口返回: {msg[:30]}", "ERROR")

    def worker(self, state: StrategyState):
        while state.running and not self.stop_evt.is_set():
            if not gm.is_connected:
                time.sleep(1); continue
            
            # 只有在没有持仓或持仓极小时才开新单（刷量逻辑）
            if abs(state.pos_amt) < (gm.cfg.quantity * 0.5):
                now = time.time()
                if now - state.last_exec_time >= gm.cfg.interval:
                    self.execute_logic(state)
                    state.last_exec_time = now
            time.sleep(0.5)

engine = TradingEngine()

# --- 3. 后端服务 ---
app = FastAPI()

@app.get("/")
async def index(): return HTMLResponse(content=html_ui)

@app.post("/api/toggle")
async def toggle():
    is_running = not gm.long.running
    gm.long.running = gm.short.running = is_running
    if is_running:
        threading.Thread(target=engine.worker, args=(gm.long,), daemon=True).start()
        threading.Thread(target=engine.worker, args=(gm.short,), daemon=True).start()
        gm.add_log(f"机器人已启动 (强制 Maker 机制开启)", "SYSTEM")
    else:
        engine.cancel_all_orders()
        gm.add_log(f"机器人已停止并清理挂单", "SYSTEM")
    return {"running": is_running}

@app.websocket("/ws")
async def ws_feed(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            payload = {
                "price": gm.current_price, 
                "balance": gm.wallet_balance, 
                "upnl": gm.unrealized_pnl, 
                "connected": gm.is_connected,
                "long_pos": gm.long.pos_amt,
                "short_pos": gm.short.pos_amt,
                "running": gm.long.running,
                "logs": list(gm.logs)
            }
            await ws.send_json(payload)
            await asyncio.sleep(0.5)
    except: pass

html_ui = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8"><title>Binance Maker Bot V4.8</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <style>
        body { background: #0b0e11; color: #eaecef; font-family: 'JetBrains Mono', monospace; }
        .card { background: #1e2329; border-radius: 12px; padding: 1.25rem; border: 1px solid #30363d; }
    </style>
</head>
<body class="p-4 md:p-8">
    <div id="app" class="max-w-4xl mx-auto">
        <div class="flex justify-between items-center mb-8">
            <h1 class="text-2xl font-black text-yellow-500 italic">PURE MAKER MODE <span class="text-xs text-gray-500 not-italic ml-2">V4.8</span></h1>
            <button @click="toggle" :class="running ? 'bg-red-500' : 'bg-yellow-600'" class="px-10 py-3 rounded-xl text-white font-bold shadow-xl transition-all">
                {{ running ? '停止运行' : '开启做市刷量' }}
            </button>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6 text-center">
            <div class="card"><div class="text-[10px] text-gray-500 mb-1">当前价格</div><div class="text-2xl font-mono">${{ price.toFixed(2) }}</div></div>
            <div class="card"><div class="text-[10px] text-gray-500 mb-1">可用保证金</div><div class="text-2xl font-mono text-yellow-500">${{ balance.toFixed(2) }}</div></div>
            <div class="card"><div class="text-[10px] text-gray-500 mb-1">未实现盈亏</div><div class="text-2xl font-mono" :class="upnl>=0?'text-green-500':'text-red-500'">${{ upnl.toFixed(2) }}</div></div>
        </div>

        <div class="grid grid-cols-2 gap-4 mb-6">
            <div class="card border-l-4 border-green-500">
                <div class="text-[10px] text-gray-500 mb-1">LONG POS</div>
                <div class="text-2xl font-bold">{{ long_pos.toFixed(3) }}</div>
            </div>
            <div class="card border-l-4 border-red-500">
                <div class="text-[10px] text-gray-500 mb-1">SHORT POS</div>
                <div class="text-2xl font-bold">{{ Math.abs(short_pos).toFixed(3) }}</div>
            </div>
        </div>

        <div class="card bg-black/40">
            <div class="flex justify-between mb-2 text-[10px] text-gray-600 font-bold uppercase tracking-widest">
                <span>实时日志 (Maker Protection Active)</span>
                <span class="text-blue-500">自动防吃单保护已开启</span>
            </div>
            <div class="h-64 overflow-y-auto space-y-1 text-[11px] font-mono">
                <div v-for="log in logs" class="flex gap-3">
                    <span class="text-gray-600 italic">{{ log.time }}</span>
                    <span :class="{'text-yellow-500':log.type==='ACTION','text-red-400':log.type==='ERROR','text-blue-400':log.type==='SYSTEM'}">{{ log.msg }}</span>
                </div>
            </div>
        </div>
    </div>
    <script>
        const { createApp } = Vue;
        createApp({
            data() { return { price: 0, balance: 0, upnl: 0, connected: false, long_pos: 0, short_pos: 0, running: false, logs: [] } },
            methods: { toggle() { fetch('/api/toggle', {method:'POST'}); } },
            mounted() {
                const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
                ws.onmessage = (e) => { Object.assign(this, JSON.parse(e.data)); };
            }
        }).mount('#app');
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    threading.Thread(target=engine.account_sync_loop, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8082, log_level="error")