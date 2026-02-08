#!/usr/bin/env python3
"""
全球分散化基金监控系统
- 自动抓取天天基金净值数据
- 基于250日均线偏离度生成买入/卖出信号
- 邮件发送每日监控报告
"""

import csv
import json
import smtplib
import time
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml

# ============================================================
# 配置加载
# ============================================================

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
TRADE_LOG_PATH = BASE_DIR / "data" / "trade_log.csv"
PORTFOLIO_PATH = BASE_DIR / "data" / "portfolio.csv"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
# 天天基金数据抓取
# ============================================================

def fetch_nav_history(fund_code: str, days: int = 300) -> list[dict]:
    """
    从天天基金抓取历史净值。
    返回 [{"date": "2025-01-01", "nav": 1.2345}, ...] 按日期升序。
    """
    url = (
        f"https://api.fund.eastmoney.com/f10/lsjz?"
        f"fundCode={fund_code}&pageIndex=1&pageSize={days}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://fundf10.eastmoney.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        records = []
        for item in data.get("Data", {}).get("LSJZList", []):
            date_str = item.get("FSRQ", "")
            nav_str = item.get("DWJZ", "")
            if date_str and nav_str:
                try:
                    records.append({"date": date_str, "nav": float(nav_str)})
                except ValueError:
                    continue
        records.reverse()  # 升序
        return records
    except Exception as e:
        print(f"  [ERROR] 抓取 {fund_code} 失败: {e}")
        return []


def fetch_current_nav(fund_code: str) -> tuple[str, float] | None:
    """获取最新净值，返回 (date, nav) 或 None。"""
    records = fetch_nav_history(fund_code, days=3)
    if records:
        latest = records[-1]
        return latest["date"], latest["nav"]
    return None


# ============================================================
# 本地数据存储
# ============================================================

def ensure_data_files():
    """确保数据目录和文件存在。"""
    (BASE_DIR / "data").mkdir(exist_ok=True)
    if not TRADE_LOG_PATH.exists():
        with open(TRADE_LOG_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "fund_code", "action", "nav", "shares_delta", "note"])


