#!/usr/bin/env python3
"""
Binance Futures Trading Bot - CYBER QUANT V7.3
FIXED: Uptime Display & Log Auto-Scroll (Top-Locked)
"""

import os
import json
import sys
import time
import threading
import math
import asyncio
import logging
import webbrowser
from datetime import datetime
from collections import deque
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Deque, Set
from contextlib import asynccontextmanager

from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient
import dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# --- 1. 环境配置 ---
dotenv.load_dotenv()
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"

# --- 2. 核心数据结构 ---
@dataclass
class TradingConfig:
    symbol: str = 'ETHUSDC'
    quantity: float = 0.01
    take_profit: float = 1.0
    direction: str = 'BUY'
    max_orders: int = 210
    wait_t1a: int = 15   
    wait_t1b: int = 30   
    wait_t1c: int = 60   
    wait_t2: int = 180   
    wait_t3: int = 600   

@dataclass
class BotState:
    running: bool = False
    shutdown_flag: bool = False
    symbol: str = "ETHUSDC"
    current_price: float = 0.0
    active_orders_count: int = 0
    cumulative_pnl: float = 0.0
    total_exposure: float = 0.0
    
    current_tier_name: str = "IDLE (DATA ONLY)"
    next_order_countdown: int = 0
    tier_limits_display: str = "--/--"
    
    wallet_balance: float = 0.0
    unrealized_pnl: float = 0.0
    
    equity_history: Deque[Dict] = field(default_factory=lambda: deque(maxlen=43200))  # 30 days * 24 hours * 60 mins
    equity_history_raw: Deque[Dict] = field(default_factory=lambda: deque(maxlen=3600))  # Last hour raw data for aggregation
    last_minute_timestamp: int = 0  # Track last minute aggregation
    positions: List[Dict] = field(default_factory=list)
    open_orders: List[Dict] = field(default_factory=list)
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=200))
    
    processed_trades: Set[int] = field(default_factory=set)
    start_time: float = 0.0 # Will be set on start

state = BotState()
config = TradingConfig()

# --- 3. 交易逻辑模块 ---

