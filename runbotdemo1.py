#!/usr/bin/env python3
"""
Binance Futures Trading Bot - CYBER QUANT V7.3 (Hedge Mode Edition)
核心修正：强制开启双向持仓（Hedge Mode），实现多空对冲以极大拉远爆仓价。
"""

import os
import json
import time
import threading
import asyncio
import webbrowser
from datetime import datetime
from collections import deque
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from binance.um_futures import UMFutures
import dotenv
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn

# --- 1. 配置加载 ---
dotenv.load_dotenv("1.env")

@dataclass
class TradingConfig:
    symbol: str = os.getenv("SYMBOL", "ETHUSDC")
    quantity: float = float(os.getenv("QUANTITY", 0.01))
    take_profit: float = float(os.getenv("TAKE_PROFIT", 0.5)) # 刷量建议点差设小
    max_orders: int = int(os.getenv("MAX_ORDERS", 210))
    # 不同阶段的冷却时间（秒）
    wait_times: Dict[str, int] = field(default_factory=lambda: {
        "t1": 15, "t2": 60, "t3": 300
    })

class BotState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.current_price = 0.0
        self.wallet_balance = 0.0
        self.unrealized_pnl = 0.0
        
        # 多空独立状态追踪
        self.long_count = 0
        self.short_count = 0
        self.long_cd = 0
        self.short_cd = 0
        
        self.positions = []
        self.logs = deque(maxlen=50)

state = BotState()

def add_log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    with state.lock:
        state.logs.appendleft({"time": ts, "type": level, "msg": msg})

# --- 2. 交易引擎 ---
class BinanceEngine:
    def __init__(self):
        api_key = os.getenv("API_KEY")
        api_secret = os.getenv("API_SECRET")
        proxy = os.getenv("HTTPS_PROXY")
        self.proxies = {'https': proxy} if proxy else None
        
        self.client = UMFutures(key=api_key, secret=api_secret, proxies=self.proxies)
        self.cfg = TradingConfig()
        self.stop_evt = threading.Event()

    def set_hedge_mode(self):
        """核心修正：强制切换至双向持仓模式"""
        try:
            # dualSidePosition="true" 表示双向持仓
            self.client.change_position_mode(dualSidePosition="true", recvWindow=5000)
            add_log("系统已强制同步为【双向持仓模式】", "SYSTEM")
        except Exception as e:
            if "No need to change" in str(e):
                add_log("账户已处于双向持仓模式", "INFO")
            else:
                add_log(f"切换持仓模式失败: {e}", "ERROR")

    def sync_account(self):
        """同步持仓和订单数"""
        while not self.stop_evt.is_set():
            try:
                acc = self.client.account()
                orders = self.client.get_orders(symbol=self.cfg.symbol)
                
                with state.lock:
                    state.wallet_balance = float(acc.get('totalWalletBalance', 0))
                    state.unrealized_pnl = float(acc.get('totalUnrealizedProfit', 0))
                    state.positions = [p for p in acc.get('positions', []) if float(p.get('positionAmt', 0)) != 0]
                    
                    # 统计多空各自的挂单（止盈单）数量
                    state.long_count = len([o for o in orders if o['positionSide'] == 'LONG' and o['side'] == 'SELL'])
                    state.short_count = len([o for o in orders if o['positionSide'] == 'SHORT' and o['side'] == 'BUY'])
                    
                    # 获取最新价格（简单轮询）
                    ticker = self.client.ticker_price(self.cfg.symbol)
                    state.current_price = float(ticker['price'])
            except: pass
            time.sleep(2)

    def run_strategy(self, pos_side: str):
        """独立线程运行：LONG 或 SHORT 逻辑"""
        side_label = "多头" if pos_side == "LONG" else "空头"
        order_side = "BUY" if pos_side == "LONG" else "SELL"
        tp_side = "SELL" if pos_side == "LONG" else "BUY"
        
        add_log(f"{side_label} 策略线程已启动", "SYSTEM")
        
        while state.running:
            try:
                # 检查挂单上限
                count = state.long_count if pos_side == "LONG" else state.short_count
                if count >= self.cfg.max_orders:
                    time.sleep(10); continue

                # 1. 尝试开仓 (使用 QUEUE 模式入场)
                res = self.client.new_order(
                    symbol=self.cfg.symbol, 
                    side=order_side, 
                    positionSide=pos_side, # 关键：明确指定持仓方向
                    type='LIMIT', 
                    quantity=self.cfg.quantity, 
                    priceMatch='QUEUE', 
                    timeInForce='GTC'
                )
                oid = res['orderId']
                
                # 2. 等待成交
                fill_price = 0
                for _ in range(10):
                    chk = self.client.get_order(symbol=self.cfg.symbol, orderId=oid)
                    if chk['status'] == 'FILLED':
                        fill_price = float(chk['avgPrice'])
                        break
                    time.sleep(1)
                
                if fill_price > 0:
                    # 3. 挂止盈
                    tp_price = round(fill_price + self.cfg.take_profit if pos_side == "LONG" else fill_price - self.cfg.take_profit, 2)
                    self.client.new_order(
                        symbol=self.cfg.symbol, 
                        side=tp_side, 
                        positionSide=pos_side, 
                        type='LIMIT',
                        quantity=self.cfg.quantity, 
                        price=str(tp_price),
                        reduceOnly="true", 
                        timeInForce='GTC'
                    )
                    add_log(f"[{side_label}] 成交 @{fill_price} -> 止盈已挂 @{tp_price}", "SUCCESS")
                else:
                    self.client.cancel_order(symbol=self.cfg.symbol, orderId=oid)
                    add_log(f"[{side_label}] 入场超时撤单", "WARN")

                # 4. 冷却
                wait = self.cfg.wait_times["t1"] if count < 50 else self.cfg.wait_times["t2"]
                for i in range(wait, 0, -1):
                    if not state.running: break
                    if pos_side == "LONG": state.long_cd = i
                    else: state.short_cd = i
                    time.sleep(1)

            except Exception as e:
                add_log(f"[{side_label}] 异常: {e}", "ERROR")
                time.sleep(5)

