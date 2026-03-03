import os
import json
import time
import datetime
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from mootdx.quotes import Quotes
import warnings
warnings.filterwarnings('ignore')

# ==========================================
# ⚙️ 模拟实盘核心配置 (黄金参数)
# ==========================================
INITIAL_CAPITAL = 1000000.0   # 初始本金 100 万
MAX_POSITION_PCT = 0.20       # 单只股票最大仓位 20%
TAKE_PROFIT = 0.12            # 止盈 12%
STOP_LOSS = -0.04             # 止损 -4%
MAX_HOLD_DAYS = 8             # 最大持仓 8 天
SLOPE_THRESHOLD = 25.0        # MA20 抢筹斜率阈值

PORTFOLIO_FILE = 'portfolio.json' # 账户记忆文件
EXCEL_LIST = 'stock_list.xlsx'    # 您的股票池
HTML_OUTPUT = 'index.html'        # 最终推送到网页的看板

# ==========================================
# 🧮 A股真实费率计算器
# ==========================================
def calc_buy_cost(price, shares):
    """买入成本：佣金(万2.5,最低5元) + 过户费(十万分之1)"""
    value = price * shares
    commission = max(5.0, value * 0.00025)
    transfer_fee = value * 0.00001
    return value + commission + transfer_fee, commission + transfer_fee

def calc_sell_revenue(price, shares):
    """卖出收入：扣除 印花税(千0.5) + 佣金(万2.5,最低5) + 过户费(十万分之1)"""
    value = price * shares
    stamp_tax = value * 0.0005
    commission = max(5.0, value * 0.00025)
    transfer_fee = value * 0.00001
    total_fee = stamp_tax + commission + transfer_fee
    return value - total_fee, total_fee

# ==========================================
# 🧠 账户记忆管理
# ==========================================
def load_portfolio():
    """读取账户，如果不存在则自动创建 100 万初始账户"""
    if not os.path.exists(PORTFOLIO_FILE):
        init_data = {
            "initial_capital": INITIAL_CAPITAL,
            "cash": INITIAL_CAPITAL,
            "holdings": {},  # 格式: {"000001": {"name":"平安银行", "shares":1000, "buy_price":10.5, "buy_date":"2024-05-01", "cost":10505.5}}
            "history": []    # 历史流水
        }
        with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
            json.dump(init_data, f, ensure_ascii=False, indent=4)
        return init_data
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_portfolio(data):
    """保存账户状态"""
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# ==========================================
# 📊 策略计算引擎 (对齐您的右侧主升浪)
# ==========================================
def analyze_stock(stock_info, client):
    symbol = stock_info['code']
    try:
        # 实盘只需最近 100 根 K 线计算指标即可，提升极速抓取性能
        df = client.bars(symbol=symbol, frequency=9, offset=100)
        if df is None or len(df) < 60: return None
            
        df.rename(columns={'datetime':'日期','open':'开盘','close':'收盘','high':'最高','low':'最低','vol':'成交量'}, inplace=True)
        for c in ['开盘', '收盘', '最高', '最低', '成交量']: df[c] = pd.to_numeric(df[c], errors='coerce')

        df['MA5'] = df['收盘'].rolling(5).mean()
        df['MA10'] = df['收盘'].rolling(10).mean()
        df['MA20'] = df['收盘'].rolling(20).mean()
        df['MA60'] = df['收盘'].rolling(60).mean()
        df['VOL_MA5'] = df['成交量'].rolling(5).mean()

        df['MA20_ANGLE'] = np.degrees(np.arctan((df['MA20'] / df['MA20'].shift(1) - 1) * 100))
        cond_trend = (df['收盘'] > df['MA10']) & (df['MA5'] > df['MA20']) & (df['MA20'] > df['MA60']) & (df['MA60'] > df['MA60'].shift(1))
        cond_power = (df['收盘'] / df['收盘'].shift(1) > 1.03) & (df['收盘'] > df['开盘'])
        cond_vol = df['成交量'] > df['VOL_MA5']
        
        df['DIF'] = df['收盘'].ewm(span=12, adjust=False).mean() - df['收盘'].ewm(span=26, adjust=False).mean()
        df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
        cond_macd = (df['DIF'] > 0) & (df['DIF'] > df['DEA'])

        # 核心信号提取 (只取最后一行切片数据，即“当下”的数据)
        last_row = df.iloc[-1]
        
        buy_signal = (last_row['MA20_ANGLE'] > SLOPE_THRESHOLD) and cond_trend.iloc[-1] and cond_power.iloc[-1] and cond_vol.iloc[-1] and cond_macd.iloc[-1]
        
        # 卖出信号: 跌破10日线 或 (均线向下且跌破20日线)
        cross_ma10 = (df['收盘'].shift(1).iloc[-1] >= df['MA10'].shift(1).iloc[-1]) and (last_row['收盘'] < last_row['MA10'])
        ma20_bad = (last_row['MA20_ANGLE'] < 0) and (last_row['收盘'] < last_row['MA20'])
        sell_signal = cross_ma10 or ma20_bad

        return {
            'code': symbol,
            'name': stock_info['name'],
            'price': last_row['收盘'],
            'angle': last_row['MA20_ANGLE'],
            'buy_signal': buy_signal,
            'sell_signal': sell_signal
        }
    except Exception:
        return None