def load_trade_log() -> list[dict]:
    if not TRADE_LOG_PATH.exists():
        return []
    with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_trade_log(date: str, fund_code: str, action: str, nav: float,
                     shares_delta: int, note: str = ""):
    with open(TRADE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([date, fund_code, action, nav, shares_delta, note])


def get_current_shares(fund_code: str, trade_log: list[dict]) -> int:
    """计算某基金当前持有份数。"""
    total = 0
    for row in trade_log:
        if row["fund_code"] == fund_code:
            total += int(row["shares_delta"])
    return max(total, 0)


def get_last_trade_date(fund_code: str, action: str, trade_log: list[dict]) -> str | None:
    """获取某基金最近一次 buy/sell 的日期。"""
    for row in reversed(trade_log):
        if row["fund_code"] == fund_code and row["action"] == action:
            return row["date"]
    return None


def get_avg_cost(fund_code: str, trade_log: list[dict]) -> float | None:
    """计算某基金的平均持仓成本。"""
    total_cost = 0.0
    total_shares = 0
    for row in trade_log:
        if row["fund_code"] == fund_code:
            delta = int(row["shares_delta"])
            nav = float(row["nav"])
            if delta > 0:
                total_cost += delta * nav
                total_shares += delta
            elif delta < 0:
                if total_shares > 0:
                    avg = total_cost / total_shares
                    sold = min(abs(delta), total_shares)
                    total_cost -= sold * avg
                    total_shares -= sold
    if total_shares > 0:
        return total_cost / total_shares
    return None


def get_extra_shares(fund_code: str, trade_log: list[dict]) -> int:
    """计算某基金当前持有的额外份数（extra_buy - extra_sell）。"""
    total = 0
    for row in trade_log:
        if row["fund_code"] == fund_code and row["action"] in ("extra_buy", "extra_sell"):
            total += int(row["shares_delta"])
    return max(total, 0)


def has_used_extra(fund_code: str, trade_log: list[dict]) -> bool:
    """该基金是否曾经使用过额外加仓机会。"""
    for row in trade_log:
        if row["fund_code"] == fund_code and row["action"] == "extra_buy":
            return True
    return False


def get_last_buy_nav(fund_code: str, trade_log: list[dict]) -> float | None:
    """获取某基金最近一次 buy/hold_buy/extra_buy 的净值。"""
    for row in reversed(trade_log):
        if row["fund_code"] == fund_code and row["action"] in ("buy", "extra_buy"):
            return float(row["nav"])
    return None


def get_last_action_date(fund_code: str, trade_log: list[dict]) -> str | None:
    """获取某基金最近一次任何交易（含extra）的日期，用于统一冷却判断。"""
    for row in reversed(trade_log):
        if row["fund_code"] == fund_code and row["action"] in (
            "buy", "sell", "extra_buy", "extra_sell"
        ):
            return row["date"]
    return None


# ============================================================
# 信号生成
# ============================================================

class Signal:
    def __init__(self, fund_code: str, fund_name: str, category: str):
        self.fund_code = fund_code
        self.fund_name = fund_name
        self.category = category
        self.current_nav: float = 0.0
        self.current_date: str = ""
        self.ma250: float | None = None
        self.deviation_pct: float | None = None
        self.held_shares: int = 0
        self.extra_shares: int = 0       # 额外持仓份数
        self.max_shares: int = 0
        self.avg_cost: float | None = None
        self.pnl_pct: float | None = None
        # 信号
        self.signal_type: str = ""       # buy / sell / extra_buy / extra_sell / hold_buy / none
        self.can_execute: bool = False
        self.reason: str = ""
        self.block_reasons: list[str] = []


def compute_signals(config: dict) -> list[Signal]:
    """对所有基金计算信号。"""
    strategy = config["strategy"]
    ma_days = strategy["ma_days"]
    thresholds = strategy["thresholds"]
    buy_cooldown = strategy["buy_cooldown_days"]
    sell_cooldown = strategy["sell_cooldown_days"]
    override_extra = strategy["cooldown_override_extra_pct"]
    extra_drop_pct = strategy["extra_drop_pct"]

    trade_log = load_trade_log()
    today = datetime.now()
    signals = []

    for fund_cfg in config["funds"]:
        code = fund_cfg["code"]
        name = fund_cfg["name"]
        category = fund_cfg["category"]
        vol = fund_cfg["volatility"]
        max_shares = fund_cfg["max_shares"]

        sig = Signal(code, name, category)
        sig.max_shares = max_shares

        print(f"  处理 {code} {name} ...", end=" ")

        # --- 债券类：不参与择时 ---
        if vol == "low":
            held = get_current_shares(code, trade_log)
            sig.held_shares = held
            if held < max_shares:
                sig.signal_type = "hold_buy"
                sig.can_execute = True
                sig.reason = f"债券类直接买入，当前{held}/{max_shares}份"
            else:
                sig.signal_type = "none"
                sig.reason = f"债券类已满仓 {held}/{max_shares}份"
            # 拉最新净值用于记录
            result = fetch_current_nav(code)
            if result:
                sig.current_date, sig.current_nav = result
            signals.append(sig)
            print(sig.reason)
            time.sleep(0.3)
            continue

        # --- 权益/商品类：均线偏离策略 ---
        history = fetch_nav_history(code, days=ma_days + 30)
        time.sleep(0.5)  # 控制频率

        if len(history) < 60:
            sig.signal_type = "none"
            sig.reason = f"历史数据不足（仅{len(history)}天），跳过"
            signals.append(sig)
            print(sig.reason)
            continue

        sig.current_nav = history[-1]["nav"]
        sig.current_date = history[-1]["date"]

        # 计算250日均线（取实际可用天数，最多ma_days）
        nav_values = [r["nav"] for r in history]
        window = min(len(nav_values), ma_days)
        sig.ma250 = sum(nav_values[-window:]) / window
        sig.deviation_pct = (sig.current_nav - sig.ma250) / sig.ma250 * 100

        # 持仓信息（常规 + 额外）
        sig.held_shares = get_current_shares(code, trade_log)
        sig.extra_shares = get_extra_shares(code, trade_log)
        sig.avg_cost = get_avg_cost(code, trade_log)
        if sig.avg_cost and sig.avg_cost > 0:
            sig.pnl_pct = (sig.current_nav - sig.avg_cost) / sig.avg_cost * 100

        # 获取阈值
        th = thresholds.get(vol, thresholds["mid"])
        buy_th = th["buy_pct"]
        sell_th = th["sell_pct"]

        # --- 判断卖出信号（优先判断，因为extra_sell优先级高）---
        if sig.deviation_pct >= sell_th:
            blocks = []

            total_held = sig.held_shares + sig.extra_shares
            if total_held <= 0:
                blocks.append("无持仓可卖")

            # 冷却期检查：任意卖出/买入操作后都要看冷却
            last_sell = get_last_trade_date(code, "sell", trade_log)
            last_extra_sell_date = None
            for row in reversed(trade_log):
                if row["fund_code"] == code and row["action"] == "extra_sell":
                    last_extra_sell_date = row["date"]
                    break
            # 取最近的卖出日期
            recent_sell = max(filter(None, [last_sell, last_extra_sell_date]), default=None)
            if recent_sell:
                days_since = (today - datetime.strptime(recent_sell, "%Y-%m-%d")).days
                if days_since < sell_cooldown:
                    blocks.append(f"卖出冷却中（{days_since}/{sell_cooldown}天）")

            sig.block_reasons = blocks
            sig.can_execute = len(blocks) == 0

            # 优先卖extra份额
            if sig.extra_shares > 0:
                sig.signal_type = "extra_sell"
                sig.reason = f"偏离 {sig.deviation_pct:+.1f}% ≥ +{sell_th}%，优先卖出额外份额"
            elif sig.held_shares > 0:
                sig.signal_type = "sell"
                sig.reason = f"偏离 {sig.deviation_pct:+.1f}% ≥ +{sell_th}%"
            else:
                sig.signal_type = "sell"
                sig.reason = f"偏离 {sig.deviation_pct:+.1f}% ≥ +{sell_th}%"

        # --- 判断买入信号 ---
        elif sig.deviation_pct <= buy_th:
            blocks = []

            # 常规配额是否满
            quota_full = sig.held_shares >= max_shares

            if quota_full:
                # 配额满 → 检查是否触发超跌加仓
                used_extra = has_used_extra(code, trade_log)
                last_buy_nav = get_last_buy_nav(code, trade_log)

                if used_extra:
                    blocks.append(f"配额已满 {sig.held_shares}/{max_shares}，额外机会已用")
                elif last_buy_nav is None:
                    blocks.append(f"配额已满 {sig.held_shares}/{max_shares}，无上次买入记录")
                else:
                    drop_from_last = (sig.current_nav - last_buy_nav) / last_buy_nav * 100
                    if drop_from_last > extra_drop_pct:
                        blocks.append(
                            f"配额已满 {sig.held_shares}/{max_shares}，"
                            f"距上次买入跌幅 {drop_from_last:.1f}% 未达 {extra_drop_pct}%"
                        )
                    else:
                        # 满足超跌条件 → 按extra_buy处理
                        sig.signal_type = "extra_buy"
                        sig.reason = (
                            f"🔥 超跌加仓！偏离 {sig.deviation_pct:+.1f}%，"
                            f"较上次买入({last_buy_nav:.4f})又跌 {drop_from_last:.1f}%"
                        )
            else:
                sig.signal_type = "buy"
                sig.reason = f"偏离 {sig.deviation_pct:+.1f}% ≤ 阈值 {buy_th}%"

            # 冷却期检查（常规买入和额外买入都需要）
            if sig.signal_type in ("buy", "extra_buy"):
                last_buy = get_last_trade_date(code, "buy", trade_log)
                last_extra_buy_date = None
                for row in reversed(trade_log):
                    if row["fund_code"] == code and row["action"] == "extra_buy":
                        last_extra_buy_date = row["date"]
                        break
                recent_buy = max(filter(None, [last_buy, last_extra_buy_date]), default=None)
                if recent_buy:
                    days_since = (today - datetime.strptime(recent_buy, "%Y-%m-%d")).days
                    if days_since < buy_cooldown:
                        if sig.deviation_pct <= buy_th - override_extra:
                            pass  # 允许突破
                        else:
                            blocks.append(f"买入冷却中（{days_since}/{buy_cooldown}天）")

            sig.block_reasons = blocks
            sig.can_execute = len(blocks) == 0 and sig.signal_type in ("buy", "extra_buy")

        else:
            sig.signal_type = "none"
            sig.reason = f"偏离 {sig.deviation_pct:+.1f}%，未触发（买≤{buy_th}% / 卖≥+{sell_th}%）"

        signals.append(sig)
        print(f"{sig.signal_type or 'hold'} | {sig.reason}")

    return signals


# ============================================================
# 交易执行（记录到日志）
# ============================================================

def execute_signals(signals: list[Signal], dry_run: bool = False):
    """将可执行信号写入交易日志。dry_run=True 时仅报告不记录。"""
    executed = []
    for sig in signals:
        if not sig.can_execute:
            continue
        if sig.signal_type in ("buy", "hold_buy"):
            if not dry_run:
                append_trade_log(
                    sig.current_date, sig.fund_code, "buy",
                    sig.current_nav, 1, sig.reason
                )
            executed.append(sig)
        elif sig.signal_type == "extra_buy":
            if not dry_run:
                append_trade_log(
                    sig.current_date, sig.fund_code, "extra_buy",
                    sig.current_nav, 1, sig.reason
                )
            executed.append(sig)
        elif sig.signal_type == "sell":
            if not dry_run:
                append_trade_log(
                    sig.current_date, sig.fund_code, "sell",
                    sig.current_nav, -1, sig.reason
                )
            executed.append(sig)
        elif sig.signal_type == "extra_sell":
            if not dry_run:
                append_trade_log(
                    sig.current_date, sig.fund_code, "extra_sell",
                    sig.current_nav, -1, sig.reason
                )
            executed.append(sig)
    return executed


def update_portfolio(config: dict):
    """根据 trade_log 生成当前持仓快照文件 portfolio.csv。"""
    trade_log = load_trade_log()
    rows = []

    for fund_cfg in config["funds"]:
        code = fund_cfg["code"]
        name = fund_cfg["name"]
        category = fund_cfg["category"]
        max_shares = fund_cfg["max_shares"]

        regular = get_current_shares(code, trade_log)
        extra = get_extra_shares(code, trade_log)
        avg_cost = get_avg_cost(code, trade_log)

        # 最近一次买入日期
        last_buy = None
        for row in reversed(trade_log):
            if row["fund_code"] == code and row["action"] in ("buy", "extra_buy"):
                last_buy = row["date"]
                break

        rows.append({
            "fund_code": code,
            "fund_name": name,
            "category": category,
            "regular_shares": regular,
            "max_shares": max_shares,
            "extra_shares": extra,
            "total_shares": regular + extra,
            "avg_cost": f"{avg_cost:.4f}" if avg_cost else "",
            "last_buy_date": last_buy or "",
        })

    with open(PORTFOLIO_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "fund_code", "fund_name", "category",
            "regular_shares", "max_shares", "extra_shares", "total_shares",
            "avg_cost", "last_buy_date",
        ])
        writer.writeheader()
        writer.writerows(rows)

    total_regular = sum(r["regular_shares"] for r in rows)
    total_extra = sum(r["extra_shares"] for r in rows)
    print(f"  持仓快照已更新: {PORTFOLIO_PATH}")
    print(f"  常规份额: {total_regular}/50 | 额外份额: {total_extra}")


