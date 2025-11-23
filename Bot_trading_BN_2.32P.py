import os, sys, json, time, threading, contextlib, io, re

# --- keys.json loader ---
def ensure_keys_file():
    path = os.path.join(os.path.dirname(__file__),'data','keys.json')
    os.makedirs(os.path.join(os.path.dirname(__file__),'data'), exist_ok=True)
    if not os.path.exists(path):
        with open(path,'w') as f:
            json.dump({
                "API_KEY_TEST":"",
                "API_SECRET_TEST":"",
                "API_KEY_LIVE":"",
                "API_SECRET_LIVE":""
            },f,indent=4)
    with open(path) as f:
        keys=json.load(f)
    return keys
keys=ensure_keys_file()
API_KEY_TEST=keys.get("API_KEY_TEST","")
API_SECRET_TEST=keys.get("API_SECRET_TEST","")
API_KEY_LIVE=keys.get("API_KEY_LIVE","")
API_SECRET_LIVE=keys.get("API_SECRET_LIVE","")

from datetime import datetime
import curses, pytz
try:
    import ccxt, tenacity
except Exception:
    ccxt = None
    tenacity = None
try:
    import plotext as plt
except Exception:
    plt = None

class NasroClient:
    def __init__(self):
        self.exchange = ccxt.binance({
            'apiKey': API_KEY_TEST,
            'secret': API_SECRET_TEST,
            'enableRateLimit': True,
            'timeout': 300000,
            'urls': {
                'api': {
                    'public': 'https://testnet.binance.vision/api',
                    'private': 'https://testnet.binance.vision/api'
                }
            }
        }) if ccxt else None

        self.exchange_live = ccxt.binance({
            'enableRateLimit': True,
            'timeout': 15000
        }) if ccxt else None
        self.symbols = ['BTC/USDT', 'ETH/USDT', 'LTC/USDT']
        self.current_symbol = 0
        self.balance = {'USDT': 1000}
        for symbol in self.symbols:
            base, _ = symbol.split('/')
            self.balance[base] = 0
        self.open_orders = []
        self.tickers = {}
        self.pnl = 0
        self.ACTIONS = ["1. Update", "2. Trading", "3. Add Coin", "4. Delete Coin", "5. Cancel"]
        self.status = "Ready..."
        self.last_update = datetime.now(pytz.timezone('Africa/Algiers')).strftime("%Y-%m-%d %H:%M:%S")
        self.data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.cached_plot_lines = {}
        self.plot_refresh_interval = 30.0   # rebuild plot every 30s (was 5s)
        self.candles_fetch_interval = 30.0  # fetch candles every 30s (was 15s)
        self.mini_chart_height = 30
        self.fullscreen_candles = 16
        self._ansi_re = re.compile(r'\x1b\[[0-9;]*m')
        self.load_data()
        self.waiting = True
    # -------------------------
    # Utilities / I/O
    # -------------------------
    def get_version(self):
        filename = os.path.basename(__file__)
        version = filename.split("_")[-1].split(".py")[0]
        return f"v{version}"
    def set_status(self, s):
        self.status = s
    def strip_ansi(self, text):
        return self._ansi_re.sub('', text)
    def data_json_path(self):
        return os.path.join(os.path.dirname(__file__), 'data/data.json')
    def candles_file_path(self, symbol):
        return os.path.join(self.data_dir, f"{symbol.replace('/','_')}_candles.json")
    def load_data(self):
        p = self.data_json_path()
        if not os.path.exists(p):
            self.set_status("No saved data")
            return
        try:
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data.get('symbols'), list):
                self.symbols = data['symbols']
            if isinstance(data.get('balance'), dict):
                self.balance = data['balance']
            if isinstance(data.get('open_orders'), list):
                self.open_orders = data['open_orders']
            self.tickers = data.get('tickers', {}) or {}
            self.pnl = data.get('pnl', self.pnl)
            self.last_update = data.get('last_update', self.last_update)
            self.set_status("Ok load data")
        except Exception as e:
            self.set_status(f"Error load data: {e}")
            time.sleep(0.2)
    def save_data(self):
        data = {}
        if self.symbols: data['symbols'] = self.symbols
        if self.balance: data['balance'] = self.balance
        if self.open_orders: data['open_orders'] = self.open_orders
        if self.tickers: data['tickers'] = self.tickers
        if self.pnl is not None: data['pnl'] = self.pnl
        if self.last_update: data['last_update'] = self.last_update
        try:
            with open(self.data_json_path(), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            self.set_status("Ok save data")
        except Exception as e:
            self.set_status(f"Error save data: {e}")
            time.sleep(0.2)
    # -------------------------
    # Candles fetch/load/save
    # -------------------------
    def fetch_candles(self, symbol):
        if not self.exchange:
            return []
        try:
            self.set_status(f"Fetching candles {symbol}...")
            data = self.exchange.fetch_ohlcv(symbol, timeframe="15m", limit=max(self.fullscreen_candles, 16))
            candles = []
            for c in data:
                candles.append({
                    "timestamp": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                })
            self.set_status(f"Ok candles {symbol}")
            return candles
        except Exception as e:
            self.set_status(f"Error fetch candles: {e}")
            return []
    def load_candles(self, symbol):
        p = self.candles_file_path(symbol)
        if not os.path.exists(p):
            return []
        try:
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    def save_candles(self, symbol, candles):
        try:
            with open(self.candles_file_path(symbol), 'w', encoding='utf-8') as f:
                json.dump(candles, f, ensure_ascii=False, indent=4)
        except Exception:
            pass
    # -------------------------
    # Ticker with retry
    # -------------------------
    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=1, min=100, max=3000)) if tenacity else (lambda f: f)
    def get_ticker(self, symbol):
        if not self.exchange:
            return None
        try:
            self.set_status(f"Getting ticker {symbol}...")
            ticker = self.exchange.fetch_ticker(symbol)
            self.set_status(f"Ok get ticker {symbol}")
            time.sleep(10)
            return ticker
        except Exception as e:
            self.set_status(f"Error get ticker {symbol}: {e}")
            time.sleep(0.2)
            return None
    # -------------------------
    # Plotext capture + build plot lines (mini+full)
    # -------------------------
    def render_plotext_to_lines(self):
        if plt is None:
            return ["plotext not installed"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                plt.show()
            except Exception:
                pass
        out = buf.getvalue().splitlines()
        return [self.strip_ansi(line) for line in out]
    def _is_axis_tick_line(self, line):
        # return True if line contains mostly numbers/dots/minus/spaces (likely the x-axis tick line)
        if not line or line.strip() == "":
            return False
        s = line.strip()
        allow = set("0123456789- .,")
        count_allowed = sum(1 for ch in s if ch in allow)
        ratio = count_allowed / max(1, len(s))
        return ratio > 0.7 and len(s) > 3
    def _format_timestamp(self, ts):
        try:
            # تحويل من ميلي ثانية لثواني
            if ts > 1_000_000_000_000:
                ts = ts / 1000
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        except:
            return str(ts)
    def build_plot_lines(self, symbol, candles, height, width, force_exact_ylim=False):
        CANDLE_BODY_HALF_WIDTH = 0.3
        BULL_COLOR = "green"
        BEAR_COLOR = "red"
    
        now = time.time()
        cache_key = (symbol, force_exact_ylim, height, width)
        cache = self.cached_plot_lines.get(cache_key)
    
        if cache and now - cache["ts"] < self.plot_refresh_interval:
            return cache["lines"]
    
        if plt is None:
            lines = ["plotext not installed"]
            self.cached_plot_lines[cache_key] = {
                "lines": lines,
                "ts": now,
                "last_close": None
            }
            return lines
    
        try:
            plt.clf()
    
            # 🔥 منع الاهتزاز (زيادة بسيطة للوحة فقط)
            plt.plotsize(width + 1, height)
    
            # 🔥 إزالة جميع الحدود
            try:
                plt.theme("plain")
                plt.frame(False)
                plt.padding(0, 0)
                plt.margin(0, 0)
            except:
                pass
    
            # إخفاء محور X في المصغر فقط
            if not force_exact_ylim:
                try:
                    plt.xticks([])
                except:
                    pass
    
            # بيانات الشموع
            dates  = list(range(len(candles)))
            opens  = [c["open"]  for c in candles] if candles else []
            highs  = [c["high"]  for c in candles] if candles else []
            lows   = [c["low"]   for c in candles] if candles else []
            closes = [c["close"] for c in candles] if candles else []
    
            # timestamps
            timestamps = [c["timestamp"] for c in candles] if candles else []
    
            # 🔥 نطاق Y بدون padding
            if highs and lows:
                plt.ylim(min(lows), max(highs))
    
            # رسم الشموع
            for i in range(len(dates)):
                x = dates[i]
                o = opens[i]; h = highs[i]; l = lows[i]; c = closes[i]
                color = BEAR_COLOR if o > c else BULL_COLOR
    
                plt.plot([x, x], [c, h], color=color)
                plt.plot([x, x], [l, o], color=color)
    
                left  = x - CANDLE_BODY_HALF_WIDTH
                right = x + CANDLE_BODY_HALF_WIDTH
    
                plt.plot([left, right], [o, o], color=color)
                plt.plot([left, right], [c, c], color=color)
                plt.plot([left, left], [o, c], color=color)
                plt.plot([right, right], [o, c], color=color)
    
            # تحويل الرسم النصي
            raw_lines = self.render_plotext_to_lines()
    
            # إزالة الأسطر الفارغة العلوية
            idx = 0
            while idx < len(raw_lines) and raw_lines[idx].strip() == "":
                idx += 1
            cleaned = raw_lines[idx:]
    
            # حذف محور X من المصغر
            cleaned2 = []
            for ln in cleaned:
                if self._is_axis_tick_line(ln):
                    if force_exact_ylim:
                        cleaned2.append(ln)
                    else:
                        continue
                else:
                    cleaned2.append(ln)
    
            # 🔥 نأخذ أعلى الرسم فقط
            if len(cleaned2) >= height - 1:
                cropped = cleaned2[:height - 1]   # نترك سطر للتاريخ
            else:
                cropped = cleaned2 + [" " * (width + 4)] * ((height - 1) - len(cleaned2))
    
            # --------- 🔥 إضافة سطر التاريخ تحت الرسم ---------
            if timestamps:
                import datetime
                # عدد الشموع الظاهرة في الرسم (آخر n)
                n = len(cropped)
                visible_ts = timestamps[-n:]
    
                time_labels = []
                for ts in visible_ts:
                    # استخدام الدالة الخاصة بك
                    time_labels.append(self._format_timestamp(ts)[11:16])  # فقط HH:MM
    
                time_line = " ".join(time_labels)
                time_line = time_line[:width + 4]
            else:
                time_line = ""
    
            cropped.append(time_line)
    
            # ضبط عرض الخطوط
            final_lines = [line[:width + 4] for line in cropped]
    
        except Exception as e:
            final_lines = [f"Plot error: {e}"]
    
        last_close = candles[-1]["close"] if candles else None
    
        self.cached_plot_lines[cache_key] = {
            "lines": final_lines,
            "ts": now,
            "last_close": last_close
        }
    
        return final_lines

    def display_fullscreen_chart(self, stdscr, symbol, candles):
        """
        Display a fullscreen chart for `symbol`.
        - Uses self.fullscreen_candles candles (already prepared by caller)
        - force_exact_ylim=True to make high touch top and low touch bottom
        - Q to quit back
        """
        # prepare candles slice (last N)
        candles = candles[-self.fullscreen_candles:] if candles else []
        # invalidate cache for fullscreen to reflect exact ylim
        self.cached_plot_lines.pop((symbol, True), None)
        while True:
            max_h, max_w = stdscr.getmaxyx()
            stdscr.clear()
            stdscr.addstr(0, 0, f"★ Full Screen Chart: {symbol} (press q to return)")
            stdscr.addstr(1, 0, "-" * max(2, max_w - 1))
            # compute plot area: leave 3 rows for header/status
            plot_h = max(6, max_h - 3)
            plot_w = max(20, max_w - 3)
            plot_lines = self.build_plot_lines(symbol, candles, plot_h, plot_w, force_exact_ylim=True)
            left = max(0, (max_w - plot_w) // 2)
            for i in range(min(plot_h, len(plot_lines))):
                try:
                    stdscr.addstr(2 + i, left, plot_lines[i][:max(0, max_w - left - 1)])
                except Exception:
                    pass
            # status at bottom
            try:
                stdscr.addstr(max_h - 1, 0, self.status[:max(0, max_w - 1)])
            except Exception:
                pass
            stdscr.refresh()
            stdscr.timeout(300)
            key = stdscr.getch()
            if key == ord('q') or key == ord('Q'):
                break
            # allow resize / periodic refresh: if candles updated externally, force rebuild
            # (caller is responsible for updating cached_plot_lines invalidation)
    # -------------------------
    # Original UI functions (preserved) with safe improvements
    # -------------------------
    def display_symbols(self, stdscr):
        max_h, max_w = stdscr.getmaxyx()
        stdscr.clear()
        stdscr.addstr(0, 0, "<<<<<<<<<<<<<<<<<<<<<<< Private Trading Program >>>>>>>>>>>>>>>>>>>>>>>>")
        stdscr.addstr(1, 0, f"Balance: {self.balance.get('USDT',0):.2f} USDT | PNL: {self.pnl}")
        version = self.get_version()
        try:
            stdscr.addstr(1, max(0, max_w - (12 + len(version))), f"By dj_nasro {version}")
        except Exception:
            pass
        stdscr.addstr(2, 0, f"Open Orders: {len(self.open_orders)}")
        try:
            stdscr.addstr(2, max(0, max_w - 32), f"Last Update: {self.last_update}")
        except Exception:
            pass
        stdscr.addstr(3, 0, "------------------------------------------------------------------------")
        for i, symbol in enumerate(self.symbols):
            price = self.tickers.get(symbol, {}).get('last', 0)
            pct = self.tickers.get(symbol, {}).get('percentage', 0) or 0.0
            qty = self.balance.get(symbol.split('/')[0], 0)
            line = f"{symbol} | Price: {price} | Change: {pct:.2f}% | Quantity: {qty}"
            try:
                if i == self.current_symbol:
                    stdscr.addstr(i + 4, 0, f"> {line}"[:max(0, max_w - 1)], curses.A_REVERSE)
                else:
                    stdscr.addstr(i + 4, 0, f"  {line}"[:max(0, max_w - 1)])
            except Exception:
                pass
        # hint for R key
        try:
            stdscr.addstr(max_h - 3, 0, "Press 'r' to open full-screen chart for selected symbol.")
        except Exception:
            pass
        # status: always bottom row to avoid keyboard overlay hiding it
        try:
            row = max_h - 1
            stdscr.addstr(row, 0, self.status[:max(0, max_w - 1)])
        except Exception:
            pass
        curses.update_lines_cols()
        stdscr.refresh()
    def update_ticker_background(self, symbol):
        # background updater for a single symbol (does not write to stdscr)
        while True:
            t = self.get_ticker(symbol)
            if t is not None:
                self.tickers[symbol] = t
                self.last_update = datetime.now(pytz.timezone('Africa/Algiers')).strftime("%Y-%m-%d %H:%M:%S")
            time.sleep(10)  # reduced frequency to 10s to lower load
    def display_trading_menu(self, stdscr, ticker):
        symbol = self.symbols[self.current_symbol]
        candles = self.load_candles(symbol)
        fetched = self.fetch_candles(symbol)
        if fetched:
            candles = fetched[-16:]
            self.save_candles(symbol, candles)
        self.cached_plot_lines.pop((symbol, False), None)
        self.cached_plot_lines.pop((symbol, True), None)
        type_f = ''
        quantity = ''
        price_f = ''
        tp = ''
        sl = ''
        current_field = 0
        open_orders_index = 0
        status_local = 'Ready...'
        last_price_update = 0
        last_candles_update = 0
        while True:
            max_h, max_w = stdscr.getmaxyx()
            stdscr.clear()
            # ---------------- HEADER ----------------
            stdscr.addstr(0, 0, f"                              ★ TRADING ★")
            ba = f"Balance: {self.balance.get('USDT',0):.2f} USDT"
            try:
                stdscr.addstr(0, max(0, max_w - len(ba) - 1), ba)
            except Exception:
                pass
            stdscr.addstr(1, 0, "------------------------------------------------------------------------")
            last_price = ticker.get('last', 0) if isinstance(ticker, dict) else 0
            last_pct = ticker.get('percentage', 0) if isinstance(ticker, dict) else 0
            stdscr.addstr(
                2, 0,
                f"{symbol}: {last_price} | {last_pct:.2f}% | Quantity: {self.balance.get(symbol.split('/')[0],0)}"
            )
            stdscr.addstr(3, 0, "------------------------------------------------------------------------")
            # ---------------- CHART POSITION ----------------
            info_height = 4
            plot_h = self.mini_chart_height       # now
            plot_w = max(20, max_w - 6)
            base_row = info_height                # chart begins right under symbol info
            # ---------------- PLOT GENERATION ----------------
            plot_lines = self.build_plot_lines(
                symbol, candles, plot_h, plot_w,
                force_exact_ylim=True
            )
            # ---------------- DRAW MINI-CHART ----------------
            for i, line in enumerate(plot_lines):
                row = base_row + i
                if row >= curses.LINES - 10:
                    break
                try:
                    stdscr.addstr(row, 0, line[:max_w - 4])
                except Exception:
                    pass
            # ---------------- INPUT FIELDS ----------------
            input_row = base_row + plot_h + 1
            stdscr.addstr(input_row - 1, 0, "------------------------------------------------------------------------")
            fields = [
                f"Enter type b/s:{type_f}",
                f"Enter quantity:{quantity}",
                f"Enter price:{price_f}",
                f"Enter TP:{tp}",
                f"Enter SL:{sl}",
                "ENTER"
            ]
            for fi, text in enumerate(fields):
                label = "> " + text if current_field == fi else "  " + text
                try:
                    stdscr.addstr(input_row + fi, 0, label)
                except:
                    pass
            stdscr.addstr(input_row + 6, 0, "------------------------------------------------------------------------")
            # ---------------- ORDERS LIST ----------------
            stdscr.addstr(input_row + 7, 0, f"Open Orders: {len(self.open_orders)}")
            for i, order in enumerate(self.open_orders):
                row = input_row + 8 + i
                if row >= curses.LINES - 2:
                    break
                text = f"{i+1}. {order['side']} {order['amount']} {order['symbol']} | {order['price']}"
                if i == open_orders_index:
                    try:
                        stdscr.addstr(row, 0, "> " + text, curses.A_REVERSE)
                    except:
                        pass
                else:
                    try:
                        stdscr.addstr(row, 0, "  " + text)
                    except:
                        pass
            # ---------------- R KEY HINT ----------------
            try:
                stdscr.addstr(input_row + 1, max(0, max_w - 40), "Press 'r' to open full-screen chart")
            except:
                pass
            # ---------------- STATUS BOTTOM ----------------
            try:
                stdscr.addstr(max_h - 1, 0, self.status[:max_w - 1])
            except:
                pass
            stdscr.refresh()
            # ---------------- UPDATES ----------------
            now = time.time()
            if now - last_price_update >= 10:
                nt = self.get_ticker(symbol)
                if nt:
                    ticker = nt
                    self.tickers[symbol] = nt
                    self.last_update = datetime.now(pytz.timezone('Africa/Algiers')).strftime("%Y-%m-%d %H:%M:%S")
                    self.cached_plot_lines.pop((symbol, False), None)
                    self.cached_plot_lines.pop((symbol, True), None)
                last_price_update = now
            if now - last_candles_update >= self.candles_fetch_interval:
                fetched = self.fetch_candles(symbol)
                if fetched:
                    prev_last = candles[-1]['close'] if candles else None
                    candles = fetched[-max(self.fullscreen_candles, 16):]
                    self.save_candles(symbol, candles)
                    new_last = candles[-1]['close'] if candles else None
                    if prev_last != new_last:
                        self.cached_plot_lines.pop((symbol, False), None)
                        self.cached_plot_lines.pop((symbol, True), None)
                last_candles_update = now
            # ---------------- INPUT HANDLING ----------------
            stdscr.timeout(200)
            key = stdscr.getch()
            if key == curses.KEY_UP:
                if current_field > 0:
                    current_field -= 1
                elif open_orders_index > 0:
                    open_orders_index -= 1
            elif key == curses.KEY_DOWN:
                if current_field < 5:
                    current_field += 1
                elif open_orders_index < len(self.open_orders) - 1:
                    open_orders_index += 1
            elif key in (ord('b'), ord('s')):
                if current_field == 0:
                    type_f = chr(key)
            elif key in range(ord('0'), ord('9') + 1) or key == ord('.'):
                if current_field == 1:
                    quantity += chr(key)
                elif current_field == 2:
                    price_f += chr(key)
                elif current_field == 3:
                    tp += chr(key)
                elif current_field == 4:
                    sl += chr(key)
            elif key == 263:
                if current_field == 0:
                    type_f = type_f[:-1]
                elif current_field == 1:
                    quantity = quantity[:-1]
                elif current_field == 2:
                    price_f = price_f[:-1]
                elif current_field == 3:
                    tp = tp[:-1]
                elif current_field == 4:
                    sl = sl[:-1]
            elif key == 10:
                if current_field == 5:
                    if type_f and quantity and price_f:
                        try:
                            self.place_limit_order(type_f, float(quantity), float(price_f), symbol)
                            self.set_status(f"{type_f} order for {symbol} added successfully")
                            self.save_data()
                            self.cached_plot_lines.pop((symbol, False), None)
                            self.cached_plot_lines.pop((symbol, True), None)
                        except Exception as e:
                            self.set_status(f"Failed: {e}")
                        type_f = quantity = price_f = tp = sl = ''
                    else:
                        self.set_status("Please fill all fields")
            elif key == ord('c'):
                if open_orders_index < len(self.open_orders):
                    order = self.open_orders[open_orders_index]
                    options = ["Edit order", "Delete order", "Delete all", "Cancel"]
                    option_index = 0
                    while True:
                        stdscr.clear()
                        stdscr.addstr(0, 0, "Options:")
                        for i, option in enumerate(options):
                            if i == option_index:
                                stdscr.addstr(i + 1, 0, f"> {option}")
                            else:
                                stdscr.addstr(i + 1, 0, f"  {option}")
                        stdscr.refresh()
                        key2 = stdscr.getch()
                        if key2 == curses.KEY_UP:
                            option_index = (option_index - 1) % len(options)
                        elif key2 == curses.KEY_DOWN:
                            option_index = (option_index + 1) % len(options)
                        elif key2 == 10:
                            if option_index == 0:
                                # Edit order (preserve original behavior)
                                ticker_loc = self.tickers.get(order['symbol'])
                                type_f = order['side']
                                quantity = str(order['amount'])
                                price_f = str(order['price'])
                                tp = str(order.get('tp', ''))
                                sl = str(order.get('sl', ''))
                                current_field = 0
                                while True:
                                    stdscr.clear()
                                    stdscr.addstr(0, 0, f"                             ★ EDIT ORDER ★")
                                    ba = f"Balance: {self.balance['USDT']:.2f} USDT"
                                    try:
                                        stdscr.addstr(0, max(0, max_w - len(ba) - 1), ba)
                                    except Exception:
                                        pass
                                    stdscr.addstr(1, 0, "------------------------------------------------------------------------")
                                    ticker_loc = self.tickers.get(order['symbol'])
                                    if ticker_loc is not None:
                                        stdscr.addstr(2, 0, f"{order['symbol']}: {ticker_loc.get('last', 0)} | {ticker_loc.get('percentage', 0) or 0:.2f}%")
                                    stdscr.addstr(3, 0, "------------------------------------------------------------------------")
                                    if current_field == 0:
                                        stdscr.addstr(4, 0, f"> Enter type b/s:{type_f}")
                                    else:
                                        stdscr.addstr(4, 0, f"  Enter type b/s:{type_f}")
                                    if current_field == 1:
                                        stdscr.addstr(5, 0, f"> Enter quantity:{quantity}")
                                    else:
                                        stdscr.addstr(5, 0, f"  Enter quantity:{quantity}")
                                    if current_field == 2:
                                        stdscr.addstr(6, 0, f"> Enter price:{price_f}")
                                    else:
                                        stdscr.addstr(6, 0, f"  Enter price:{price_f}")
                                    if current_field == 3:
                                        stdscr.addstr(7, 0, f"> Enter TP:{tp}")
                                    else:
                                        stdscr.addstr(7, 0, f"  Enter TP:{tp}")
                                    if current_field == 4:
                                        stdscr.addstr(8, 0, f"> Enter SL:{sl}")
                                    else:
                                        stdscr.addstr(8, 0, f"  Enter SL:{sl}")
                                    if current_field == 5:
                                        stdscr.addstr(9, 0, f"> ENTER")
                                    else:
                                        stdscr.addstr(9, 0, f"  ENTER")
                                    stdscr.addstr(max_h - 2, 0, status_local)
                                    stdscr.refresh()
                                    k = stdscr.getch()
                                    if k == curses.KEY_UP:
                                        if current_field > 0: current_field -= 1
                                    elif k == curses.KEY_DOWN:
                                        if current_field < 5: current_field += 1
                                    elif k == ord('b') or k == ord('s'):
                                        if current_field == 0: type_f = chr(k)
                                    elif (k >= ord('0') and k <= ord('9')) or k == ord('.'):
                                        if current_field == 1: quantity += chr(k)
                                        elif current_field == 2: price_f += chr(k)
                                        elif current_field == 3: tp += chr(k)
                                        elif current_field == 4: sl += chr(k)
                                    elif k == 263:
                                        if current_field == 0: type_f = type_f[:-1]
                                        elif current_field == 1: quantity = quantity[:-1]
                                        elif current_field == 2: price_f = price_f[:-1]
                                        elif current_field == 3: tp = tp[:-1]
                                        elif current_field == 4: sl = sl[:-1]
                                    elif k == 10:
                                        if current_field == 5:
                                            if type_f and quantity and price_f:
                                                try:
                                                    try:
                                                        self.open_orders.remove(order)
                                                    except Exception:
                                                        pass
                                                    self.place_limit_order(type_f, float(quantity.strip()), float(price_f.strip()), self.symbols[self.current_symbol])
                                                    status_local = f"{type_f} order for {self.symbols[self.current_symbol]} edited successfully"
                                                except Exception as e:
                                                    status_local = f"Failed to edit order: {e}"
                                                type_f = quantity = price_f = tp = sl = ''
                                                # invalidate cache
                                                self.cached_plot_lines.pop((symbol, False), None)
                                                self.cached_plot_lines.pop((symbol, True), None)
                                                break
                                            else:
                                                status_local = "Please fill in all fields"
                                    elif k == ord('q'):
                                        type_f = quantity = price_f = tp = sl = ''
                                        break
                            elif option_index == 1:
                                try:
                                    self.open_orders.remove(order)
                                except Exception:
                                    pass
                                # invalidate caches
                                self.cached_plot_lines.pop((symbol, False), None)
                                self.cached_plot_lines.pop((symbol, True), None)
                            elif option_index == 2:
                                self.open_orders = []
                                self.cached_plot_lines.pop((symbol, False), None)
                                self.cached_plot_lines.pop((symbol, True), None)
                            break
                        elif key2 == ord('q'):
                            break
            elif key in (ord('r'), ord('R')):
                fetched = self.fetch_candles(symbol)
                if fetched:
                    candles = fetched[-max(self.fullscreen_candles, 16):]
                    self.save_candles(symbol, candles)
                else:
                    candles = self.load_candles(symbol)
                self.display_fullscreen_chart(stdscr, symbol, candles)
                self.cached_plot_lines.pop((symbol, False), None)
                self.cached_plot_lines.pop((symbol, True), None)
            elif key == ord('q'):
                break
    def show_menu(self, stdscr):
        h = 0
        while True:
            stdscr.clear()
            stdscr.addstr(0, 0, "Options:")
            for i, act in enumerate(self.ACTIONS):
                mode = curses.A_REVERSE if i == h else curses.A_NORMAL
                stdscr.addstr(i + 1, 2, act, mode)
            max_h, max_w = stdscr.getmaxyx()
            stdscr.addstr(max_h - 2, 0, self.status[:max(0, max_w - 1)])
            stdscr.refresh()
            stdscr.noutrefresh()
            curses.doupdate()
            k = stdscr.getch()
            if k == curses.KEY_UP:
                h = (h - 1) % len(self.ACTIONS)
            elif k == curses.KEY_DOWN:
                h = (h + 1) % len(self.ACTIONS)
            elif k == 10:
                action = self.ACTIONS[h]
                if action == "1. Update":
                    stdscr.addstr(max_h - 2, 0, "Updating...                                                                                                        ")
                    stdscr.refresh()
                    return action
                else:
                    return action

    def get_live_ticker(self, symbol):
        try:
            return self.exchange_live.fetch_ticker(symbol)
        except Exception:
            return None
    def update_tickers(self):
        # background ticker updater (gentle sleep to avoid busy-loop)
        while True:
            for symbol in self.symbols:
                ticker = self.get_ticker(symbol)
                if ticker is not None:
                    self.tickers[symbol] = ticker
                    self.last_update = datetime.now(pytz.timezone('Africa/Algiers')).strftime("%Y-%m-%d %H:%M:%S")
            try:
                self.save_data()
            except Exception:
                pass
            time.sleep(3)  # slower: 10s to reduce CPU and redraw overload
    def place_limit_order(self, side, amount, price, symbol):
        self.open_orders.append({'side': side, 'amount': amount, 'price': price, 'symbol': symbol})
        base, quote = symbol.split('/')
        if side == 'b':
            self.balance[quote] -= amount * price
            self.balance[base] = self.balance.get(base, 0) + amount
        elif side == 's':
            self.balance[quote] += amount * price
            self.balance[base] = self.balance.get(base, 0) - amount
    def run(self, stdscr):
        curses.curs_set(0)
        thread = threading.Thread(target=self.update_tickers, daemon=True)
        thread.start()
        curses.cbreak()
        stdscr.keypad(True)
        stdscr.timeout(300)
        while True:
            self.display_symbols(stdscr)
            key = stdscr.getch()
            if key == -1:
                continue
            elif key == curses.KEY_UP:
                self.current_symbol = (self.current_symbol - 1) % len(self.symbols)
                self.cached_plot_lines.clear()
            elif key == curses.KEY_DOWN:
                self.current_symbol = (self.current_symbol + 1) % len(self.symbols)
                self.cached_plot_lines.clear()
            elif key == 10:
                ticker = self.tickers.get(self.symbols[self.current_symbol])
                if ticker is not None:
                    self.display_trading_menu(stdscr, ticker)
            elif key == ord('c'):
                action = self.show_menu(stdscr)
                if action == "1. Update":
                    self.waiting = False
                    t = threading.Thread(target=self.update_tickers, daemon=True)
                    t.start()
                    self.waiting = True
                elif action == "2. Trading":
                    ticker = self.tickers.get(self.symbols[self.current_symbol])
                    if ticker is not None:
                        self.display_trading_menu(stdscr, ticker)
                elif action == "3. Add Coin":
                    stdscr.timeout(-1)
                    max_h, max_w = stdscr.getmaxyx()
                    stdscr.addstr(min(max_h-2,45), 0, "Enter symbol: ")
                    stdscr.refresh()
                    curses.echo()
                    symbol = stdscr.getstr(min(max_h-2,45), 13, 20).decode('utf-8')
                    curses.noecho()
                    if symbol:
                        self.symbols.append(symbol)
                        self.save_data()
                    stdscr.timeout(100)
                elif action == "4. Delete Coin":
                    symbol = self.symbols[self.current_symbol]
                    try:
                        self.symbols.remove(symbol)
                        self.save_data()
                    except Exception:
                        pass
                elif action == "5. Cancel":
                    pass
            elif key == ord('r') or key == ord('R'):
                # open full-screen chart from Symbols screen (A=3 behavior)
                symbol = self.symbols[self.current_symbol]
                candles = self.load_candles(symbol)
                fetched = self.fetch_candles(symbol)
                if fetched:
                    candles = fetched[-self.fullscreen_candles:]
                    self.save_candles(symbol, candles)
                self.display_fullscreen_chart(stdscr, symbol, candles)
                # after closing fullscreen, invalidate caches so mini-chart redraws properly
                self.cached_plot_lines.pop((symbol, False), None)
                self.cached_plot_lines.pop((symbol, True), None)
            elif key == ord('q'):
                curses.endwin()
                sys.exit(0)

if __name__ == "__main__":
    client = NasroClient()
    curses.wrapper(client.run)