# ==========================================
# 🌐 HTML 实时看板生成器
# ==========================================
def generate_dashboard(portfolio, current_market_data):
    today_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 1. 计算持仓总市值
    holdings_value = 0.0
    holdings_html = ""
    for code, info in portfolio['holdings'].items():
        # 获取最新现价，如果当天由于停牌没取到，用买入价代替
        current_price = current_market_data.get(code, {}).get('price', info['buy_price']) 
        market_val = current_price * info['shares']
        holdings_value += market_val
        
        float_pnl = market_val - info['cost']
        float_pnl_pct = (market_val / info['cost'] - 1) * 100
        color_class = "text-danger" if float_pnl > 0 else "text-success" # 红色上涨，绿色下跌
        
        holdings_html += f"""
        <tr>
            <td>{code}</td>
            <td>{info['name']}</td>
            <td>{info['shares']}</td>
            <td>¥{info['buy_price']:.2f}</td>
            <td>¥{current_price:.2f}</td>
            <td class="{color_class}">¥{float_pnl:.2f} ({float_pnl_pct:.2f}%)</td>
            <td>{info['buy_date']}</td>
        </tr>
        """
    if not holdings_html:
        holdings_html = "<tr><td colspan='7' class='text-center'>当前空仓，等待主升浪猎物...</td></tr>"

    # 2. 计算账户核心指标
    total_assets = portfolio['cash'] + holdings_value
    total_pnl = total_assets - portfolio['initial_capital']
    total_return = (total_assets / portfolio['initial_capital'] - 1) * 100
    
    # 3. 渲染历史流水
    history_html = ""
    for record in reversed(portfolio['history']): # 倒序，最新的在最上面
        pnl_str = f"¥{record.get('pnl', 0):.2f}" if record['action'] == 'SELL' else "-"
        color = "danger" if record['action'] == 'BUY' else "primary"
        history_html += f"""
        <tr>
            <td>{record['time']}</td>
            <td><span class="badge bg-{color}">{record['action']}</span></td>
            <td>{record['code']} ({record['name']})</td>
            <td>¥{record['price']:.2f}</td>
            <td>{record['shares']}</td>
            <td>¥{record['fees']:.2f}</td>
            <td>{pnl_str}</td>
            <td>{record['reason']}</td>
        </tr>
        """
    if not history_html:
        history_html = "<tr><td colspan='8' class='text-center'>暂无交易流水记录，持续运行以积累数据。</td></tr>"

    # HTML 模板拼装
    html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>实盘量化中控台</title>
        <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
        <style>body {{background-color: #f1f5f9; font-family: 'Microsoft YaHei';}} .card{{border:none; border-radius:10px; box-shadow:0 4px 6px rgba(0,0,0,0.05);}} .metric-title{{color:#64748b; font-size:0.9rem; font-weight:bold;}} .metric-value{{font-size:1.8rem; font-weight:bold; color:#0f172a;}} .up-red{{color:#dc3545!important;}} .down-green{{color:#198754!important;}}</style>
    </head>
    <body class="p-4">
        <h2 class="mb-4">🚀 主升浪量化中控台 <small class="text-muted" style="font-size:1rem;">(更新时间: {today_str})</small></h2>
        
        <div class="row mb-4">
            <div class="col"><div class="card p-3"><div class="metric-title">初始本金</div><div class="metric-value">¥{portfolio['initial_capital']:,.2f}</div></div></div>
            <div class="col"><div class="card p-3"><div class="metric-title">可用资金</div><div class="metric-value">¥{portfolio['cash']:,.2f}</div></div></div>
            <div class="col"><div class="card p-3"><div class="metric-title">持仓市值</div><div class="metric-value">¥{holdings_value:,.2f}</div></div></div>
            <div class="col"><div class="card p-3"><div class="metric-title">当前总盈亏</div><div class="metric-value {'up-red' if total_pnl>0 else 'down-green'}">¥{total_pnl:,.2f}</div></div></div>
            <div class="col"><div class="card p-3"><div class="metric-title">当前收益率</div><div class="metric-value {'up-red' if total_return>0 else 'down-green'}">{total_return:.2f}%</div></div></div>
        </div>

        <div class="card mb-4">
            <div class="card-header bg-dark text-white fw-bold">💼 当前实盘持仓</div>
            <div class="table-responsive">
                <table class="table table-hover mb-0">
                    <thead class="table-light"><tr><th>代码</th><th>名称</th><th>持仓股数</th><th>持仓成本价</th><th>最新现价</th><th>浮动盈亏</th><th>买入日期</th></tr></thead>
                    <tbody>{holdings_html}</tbody>
                </table>
            </div>
        </div>

        <div class="card">
            <div class="card-header bg-secondary text-white fw-bold">📜 实盘交易流水 (支持1个月记忆)</div>
            <div class="table-responsive" style="max-height: 500px; overflow-y: auto;">
                <table class="table table-striped mb-0">
                    <thead class="table-light sticky-top"><tr><th>时间</th><th>方向</th><th>股票</th><th>成交价</th><th>数量</th><th>手续费/税</th><th>平仓盈亏</th><th>触发原因</th></tr></thead>
                    <tbody>{history_html}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
    with open(HTML_OUTPUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ HTML 看板已更新！")

# ==========================================
# 🚀 主程序入口 (交易撮合枢纽)
# ==========================================
if __name__ == '__main__':
    print("===========================================")
    print("📡 正在启动实盘模拟引擎 (盘中突击版)...")
    print("===========================================")
    
    today_date = datetime.date.today().strftime('%Y-%m-%d')
    now_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 1. 加载账户记忆
    portfolio = load_portfolio()
    
    # 2. 读取股票池
    meta_df = pd.read_excel(EXCEL_LIST, usecols=[0, 1])
    meta_df.columns = ['code', 'name']
    meta_df.dropna(subset=['code'], inplace=True)
    meta_df['code'] = meta_df['code'].astype(str).str.replace(r'\.0$', '', regex=True).str.zfill(6)
    stock_list = meta_df.to_dict('records')

    # 3. 极速获取全市场最新切片行情
    client = Quotes.factory(market='std', multithread=True, heartbeat=True)
    market_data = {}
    valid_buys = []
    
    print("🔍 正在扫描全市场最新行情与信号...")
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {executor.submit(analyze_stock, stock, client): stock['code'] for stock in stock_list}
        for future in as_completed(futures):
            res = future.result()
            if res:
                market_data[res['code']] = res
                if res['buy_signal']:
                    valid_buys.append(res)
            time.sleep(0.01) # 防封锁

    # ==========================
    # 🛑 处理卖出 (严格 T+1 与参数控制)
    # ==========================
    sold_codes = []
    for code, info in list(portfolio['holdings'].items()):
        # 【铁律】：今日买入的绝对不允许卖出 (T+1锁)
        if info['buy_date'] == today_date:
            continue
            
        current_data = market_data.get(code)
        if not current_data: continue
            
        curr_price = current_data['price']
        profit_ratio = (curr_price / info['buy_price']) - 1
        
        # 计算已持仓天数 (粗略按日历天算，如果在周末运行不会有问题)
        days_held = (datetime.date.today() - datetime.datetime.strptime(info['buy_date'], '%Y-%m-%d').date()).days
        
        sell_reason = ""
        if profit_ratio >= TAKE_PROFIT: sell_reason = f"硬止盈 (+{profit_ratio*100:.1f}%)"
        elif profit_ratio <= STOP_LOSS: sell_reason = f"硬止损 ({profit_ratio*100:.1f}%)"
        elif days_held >= MAX_HOLD_DAYS: sell_reason = f"持仓超时 ({days_held}天)"
        elif current_data['sell_signal']: sell_reason = "技术形态恶化 (S点)"

        if sell_reason:
            shares = info['shares']
            net_revenue, fees = calc_sell_revenue(curr_price, shares)
            
            # 更新账户
            portfolio['cash'] += net_revenue
            pnl = net_revenue - info['cost']
            
            # 记录历史
            portfolio['history'].append({
                'time': now_time, 'action': 'SELL', 'code': code, 'name': info['name'],
                'price': curr_price, 'shares': shares, 'fees': fees, 'pnl': pnl, 'reason': sell_reason
            })
            del portfolio['holdings'][code]
            sold_codes.append(code)
            print(f"💰 卖出触发: {info['name']} ({code}) - {sell_reason}, 获利: {pnl:.2f}")

    # ==========================
    # 🟢 处理买入 (MA20 抢筹过滤)
    # ==========================
    # 按斜率角度降序，最猛的在最前面
    valid_buys.sort(key=lambda x: x['angle'], reverse=True)
    
    for stock in valid_buys:
        code = stock['code']
        # 防重复：已经在持仓里的不买，刚刚卖出的今天不买接回
        if code in portfolio['holdings'] or code in sold_codes:
            continue
            
        price = stock['price']
        min_lot = 200 if code.startswith('688') else 100
        
        # 计算资金能买多少
        max_money = min(portfolio['initial_capital'] * MAX_POSITION_PCT, portfolio['cash'])
        shares_to_buy = int(max_money // price)
        shares_to_buy = (shares_to_buy // min_lot) * min_lot # 向下取整到手
        
        if shares_to_buy >= min_lot:
            total_cost, fees = calc_buy_cost(price, shares_to_buy)
            
            # 再次检查加上手续费后钱够不够
            if portfolio['cash'] >= total_cost:
                portfolio['cash'] -= total_cost
                
                # 记录持仓
                portfolio['holdings'][code] = {
                    'name': stock['name'],
                    'shares': shares_to_buy,
                    'buy_price': price,
                    'buy_date': today_date,
                    'cost': total_cost
                }
                
                # 记录历史
                portfolio['history'].append({
                    'time': now_time, 'action': 'BUY', 'code': code, 'name': stock['name'],
                    'price': price, 'shares': shares_to_buy, 'fees': fees, 'reason': f"主升浪捕捉 (斜率:{stock['angle']:.1f}°)"
                })
                print(f"🔫 买入触发: {stock['name']} ({code}) - 耗资: {total_cost:.2f}")

    # 4. 保存状态 & 渲染 HTML
    save_portfolio(portfolio)
    generate_dashboard(portfolio, market_data)

    print("✅ 任务完成，正在断开连接并安全退出...")
    os._exit(0)