# ============================================================
# 邮件报告
# ============================================================

SIGNAL_EMOJI = {
    "buy": "🟢 买入",
    "sell": "🔴 卖出",
    "extra_buy": "🔥 超跌加仓",
    "extra_sell": "📤 卖出(额外)",
    "hold_buy": "🔵 建仓",
    "none": "⚪ 持有",
}


def build_email_html(signals: list[Signal], executed: list[Signal]) -> str:
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 统计
    buy_signals = [s for s in signals if s.signal_type in ("buy", "hold_buy", "extra_buy")]
    sell_signals = [s for s in signals if s.signal_type in ("sell", "extra_sell")]
    exec_buy = [s for s in executed if s.signal_type in ("buy", "hold_buy", "extra_buy")]
    exec_sell = [s for s in executed if s.signal_type in ("sell", "extra_sell")]

    html = f"""
    <html><head><style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; background: #fff; border-radius: 8px;
                      box-shadow: 0 2px 8px rgba(0,0,0,0.1); padding: 24px; }}
        h1 {{ color: #2F5496; border-bottom: 2px solid #2F5496; padding-bottom: 8px; font-size: 20px; }}
        h2 {{ color: #4472C4; font-size: 16px; margin-top: 24px; }}
        .summary {{ display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; }}
        .card {{ background: #f8f9fa; border-radius: 6px; padding: 12px 18px; min-width: 120px;
                 border-left: 4px solid #4472C4; }}
        .card.buy {{ border-left-color: #00B050; }}
        .card.sell {{ border-left-color: #FF0000; }}
        .card .num {{ font-size: 24px; font-weight: bold; }}
        .card .label {{ font-size: 12px; color: #666; }}
        table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }}
        th {{ background: #2F5496; color: #fff; padding: 8px 10px; text-align: left; }}
        td {{ padding: 6px 10px; border-bottom: 1px solid #e0e0e0; }}
        tr:hover {{ background: #f0f4fa; }}
        .tag {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
                font-size: 11px; font-weight: bold; }}
        .tag-buy {{ background: #e6f4ea; color: #137333; }}
        .tag-sell {{ background: #fce8e6; color: #c5221f; }}
        .tag-hold {{ background: #e8eaed; color: #5f6368; }}
        .tag-exec {{ background: #d2e3fc; color: #1a73e8; }}
        .blocked {{ color: #999; font-size: 11px; }}
        .pos {{ color: #00B050; }}
        .neg {{ color: #FF0000; }}
        .footer {{ margin-top: 20px; padding-top: 12px; border-top: 1px solid #e0e0e0;
                   font-size: 11px; color: #999; }}
    </style></head><body>
    <div class="container">
    <h1>📊 基金监控日报 — {today_str}</h1>

    <div class="summary">
        <div class="card buy"><div class="num">{len(exec_buy)}</div><div class="label">执行买入</div></div>
        <div class="card sell"><div class="num">{len(exec_sell)}</div><div class="label">执行卖出</div></div>
        <div class="card"><div class="num">{len(buy_signals)}</div><div class="label">买入信号</div></div>
        <div class="card"><div class="num">{len(sell_signals)}</div><div class="label">卖出信号</div></div>
    </div>
    """

    # 需要关注的信号（非 none）
    action_signals = [s for s in signals if s.signal_type != "none"]

    if action_signals:
        html += """
        <h2>🔔 今日信号</h2>
        <table>
        <tr><th>信号</th><th>执行</th><th>基金</th><th>类别</th>
            <th>净值</th><th>MA250</th><th>偏离</th><th>持仓</th><th>说明</th></tr>
        """
        for s in action_signals:
            sig_tag = {"buy": "tag-buy", "sell": "tag-sell", "hold_buy": "tag-buy",
                       "extra_buy": "tag-buy", "extra_sell": "tag-sell",
                       }.get(s.signal_type, "tag-hold")
            exec_tag = "tag-exec" if s.can_execute else "tag-hold"
            exec_text = "✅ 已执行" if s.can_execute else "❌ 阻止"
            dev_class = "neg" if (s.deviation_pct or 0) < 0 else "pos"
            dev_str = f"{s.deviation_pct:+.1f}%" if s.deviation_pct is not None else "-"
            ma_str = f"{s.ma250:.4f}" if s.ma250 else "-"
            block_note = "<br>".join(s.block_reasons) if s.block_reasons else ""
            note = s.reason
            if block_note:
                note += f'<br><span class="blocked">⛔ {block_note}</span>'
            html += f"""
            <tr>
                <td><span class="tag {sig_tag}">{SIGNAL_EMOJI.get(s.signal_type, s.signal_type)}</span></td>
                <td><span class="tag {exec_tag}">{exec_text}</span></td>
                <td><b>{s.fund_code}</b><br>{s.fund_name}</td>
                <td>{s.category}</td>
                <td>{s.current_nav:.4f}</td>
                <td>{ma_str}</td>
                <td class="{dev_class}">{dev_str}</td>
                <td>{s.held_shares}/{s.max_shares}{f"+{s.extra_shares}extra" if s.extra_shares else ""}</td>
                <td style="font-size:11px">{note}</td>
            </tr>"""
        html += "</table>"
    else:
        html += '<h2>🔔 今日信号</h2><p style="color:#666">今日无触发信号，所有基金在正常范围内。</p>'

    # 全部持仓概览
    held_funds = [s for s in signals if s.held_shares > 0 or s.extra_shares > 0]
    if held_funds:
        html += """
        <h2>📋 当前持仓概览</h2>
        <table>
        <tr><th>基金</th><th>类别</th><th>净值</th><th>常规持仓</th><th>额外</th><th>成本</th><th>盈亏</th></tr>
        """
        for s in held_funds:
            cost_str = f"{s.avg_cost:.4f}" if s.avg_cost else "-"
            pnl_str = "-"
            pnl_class = ""
            if s.pnl_pct is not None:
                pnl_class = "pos" if s.pnl_pct >= 0 else "neg"
                pnl_str = f"{s.pnl_pct:+.1f}%"
            html += f"""
            <tr>
                <td><b>{s.fund_code}</b> {s.fund_name}</td>
                <td>{s.category}</td>
                <td>{s.current_nav:.4f}</td>
                <td>{s.held_shares}/{s.max_shares}</td>
                <td>{s.extra_shares if s.extra_shares else "-"}</td>
                <td>{cost_str}</td>
                <td class="{pnl_class}">{pnl_str}</td>
            </tr>"""
        html += "</table>"

    html += f"""
    <div class="footer">
        此邮件由基金监控系统自动发送 | 数据来源：天天基金 | 仅供参考，不构成投资建议<br>
        策略参数：MA250日均线 | 冷却期：买卖均30天
    </div>
    </div></body></html>
    """
    return html