class TradingLogger:
    def __init__(self):
        self.logger = logging.getLogger("QuantBot")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False 
        if not self.logger.handlers:
            ch = logging.StreamHandler()
            formatter = logging.Formatter("%(asctime)s - %(message)s", datefmt="%H:%M:%S")
            ch.setFormatter(formatter)
            self.logger.addHandler(ch)

    def log(self, message: str, type: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logger.info(f"[{type}] {message}")
        # Append left ensures newest is at index 0
        state.logs.appendleft({"time": ts, "type": type, "msg": message})

logger = TradingLogger()

class BinanceClient:
    def __init__(self, api_key: str, api_secret: str):
        self.proxies = None
        proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        if proxy_url:
            self.proxies = {'https': proxy_url, 'http': proxy_url}
        self.client = UMFutures(key=api_key, secret=api_secret, proxies=self.proxies)

    def get_listen_key(self):
        return self.client.new_listen_key()["listenKey"]

    def renew_listen_key(self, listen_key):
        return self.client.renew_listen_key(listenKey=listen_key)

    def aggregate_to_minute(self):
        """Aggregate raw equity data to minute-level candles"""
        now = time.time()
        current_minute = int(now // 60) * 60
        
        # Only aggregate once per minute
        if state.last_minute_timestamp == current_minute:
            return
        
        state.last_minute_timestamp = current_minute
        
        if not state.equity_history_raw:
            return
        
        # Calculate OHLC for the minute
        values = [d['value'] for d in state.equity_history_raw]
        if not values:
            return
        
        minute_data = {
            'time': datetime.fromtimestamp(current_minute).strftime("%H:%M"),
            'value': values[-1],  # Close price
            'open': values[0],
            'high': max(values),
            'low': min(values),
            'timestamp': current_minute
        }
        
        state.equity_history.append(minute_data)
    
    def get_market_data(self):
        try:
            acc = self.client.account()
            state.wallet_balance = float(acc.get('totalWalletBalance', 0))
            state.unrealized_pnl = float(acc.get('totalUnrealizedProfit', 0))
            
            positions = self.client.get_position_risk(symbol=config.symbol)
            state.positions = []
            if isinstance(positions, dict): positions = [positions]
            
            for p in positions:
                amt = float(p.get('positionAmt', 0))
                if amt != 0:
                    state.positions.append({
                        'symbol': p.get('symbol', config.symbol),
                        'size': amt,
                        'entry': float(p.get('entryPrice', 0)),
                        'pnl': float(p.get('unRealizedProfit', 0)),
                        'leverage': p.get('leverage', '20')
                    })
            
            orders = self.client.get_orders(symbol=config.symbol)
            state.open_orders = []
            exposure = 0.0
            close_side = 'SELL' if config.direction == 'BUY' else 'BUY'
            
            for o in orders:
                p = float(o.get('price', 0))
                q = float(o.get('origQty', 0))
                val = p * q
                state.open_orders.append({
                    'id': int(o.get('orderId', 0)),
                    'symbol': o.get('symbol', ''),
                    'side': o.get('side', ''),
                    'price': p,
                    'qty': q,
                    'val': val
                })
                if o.get('side') == close_side:
                    exposure += val
            
            state.open_orders.sort(key=lambda x: x['id'], reverse=True)
            state.active_orders_count = len([o for o in state.open_orders if o['side'] == close_side])
            state.total_exposure = exposure
            
            total_equity = state.wallet_balance + state.unrealized_pnl
            
            # Store raw data for aggregation
            state.equity_history_raw.append({
                'time': datetime.now().strftime("%H:%M:%S"),
                'value': total_equity
            })
            
            # Aggregate to minute level
            self.aggregate_to_minute()

        except Exception as e:
            if not state.shutdown_flag:
                print(f"[Data Sync Error] {e}")

class TradingEngine:
    def __init__(self, client: BinanceClient):
        self.client = client
        self.ws_client = None
        self.order_filled_event = threading.Event()
        self.last_fill_data = {}
        self.last_open_time = 0
        self.skip_waiting = False
        self.close_order_id = None
        self.current_ws_symbol = ""
        self.trading_thread = None
        
        self.boot_system()

    def boot_system(self):
        logger.log("Booting System...", "SYSTEM")
        self.start_ws(config.symbol)
        threading.Thread(target=self.data_sync_loop, daemon=True).start()
        logger.log("Data Streams Active.", "SYSTEM")

    def start_ws(self, symbol):
        if self.ws_client: self.ws_client.stop()
        try:
            listen_key = self.client.get_listen_key()
            ws_proxies = {'http': self.client.proxies['http'], 'https': self.client.proxies['https']} if self.client.proxies else None
            
            self.ws_client = UMFuturesWebsocketClient(on_message=self.on_ws_message, proxies=ws_proxies)
            self.ws_client.user_data(listen_key=listen_key)
            self.ws_client.mini_ticker(symbol=symbol.lower())
            
            self.current_ws_symbol = symbol
            
            threading.Thread(target=lambda: (time.sleep(1800) or self.client.renew_listen_key(listen_key)), daemon=True).start()
        except Exception as e:
            logger.log(f"WS Fail: {e}", "ERROR")

    def switch_symbol(self, new_symbol):
        if new_symbol == self.current_ws_symbol: return
        logger.log(f"Switching Feed: {new_symbol}", "INFO")
        self.start_ws(new_symbol)
        threading.Thread(target=self.client.get_market_data, daemon=True).start()

    def activate_trading(self):
        if config.symbol != self.current_ws_symbol:
            self.switch_symbol(config.symbol)

        if not state.running:
            state.running = True
            state.start_time = time.time()
            if not self.trading_thread or not self.trading_thread.is_alive():
                self.trading_thread = threading.Thread(target=self.run_trading_loop, daemon=True)
                self.trading_thread.start()
            logger.log("Trading Engine ENGAGED.", "SYSTEM")

    def on_ws_message(self, _, raw_msg):
        if state.shutdown_flag: return
        try:
            msg = json.loads(raw_msg)
            evt = msg.get('e')
            
            if evt == '24hrMiniTicker':
                state.current_price = float(msg['c'])
            
            elif evt == 'ORDER_TRADE_UPDATE':
                order = msg['o']
                if order['s'] != config.symbol: return
                
                if order['x'] == 'TRADE' and order['X'] == 'FILLED':
                    trade_id = int(order['t'])
                    if trade_id in state.processed_trades: return
                    state.processed_trades.add(trade_id)
                    if len(state.processed_trades) > 2000: state.processed_trades.pop()
                    
                    last_qty = float(order['l'])
                    avg_price = float(order['ap'])

                    if order['S'] == config.direction:
                        self.last_fill_data = {'price': avg_price, 'qty': last_qty}
                        self.order_filled_event.set()
                        if state.running: logger.log(f"Filled Entry: {last_qty} @ {avg_price}", "TRADE")
                    
                    elif order['S'] != config.direction:
                        pnl = config.take_profit * last_qty
                        state.cumulative_pnl += pnl
                        self.skip_waiting = True 
                        if state.running: logger.log(f"Take Profit! +${pnl:.2f}", "PROFIT")

        except Exception: pass

    def data_sync_loop(self):
        while not state.shutdown_flag:
            self.client.get_market_data()
            time.sleep(2) 

    def run_trading_loop(self):
        logger.log("Strategy Loop Active", "SYSTEM")
        while state.running and not state.shutdown_flag:
            try:
                lim_t1a = math.ceil(config.max_orders * 0.14)
                lim_t1b = lim_t1a + math.ceil(config.max_orders * 0.14)
                lim_t1c = lim_t1b + math.ceil(config.max_orders * 0.19)
                lim_t2 = lim_t1c + math.ceil(config.max_orders * 0.14)
                lim_t3 = lim_t2 + math.ceil(config.max_orders * 0.19)

                if self.close_order_id:
                    is_open = any(o['id'] == self.close_order_id for o in state.open_orders)
                    if not is_open: self.close_order_id = None 
                
                cnt = state.active_orders_count
                wait = 0
                can_trade = False
                
                if cnt < lim_t1a:
                    state.current_tier_name = "TIER 1-A [TURBO]"
                    state.tier_limits_display = f"{cnt} / {lim_t1a}"
                    wait = config.wait_t1a
                    can_trade = True
                elif cnt < lim_t1b:
                    state.current_tier_name = "TIER 1-B [FAST]"
                    state.tier_limits_display = f"{cnt} / {lim_t1b}"
                    wait = config.wait_t1b
                    can_trade = True
                elif cnt < lim_t1c:
                    state.current_tier_name = "TIER 1-C [NORMAL]"
                    state.tier_limits_display = f"{cnt} / {lim_t1c}"
                    wait = config.wait_t1c
                    can_trade = True
                elif cnt < lim_t2:
                    state.current_tier_name = "TIER 2 [SAFE]"
                    state.tier_limits_display = f"{cnt} / {lim_t2}"
                    wait = config.wait_t2
                    can_trade = True
                elif cnt < lim_t3:
                    state.current_tier_name = "TIER 3 [DEFENSE]"
                    state.tier_limits_display = f"{cnt} / {lim_t3}"
                    wait = config.wait_t3
                    can_trade = True
                else:
                    state.current_tier_name = "MAX CAP [HOLD]"
                    state.tier_limits_display = f"{cnt} / {config.max_orders}"
                    can_trade = False

                if self.skip_waiting:
                    wait = 0
                    self.skip_waiting = False

                next_ts = self.last_open_time + wait
                diff = max(0, next_ts - time.time())
                state.next_order_countdown = int(diff) if can_trade else 0

                if can_trade and diff == 0:
                    self.execute_trade()
                else:
                    time.sleep(1)

            except Exception as e:
                logger.log(f"Loop Error: {e}", "ERROR")
                time.sleep(5)
        
        logger.log("Trading Stopped", "SYSTEM")
        state.current_tier_name = "IDLE (DATA ONLY)"

    def execute_trade(self):
        try:
            logger.log(f"Placing Order ({state.current_tier_name})...", "ACTION")
            res = self.client.client.new_order(
                symbol=config.symbol, side=config.direction, type='LIMIT', 
                quantity=config.quantity, priceMatch='QUEUE', timeInForce='GTC'
            )
            order_id = res['orderId']
            
            self.order_filled_event.clear()
            is_filled = self.order_filled_event.wait(timeout=10)
            
            fill_price = 0
            fill_qty = 0
            
            if is_filled:
                fill_price = self.last_fill_data['price']
                fill_qty = self.last_fill_data['qty']
                self.last_open_time = time.time()
            else:
                try:
                    self.client.client.cancel_order(symbol=config.symbol, orderId=order_id)
                    logger.log("Order Timeout. Retrying...", "WARNING")
                    return 
                except:
                    fill_price = float(res['price'])
                    fill_qty = config.quantity
                    self.last_open_time = time.time()

            close_side = 'SELL' if config.direction == 'BUY' else 'BUY'
            tp_price = fill_price + config.take_profit if config.direction == 'BUY' else fill_price - config.take_profit
            
            res_close = self.client.client.new_order(
                symbol=config.symbol, side=close_side, type='LIMIT',
                quantity=fill_qty, price=str(round(tp_price, 2)), 
                reduceOnly="true", timeInForce='GTC'
            )
            self.close_order_id = int(res_close['orderId'])
            logger.log(f"TP Set @ {tp_price:.2f}", "INFO")

        except Exception as e:
            logger.log(f"Execution Failed: {e}", "ERROR")
            time.sleep(2)

# --- 4. 前端应用 ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    api_key = os.getenv("API_KEY", "").strip()
    api_secret = os.getenv("API_SECRET", "").strip()
    
    if not api_key:
        print("⚠️ FATAL: API_KEY not found in .env")
        sys.exit(1)
        
    print("🚀 System Booting... (Auto-Connect to Binance)")
    global api_client, bot_engine
    api_client = BinanceClient(api_key, api_secret)
    bot_engine = TradingEngine(api_client)
    
    print(f"✅ Web Interface Ready: http://localhost:8080")
    webbrowser.open("http://localhost:8080")
    
    yield
    
    print("🛑 Shutting down system resources...")
    state.shutdown_flag = True
    state.running = False
    if bot_engine and bot_engine.ws_client:
        bot_engine.ws_client.stop()
    print("👋 Goodbye!")

app = FastAPI(lifespan=lifespan)

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CYBER QUANT V7.3</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Rajdhani:wght@500;700&display=swap" rel="stylesheet">
    <style>
        :root { --neon-blue: #00f3ff; --neon-green: #00ff9d; --neon-pink: #ff0055; --bg-dark: #050505; --glass-bg: rgba(20, 20, 30, 0.6); --glass-border: rgba(255, 255, 255, 0.1); }
        body { background-color: var(--bg-dark); font-family: 'Rajdhani', sans-serif; color: #e0e0e0; overflow: hidden; }
        .font-mono { font-family: 'JetBrains Mono', monospace; }
        .glass-panel { background: var(--glass-bg); backdrop-filter: blur(10px); border: 1px solid var(--glass-border); box-shadow: 0 4px 30px rgba(0, 0, 0, 0.5); }
        
        @media (max-width: 768px) { body { overflow-y: auto; } }

        .glow-text-blue { color: var(--neon-blue); text-shadow: 0 0 8px rgba(0, 243, 255, 0.5); }
        .glow-text-green { color: var(--neon-green); text-shadow: 0 0 8px rgba(0, 255, 157, 0.5); }
        .glow-text-pink { color: var(--neon-pink); text-shadow: 0 0 8px rgba(255, 0, 85, 0.5); }
        
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #0a0a0a; }
        ::-webkit-scrollbar-thumb { background: #444; border-radius: 4px; border: 1px solid #000; }
        ::-webkit-scrollbar-thumb:hover { background: var(--neon-blue); }
        
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        .animate-pulse-slow { animation: pulse 3s infinite; }
        .input-cyber { background: rgba(0,0,0,0.5); border: 1px solid #333; color: var(--neon-blue); font-family: 'JetBrains Mono'; transition: all 0.3s; }
        .input-cyber:focus { border-color: var(--neon-blue); outline: none; box-shadow: 0 0 8px rgba(0, 243, 255, 0.3); }
        
        .drawer-enter-active, .drawer-leave-active { transition: transform 0.3s ease; }
        .drawer-enter-from, .drawer-leave-to { transform: translateX(-100%); }
        .fade-enter-active, .fade-leave-active { transition: opacity 0.3s ease; }
        .fade-enter-from, .fade-leave-to { opacity: 0; }
    </style>
</head>
<body id="app">
    <div class="flex flex-col md:flex-row h-screen w-full">
        <div class="md:hidden h-14 bg-black/80 border-b border-white/10 flex items-center justify-between px-4 shrink-0">
            <div class="flex items-center gap-2">
                <button @click="showMenu = true" class="text-gray-400 hover:text-white"><svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"></path></svg></button>
                <span class="text-lg font-bold text-white tracking-widest">CYBER<span class="text-cyan-400">QUANT</span></span>
            </div>
            <div class="text-xs font-mono text-gray-300">{{ status.current_price.toFixed(2) }}</div>
        </div>

        <div class="fixed inset-0 z-50 md:static md:inset-auto md:w-80 md:h-full flex" :class="{'pointer-events-none': !showMenu && isMobile}">
            <transition name="fade"><div v-if="showMenu && isMobile" @click="showMenu = false" class="absolute inset-0 bg-black/80 backdrop-blur-sm md:hidden"></div></transition>
            <div class="w-72 md:w-full h-full glass-panel border-r-0 flex flex-col bg-[#080a0f] transition-transform duration-300 transform" :class="[showMenu || !isMobile ? 'translate-x-0' : '-translate-x-full']">
                
                <div class="hidden md:block p-6 border-b border-white/10">
                    <div class="flex items-center gap-3">
                        <div class="w-8 h-8 bg-cyan-400 rounded-sm shadow-[0_0_10px_#00f3ff]"></div>
                        <div><h1 class="text-2xl font-bold leading-none tracking-widest text-white">CYBER<span class="text-cyan-400">QUANT</span></h1><p class="text-[10px] text-gray-500 tracking-[0.3em] font-mono mt-1">SYSTEM V7.3</p></div>
                    </div>
                </div>

                <div class="p-4">
                    <div class="glass-panel p-4 bg-black/40">
                        <div class="flex justify-between items-center mb-3">
                            <span class="text-xs font-bold text-gray-500 tracking-wider">ENGINE STATUS</span>
                            <div class="w-2 h-2 rounded-full" :class="running ? 'bg-green-400 shadow-[0_0_8px_#00ff9d]' : 'bg-gray-600'"></div>
                        </div>
                        <div class="grid grid-cols-2 gap-4">
                            <div><p class="text-[9px] text-gray-500 font-mono mb-1">TIER</p><p class="text-xs font-bold glow-text-blue truncate">{{ status.current_tier_name }}</p></div>
                            <div class="text-right"><p class="text-[9px] text-gray-500 font-mono mb-1">CYCLE</p><p class="text-xs font-mono font-bold" :class="status.next_order_countdown > 0 ? 'text-yellow-400' : 'text-gray-600'">{{ status.next_order_countdown }}s</p></div>
                            <div><p class="text-[9px] text-gray-500 font-mono mb-1">PRICE</p><p class="text-sm font-mono text-white">{{ status.current_price.toFixed(2) }}</p></div>
                            <div class="text-right"><p class="text-[9px] text-gray-500 font-mono mb-1">LIMITS</p><p class="text-xs font-mono text-gray-400">{{ status.tier_limits_display }}</p></div>
                        </div>
                    </div>
                </div>

                <div class="flex-1 overflow-y-auto p-4 space-y-4 pb-8">
                    <div class="space-y-1"><label class="text-[10px] text-gray-500 font-bold uppercase">Symbol</label><input v-model="config.symbol" class="w-full input-cyber p-2 text-sm rounded-sm" :disabled="running"></div>
                    <div class="grid grid-cols-2 gap-3">
                        <div class="space-y-1"><label class="text-[10px] text-gray-500 font-bold uppercase">Size</label><input type="number" v-model.number="config.quantity" step="0.001" class="w-full input-cyber p-2 text-sm rounded-sm" :disabled="running"></div>
                        <div class="space-y-1"><label class="text-[10px] text-gray-500 font-bold uppercase">TP ($)</label><input type="number" v-model.number="config.take_profit" step="0.1" class="w-full input-cyber p-2 text-sm rounded-sm" :disabled="running"></div>
                    </div>
                    <div class="space-y-1"><label class="text-[10px] text-gray-500 font-bold uppercase">Max Orders</label><input type="number" v-model.number="config.max_orders" class="w-full input-cyber p-2 text-sm rounded-sm" :disabled="running"></div>
                    
                    <div class="border border-white/20 rounded p-3 bg-black/40">
                        <p class="text-[11px] text-cyan-400 font-bold mb-3 uppercase flex items-center gap-2"><svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg> LIVE STRATEGY DELAYS</p>
                        <div class="grid grid-cols-3 gap-2 mb-3">
                            <div><label class="text-[9px] text-gray-500 block text-center">T1-A</label><input type="number" v-model.number="config.wait_t1a" class="input-cyber p-1 text-xs text-center w-full border-white/30"></div>
                            <div><label class="text-[9px] text-gray-500 block text-center">T1-B</label><input type="number" v-model.number="config.wait_t1b" class="input-cyber p-1 text-xs text-center w-full border-white/30"></div>
                            <div><label class="text-[9px] text-gray-500 block text-center">T1-C</label><input type="number" v-model.number="config.wait_t1c" class="input-cyber p-1 text-xs text-center w-full border-white/30"></div>
                        </div>
                        <div class="grid grid-cols-2 gap-2">
                            <div><label class="text-[9px] text-gray-500 block text-center">TIER 2</label><input type="number" v-model.number="config.wait_t2" class="input-cyber p-1 text-xs text-center w-full border-white/30"></div>
                            <div><label class="text-[9px] text-gray-500 block text-center">TIER 3</label><input type="number" v-model.number="config.wait_t3" class="input-cyber p-1 text-xs text-center w-full border-white/30"></div>
                        </div>
                    </div>
                </div>

                <div class="p-4 border-t border-white/10">
                    <button @click="toggleBot" class="w-full h-10 font-bold text-black bg-[#00F0FF] hover:bg-[#00D4E3] border-none rounded shadow-[0_0_15px_rgba(0,240,255,0.4)] transition-all text-sm tracking-widest uppercase" :disabled="running">{{ running ? 'SYSTEM RUNNING' : 'INITIALIZE' }}</button>
                </div>
            </div>
        </div>

        <div class="flex-1 flex flex-col h-full overflow-hidden relative bg-[#050505]">
            <div class="absolute inset-0 z-0 opacity-10 pointer-events-none" style="background-image: linear-gradient(#1a1a1a 1px, transparent 1px), linear-gradient(90deg, #1a1a1a 1px, transparent 1px); background-size: 40px 40px;"></div>

            <div class="h-auto md:h-20 border-b border-white/10 bg-black/40 backdrop-blur-sm z-10 p-4 md:px-8 shrink-0">
                <div class="grid grid-cols-3 md:flex md:justify-between gap-4 md:gap-8 items-center">
                    <div class="flex flex-col"><span class="text-[9px] font-bold text-gray-500 uppercase">Realized</span><span class="text-sm md:text-2xl font-mono font-bold glow-text-green">${{ status.cumulative_pnl.toFixed(2) }}</span></div>
                    <div class="flex flex-col"><span class="text-[9px] font-bold text-gray-500 uppercase">uPnL</span><span class="text-sm md:text-xl font-mono font-bold" :class="status.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-500'">${{ status.unrealized_pnl.toFixed(2) }}</span></div>
                    <div class="flex flex-col"><span class="text-[9px] font-bold text-gray-500 uppercase">Wallet</span><span class="text-sm md:text-xl font-mono text-white">${{ status.wallet_balance.toFixed(0) }}</span></div>
                    <div class="flex flex-col"><span class="text-[9px] font-bold text-gray-500 uppercase">Exp</span><span class="text-sm md:text-xl font-mono text-cyan-400">${{ status.total_exposure.toFixed(0) }}</span></div>
                    <div class="flex flex-col"><span class="text-[9px] font-bold text-gray-500 uppercase">Orders</span><span class="text-sm md:text-xl font-mono text-gray-300">{{ status.active_orders_count }}</span></div>
                    <div class="hidden md:flex flex-col"><span class="text-[9px] font-bold text-gray-500 uppercase">Uptime</span><span class="text-sm font-mono text-gray-500">{{ status.uptime_display }}</span></div>
                </div>
            </div>

            <div class="flex-1 flex flex-col p-2 md:p-4 gap-2 md:gap-4 overflow-hidden z-10">
                <div class="h-[250px] md:flex-[2] glass-panel relative p-0 overflow-hidden shrink-0"><div class="absolute top-3 left-4 z-10 text-[10px] font-bold text-gray-500 tracking-wider">LIVE EQUITY</div><div id="chart" class="w-full h-full"></div></div>
                <div class="flex-1 flex flex-col md:flex-row gap-2 md:gap-4 min-h-0">
                    
                    <div class="flex-[3] md:w-2/3 glass-panel flex flex-col overflow-hidden">
                        <div class="flex border-b border-white/10 bg-black/20"><button @click="activeTab='positions'" :class="activeTab==='positions'?'text-cyan-400 border-b-2 border-cyan-400 bg-white/5':''" class="flex-1 py-2 text-[10px] md:text-xs font-bold text-gray-500 hover:text-white transition">POSITIONS</button><button @click="activeTab='orders'" :class="activeTab==='orders'?'text-cyan-400 border-b-2 border-cyan-400 bg-white/5':''" class="flex-1 py-2 text-[10px] md:text-xs font-bold text-gray-500 hover:text-white transition">ORDERS</button></div>
                        <div class="flex-1 overflow-auto bg-black/20">
                            <table class="w-full text-left border-collapse">
                                <thead class="sticky top-0 bg-[#0a0a0a] text-[9px] md:text-[10px] text-gray-500 font-mono uppercase z-10"><tr v-if="activeTab==='positions'"><th class="p-2 pl-3">Sym</th><th class="p-2 text-right">Sz</th><th class="p-2 text-right">Ent</th><th class="p-2 text-right pr-3">PnL</th></tr><tr v-else><th class="p-2 pl-3 w-16">Sym</th><th class="p-2 w-24">ID</th><th class="p-2 text-center w-16">Side</th><th class="p-2 text-right">Price</th><th class="p-2 text-right pr-3">Qty</th></tr></thead>
                                <tbody class="text-[10px] md:text-xs font-mono text-gray-300 divide-y divide-white/5"><tr v-if="activeTab==='positions'" v-for="p in status.positions" :key="p.symbol"><td class="p-2 pl-3">{{ p.symbol }}</td><td class="p-2 text-right">{{ p.size }}</td><td class="p-2 text-right text-gray-400">{{ p.entry.toFixed(2) }}</td><td class="p-2 text-right pr-3 font-bold" :class="p.pnl>=0?'text-green-400':'text-red-500'">{{ p.pnl.toFixed(2) }}</td></tr><tr v-else v-for="o in status.open_orders" :key="o.id"><td class="p-2 pl-3 text-cyan-400 font-bold">{{ o.symbol }}</td><td class="p-2 text-gray-500">{{ o.id }}</td><td class="p-2 text-center" :class="o.side==='BUY'?'text-green-400':'text-red-400'">{{ o.side }}</td><td class="p-2 text-right text-cyan-400">{{ o.price.toFixed(2) }}</td><td class="p-2 text-right pr-3">{{ o.qty }}</td></tr></tbody>
                            </table>
                        </div>
                    </div>
                    <div class="h-32 md:h-auto md:w-1/3 glass-panel flex flex-col bg-black">
                        <div class="px-3 py-1 border-b border-white/10 bg-white/5 flex justify-between items-center"><span class="text-[10px] font-bold text-gray-500 tracking-widest">> LOGS</span><div class="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-pulse"></div></div>
                        <div class="flex-1 overflow-auto p-2 font-mono text-[9px] space-y-1" id="log-container"><div v-for="(log, i) in status.logs" :key="i" class="break-words"><span class="text-gray-600">[{{ log.time }}]</span><span :class="{'text-green-400': log.type === 'PROFIT' || log.type === 'TRADE','text-red-500': log.type === 'ERROR','text-cyan-400': log.type === 'ACTION' || log.type === 'INFO','text-gray-400': log.type === 'SYSTEM'}" class="font-bold ml-1">{{ log.type }}:</span><span class="text-gray-300 ml-1">{{ log.msg }}</span></div></div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script>
        const { createApp, ref, onMounted, onUnmounted, watch, nextTick, computed } = Vue;
        createApp({
            setup() {
                const running = ref(false);
                const activeTab = ref('positions');
                const showMenu = ref(false);
                const windowWidth = ref(window.innerWidth);
                const status = ref({ current_price: 0, cumulative_pnl: 0, unrealized_pnl: 0, wallet_balance: 0, active_orders_count: 0, total_exposure: 0, next_order_countdown: 0, current_tier_name: 'OFFLINE', tier_limits_display: '--/--', uptime_display: '--:--', positions: [], open_orders: [], logs: [], equity_history: [] });
                const config = ref({ symbol: 'ETHUSDC', quantity: 0.01, take_profit: 1.0, max_orders: 210, wait_t1a: 15, wait_t1b: 30, wait_t1c: 60, wait_t2: 180, wait_t3: 600 });
                let socket = null, chartInstance = null, debounceTimer = null;
                const isMobile = computed(() => windowWidth.value < 768);
                const onResize = () => { windowWidth.value = window.innerWidth; if (chartInstance) chartInstance.resize(); if (!isMobile.value) showMenu.value = false; };
                const initChart = () => { chartInstance = echarts.init(document.getElementById('chart')); chartInstance.setOption({ backgroundColor: 'transparent', animation: false, grid: { top: 30, right: 10, bottom: 25, left: 45 }, tooltip: { trigger: 'axis', backgroundColor: 'rgba(0,0,0,0.8)', borderColor: '#333', textStyle: {color: '#eee'} }, xAxis: { type: 'category', data: [], axisLine: { lineStyle: { color: '#333' } }, axisLabel: { color: '#666' } }, yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#222' } }, axisLabel: { color: '#666' } }, series: [{ type: 'line', data: [], smooth: 0.2, showSymbol: false, lineStyle: { color: '#00ff9d', width: 2 }, areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{offset: 0, color: 'rgba(0, 255, 157, 0.2)'}, {offset: 1, color: 'rgba(0, 255, 157, 0)'}]) } }], dataZoom: [ { type: 'inside' }, { type: 'slider', bottom: 0, height: 20, borderColor: '#333', fillerColor: 'rgba(0, 243, 255, 0.2)', textStyle: {color: '#666'}, handleStyle: {color: '#00f3ff'} } ] }); };
                const connectWS = () => { const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'; socket = new WebSocket(`${protocol}//${window.location.host}/ws`); socket.onmessage = (event) => { const data = JSON.parse(event.data); status.value = data; running.value = data.running; if (data.equity_history && data.equity_history.length > 0) { chartInstance.setOption({ xAxis: { data: data.equity_history.map(d => d.time) }, series: [{ data: data.equity_history.map(d => d.value) }] }); } }; socket.onclose = () => setTimeout(connectWS, 1000); };
                const toggleBot = async () => { const res = await fetch('/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config.value) }); if (!res.ok) alert("Error"); if (isMobile.value) showMenu.value = false; };
                const updateConfig = async () => { await fetch('/update_config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config.value) }); };
                // Live Tuning Watchers
                watch(() => config.value.wait_t1a, () => updateConfig());
                watch(() => config.value.wait_t1b, () => updateConfig());
                watch(() => config.value.wait_t1c, () => updateConfig());
                watch(() => config.value.wait_t2, () => updateConfig());
                watch(() => config.value.wait_t3, () => updateConfig());
                watch(() => config.value.symbol, () => { clearTimeout(debounceTimer); debounceTimer = setTimeout(() => updateConfig(), 500); });
                // FIX: Smart Scroll (Force top on update)
                watch(() => status.value.logs, () => { nextTick(() => { const el = document.getElementById('log-container'); if (el) el.scrollTop = 0; }); }, { deep: true });
                onMounted(() => { initChart(); connectWS(); window.addEventListener('resize', onResize); });
                onUnmounted(() => window.removeEventListener('resize', onResize));
                return { running, activeTab, status, config, toggleBot, showMenu, isMobile };
            }
        }).mount('#app');
    </script>
</body>
</html>
"""

@app.get("/")
async def get_ui(): return HTMLResponse(html_content)

@app.post("/update_config")
async def update_config_endpoint(cfg: dict):
    config.symbol = cfg['symbol']
    config.wait_t1a = int(cfg['wait_t1a']); config.wait_t1b = int(cfg['wait_t1b']); config.wait_t1c = int(cfg['wait_t1c'])
    config.wait_t2 = int(cfg['wait_t2']); config.wait_t3 = int(cfg['wait_t3'])
    
    if bot_engine: bot_engine.switch_symbol(config.symbol)
    return {"status": "updated"}

@app.post("/start")
async def start_bot(cfg: dict):
    global api_client, bot_engine
    if not api_client: return {"status": "error", "msg": "System Init Failed"}
    config.symbol = cfg['symbol']; config.quantity = float(cfg['quantity']); config.take_profit = float(cfg['take_profit']); config.max_orders = int(cfg['max_orders'])
    config.wait_t1a = int(cfg['wait_t1a']); config.wait_t1b = int(cfg['wait_t1b']); config.wait_t1c = int(cfg['wait_t1c']); config.wait_t2 = int(cfg['wait_t2']); config.wait_t3 = int(cfg['wait_t3'])
    bot_engine.activate_trading()
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Calc Uptime
            uptime_str = "--:--"
            if state.running and state.start_time > 0:
                delta = int(time.time() - state.start_time)
                h, r = divmod(delta, 3600)
                m, s = divmod(r, 60)
                uptime_str = f"{h:02}:{m:02}:{s:02}"

            # Send only last 1440 minutes (24 hours) for chart display, but keep 30 days in backend
            equity_display = list(state.equity_history)[-1440:] if len(state.equity_history) > 1440 else list(state.equity_history)
            
            data = { "running": state.running, "current_price": state.current_price, "active_orders_count": state.active_orders_count, "cumulative_pnl": state.cumulative_pnl, "total_exposure": state.total_exposure, "current_tier_name": state.current_tier_name, "next_order_countdown": state.next_order_countdown, "tier_limits_display": state.tier_limits_display, "wallet_balance": state.wallet_balance, "unrealized_pnl": state.unrealized_pnl, "equity_history": equity_display, "positions": state.positions, "open_orders": state.open_orders, "logs": list(state.logs), "uptime_display": uptime_str, "total_data_points": len(state.equity_history) }
            await websocket.send_json(data)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect: pass

if __name__ == "__main__":
    api_key = os.getenv("API_KEY", "").strip()
    api_secret = os.getenv("API_SECRET", "").strip()
    if not api_key:
        print("⚠️ FATAL: API_KEY not found in .env")
        sys.exit(1)
        
    print("🚀 System Booting... (Auto-Connect to Binance)")
    api_client = BinanceClient(api_key, api_secret)
    bot_engine = TradingEngine(api_client) # Starts data stream immediately
    
    print(f"✅ Web Interface Ready: http://localhost:8080")
    # FIX: Open browser only once here
    webbrowser.open("http://localhost:8080")
    # FIX: Force exit after uvicorn finishes
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
    os._exit(0)