engine = BinanceEngine()

# --- 3. 接口与 UI ---
app = FastAPI()

@app.post("/api/toggle")
async def toggle():
    state.running = not state.running
    if state.running:
        engine.set_hedge_mode() # 启动时检查持仓模式
        threading.Thread(target=engine.run_strategy, args=("LONG",), daemon=True).start()
        threading.Thread(target=engine.run_strategy, args=("SHORT",), daemon=True).start()
    return {"running": state.running}

@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            payload = {
                "price": state.current_price, "balance": state.wallet_balance, "upnl": state.unrealized_pnl,
                "running": state.running, "long_count": state.long_count, "short_count": state.short_count,
                "long_cd": state.long_cd, "short_cd": state.short_cd,
                "positions": state.positions, "logs": list(state.logs)
            }
            await ws.send_json(payload); await asyncio.sleep(1)
    except: pass

@app.get("/")
async def root():
    # 修复核心：在 Windows 环境下显式使用 utf-8 读取文件，避免 UnicodeDecodeError
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            content = f.read()
        if "'''html" in content:
            return HTMLResponse(content=content.split("'''html")[-1].split("html'''")[0])
    except:
        pass
    return HTMLResponse(content=html_content)

html_content = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><script src="https://cdn.tailwindcss.com"></script><script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
<style>body{background:#0b0e11;color:#eaecef;}.card{background:#1e2329;border:1px solid #30363d;border-radius:12px;}</style></head>
<body class="p-6"><div id="app" class="max-w-4xl mx-auto">
    <div class="flex justify-between items-center mb-8">
        <h1 class="text-2xl font-bold text-blue-500">CYBER QUANT HEDGE <span class="text-white font-light text-sm italic">V7.3 对冲版</span></h1>
        <button @click="toggle" :class="running?'bg-red-600':'bg-green-600'" class="px-10 py-2 rounded-lg font-bold transition-all">{{running?'停止运行':'启动双向对冲'}}</button>
    </div>
    <div class="grid grid-cols-3 gap-4 mb-6 text-center">
        <div class="card p-4"><div class="text-gray-500 text-xs mb-1">价格</div><div class="text-xl font-mono text-yellow-500">{{price.toFixed(2)}}</div></div>
        <div class="card p-4"><div class="text-gray-500 text-xs mb-1">可用余额</div><div class="text-xl font-mono text-blue-400">{{balance.toFixed(2)}}</div></div>
        <div class="card p-4"><div class="text-gray-500 text-xs mb-1">未实现盈亏</div><div :class="upnl>=0?'text-green-500':'text-red-500'" class="text-xl font-mono">{{upnl.toFixed(2)}}</div></div>
    </div>
    <div class="grid grid-cols-2 gap-6 mb-8">
        <div class="card p-6 border-l-4 border-green-500">
            <div class="flex justify-between mb-4"><span class="font-bold text-green-500">多头 LONG</span><span class="text-xs text-gray-500">冷却: {{long_cd}}s</span></div>
            <div class="text-3xl font-mono">{{long_count}} <span class="text-sm text-gray-600">单挂单</span></div>
        </div>
        <div class="card p-6 border-l-4 border-red-500">
            <div class="flex justify-between mb-4"><span class="font-bold text-red-500">空头 SHORT</span><span class="text-xs text-gray-500">冷却: {{short_cd}}s</span></div>
            <div class="text-3xl font-mono">{{short_count}} <span class="text-sm text-gray-600">单挂单</span></div>
        </div>
    </div>
    <div class="card p-4 h-64 overflow-hidden flex flex-col"><div class="text-xs text-gray-500 mb-2 font-bold uppercase">运行终端</div>
        <div class="overflow-y-auto flex-1 space-y-1 text-[11px] font-mono">
            <div v-for="log in logs" class="flex gap-3 border-l-2 border-gray-800 pl-2">
                <span class="text-gray-600">{{log.time}}</span>
                <span :class="{'text-green-400':log.type==='SUCCESS','text-red-400':log.type==='ERROR','text-blue-400':log.type==='SYSTEM'}" class="font-bold">[{{log.type}}]</span>
                <span class="text-gray-300">{{log.msg}}</span>
            </div>
        </div>
    </div>
</div><script>
    const { createApp } = Vue;
    createApp({
        data(){return{price:0,balance:0,upnl:0,running:false,long_count:0,short_count:0,long_cd:0,short_cd:0,logs:[]}},
        methods:{async toggle(){const r=await fetch('/api/toggle',{method:'POST'});const d=await r.json();this.running=d.running;}},
        mounted(){
            const ws=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws');
            ws.onmessage=(e)=>{Object.assign(this,JSON.parse(e.data));};
        }
    }).mount('#app');
</script></body></html>
"""

if __name__ == "__main__":
    # 修改端口为 8083 以避免端口占用冲突
    threading.Thread(target=engine.sync_account, daemon=True).start()
    print("正在启动 Web 服务，请访问: http://127.0.0.1:8083")
    uvicorn.run(app, host="127.0.0.1", port=8083)