def send_email(config: dict, subject: str, html_body: str):
    """发送HTML邮件。"""
    email_cfg = config["email"]
    if not email_cfg.get("enabled", False):
        print("[INFO] 邮件通知未启用")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["receiver"]
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if email_cfg.get("use_ssl", True):
            server = smtplib.SMTP_SSL(email_cfg["smtp_server"], email_cfg["smtp_port"])
        else:
            server = smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"])
            server.starttls()
        server.login(email_cfg["sender"], email_cfg["password"])
        server.sendmail(email_cfg["sender"], email_cfg["receiver"], msg.as_string())
        server.quit()
        print("[OK] 邮件发送成功")
    except Exception as e:
        print(f"[ERROR] 邮件发送失败: {e}")


# ============================================================
# 主入口
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="基金监控系统")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅计算信号，不写入交易记录")
    parser.add_argument("--no-email", action="store_true",
                        help="不发送邮件")
    parser.add_argument("--report-all", action="store_true",
                        help="邮件中包含所有基金状态（默认仅信号）")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  基金监控系统启动 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    config = load_config()
    ensure_data_files()

    print(f"\n[1/3] 抓取净值并计算信号（共{len(config['funds'])}只基金）...\n")
    signals = compute_signals(config)

    print(f"\n[2/3] 执行交易信号...")
    executed = execute_signals(signals, dry_run=args.dry_run)
    if args.dry_run:
        print(f"  [DRY RUN] 有 {len(executed)} 个可执行信号（未写入日志）")
    else:
        print(f"  已执行 {len(executed)} 笔交易")

    # 更新持仓快照（dry_run 时也根据现有 trade_log 生成）
    update_portfolio(config)

    print(f"\n[3/3] 生成报告...")
    action_count = sum(1 for s in signals if s.signal_type != "none")

    if action_count > 0 or args.report_all:
        today_str = datetime.now().strftime("%m/%d")
        buy_count = sum(1 for s in executed if s.signal_type in ("buy", "hold_buy", "extra_buy"))
        sell_count = sum(1 for s in executed if s.signal_type in ("sell", "extra_sell"))

        subject = f"📊 基金日报 {today_str}"
        if buy_count:
            subject += f" | 买入{buy_count}笔"
        if sell_count:
            subject += f" | 卖出{sell_count}笔"
        if action_count == 0:
            subject += " | 无信号"

        html = build_email_html(signals, executed)

        if not args.no_email:
            send_email(config, subject, html)
        else:
            # 保存到本地文件
            report_path = BASE_DIR / "data" / f"report_{datetime.now().strftime('%Y%m%d')}.html"
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  报告已保存到: {report_path}")
    else:
        print("  今日无信号，跳过邮件")

    print(f"\n{'=' * 60}")
    print(f"  完成！")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
