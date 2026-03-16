#!/usr/bin/env python3
"""Analyze ORB trade signals from Telegram channel export."""

import json
import re
from collections import defaultdict
from datetime import datetime

INPUT_FILE = "/mnt/c/Users/Anand/Downloads/Telegram Desktop/ChatExport_2026-03-12 (1)/result.json"

def extract_text(msg):
    """Extract plain text from message text field."""
    text = msg.get("text", "")
    if isinstance(text, list):
        parts = []
        for item in text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return text

def parse_entry_signal(text):
    """Parse entry signal - handles both old and new formats."""
    # New format: "ORB LONG | SYMBOL" or "ORB SHORT | SYMBOL"
    new_match = re.search(r'ORB (LONG|SHORT)\s*\|\s*(\w+)', text)
    # Old format: "ORB LONG/SHORT ... Symbol: XXXX"
    old_dir_match = re.search(r'ORB (LONG|SHORT)', text)
    old_sym_match = re.search(r'Symbol:\s*(\w+)', text)

    direction = None
    symbol = None
    if new_match:
        direction = new_match.group(1)
        symbol = new_match.group(2)
    elif old_dir_match and old_sym_match:
        direction = old_dir_match.group(1)
        symbol = old_sym_match.group(1)

    if not direction or not symbol:
        return None

    entry_match = re.search(r'Entry:\s*([\d.]+)', text)
    target_match = re.search(r'Target:\s*([\d.]+)', text)
    sl_match = re.search(r'(?:Stop Loss|SL):\s*([\d.]+)', text)
    risk_match = re.search(r'Risk:\s*[+-]?([\d.]+)', text)
    reward_match = re.search(r'Reward:\s*[+-]?([\d.]+)', text)
    # New format: "R:R 1:1.5" or old: "R:R: 1 (1:1)"
    rr_new = re.search(r'R:R\s+1:([\d.]+)', text)
    rr_old = re.search(r'R:R:\s*[\d.]+\s*\(1:([\d.]+)\)', text)
    time_match = re.search(r'(\d{2}:\d{2})\s*IST', text)

    rr_ratio = None
    if rr_new:
        rr_ratio = float(rr_new.group(1))
    elif rr_old:
        rr_ratio = float(rr_old.group(1))

    return {
        "type": "entry",
        "direction": direction,
        "symbol": symbol,
        "entry": float(entry_match.group(1)) if entry_match else None,
        "target": float(target_match.group(1)) if target_match else None,
        "sl": float(sl_match.group(1)) if sl_match else None,
        "risk": float(risk_match.group(1)) if risk_match else None,
        "reward": float(reward_match.group(1)) if reward_match else None,
        "rr_ratio": rr_ratio,
        "time": time_match.group(1) if time_match else None,
    }

def parse_exit_signal(text):
    """Parse exit signal - handles both old and new formats."""
    # Determine exit type
    exit_type = None
    if "SL HIT" in text or ("STOP LOSS" in text and "Exit:" in text):
        exit_type = "SL"
    elif "TP1.5 HIT" in text or "TAKE PROFIT (TP1.5)" in text:
        exit_type = "TP1.5"
    elif "TP1 HIT" in text or "TAKE PROFIT (TP1)" in text:
        exit_type = "TP1"
    elif "TP2 HIT" in text or "TAKE PROFIT (TP2)" in text:
        exit_type = "TP2"
    elif "TIME EXIT" in text:
        exit_type = "TIME_EXIT"

    if not exit_type:
        return None

    # Extract symbol - multiple patterns
    symbol = None
    # New format: "| SYMBOL\n" at end of first line
    sym_new = re.search(r'\|\s*([A-Z]+)\s*\n', text)
    # Old format: "Symbol: XXXX"
    sym_old = re.search(r'Symbol:\s*(\w+)', text)
    if sym_new:
        symbol = sym_new.group(1)
    elif sym_old:
        symbol = sym_old.group(1)

    # Direction
    direction = None
    dir_match = re.search(r'(LONG|SHORT)', text)
    if dir_match:
        direction = dir_match.group(1)

    # Prices
    entry_match = re.search(r'Entry:\s*([\d.]+)', text)
    exit_match = re.search(r'Exit:\s*([\d.]+)', text)

    # PnL
    pnl_pct = None
    # New format: "+0.42%" or "-0.42%" or "(+0.42%)" or "(-0.42%)"
    pct_match = re.search(r'\(([+-][\d.]+)%\)', text)
    if pct_match:
        pnl_pct = float(pct_match.group(1))

    # PnL amount
    pnl_amount = None
    loss_match = re.search(r'Loss:\s*[+-]?([\d.]+)', text)
    profit_match = re.search(r'Profit:\s*\+?([\d.]+)', text)
    pnl_raw = re.search(r'P&L:\s*([+-]?[\d.]+)', text)
    if loss_match:
        pnl_amount = -float(loss_match.group(1))
    elif profit_match:
        pnl_amount = float(profit_match.group(1))
    elif pnl_raw:
        pnl_amount = float(pnl_raw.group(1))

    return {
        "type": "exit",
        "exit_type": exit_type,
        "symbol": symbol,
        "direction": direction,
        "entry_price": float(entry_match.group(1)) if entry_match else None,
        "exit_price": float(exit_match.group(1)) if exit_match else None,
        "pnl_amount": pnl_amount,
        "pnl_pct": pnl_pct,
    }

def classify_message(text):
    """Classify message type."""
    if not text or not text.strip():
        return "empty"
    if "ORB LONG" in text or "ORB SHORT" in text:
        return "entry"
    if "SL HIT" in text or "STOP LOSS" in text and "Exit:" in text:
        return "exit"
    if "TP1 HIT" in text or "TP1.5 HIT" in text or "TP2 HIT" in text:
        return "exit"
    if "TAKE PROFIT" in text and "Exit:" in text:
        return "exit"
    if "TIME EXIT" in text:
        return "exit"
    if "TEST ALERT" in text:
        return "test"
    return "unknown"

def main():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = data.get("messages", [])
    print(f"Total messages in channel: {len(messages)}\n")

    entries = []
    exits = []
    unparsed = []

    for msg in messages:
        if msg.get("type") != "message":
            continue

        text = extract_text(msg)
        date = msg.get("date", "")
        msg_type = classify_message(text)

        if msg_type == "entry":
            parsed = parse_entry_signal(text)
            if parsed:
                parsed["date"] = date
                entries.append(parsed)
            else:
                unparsed.append(text[:100])
        elif msg_type == "exit":
            parsed = parse_exit_signal(text)
            if parsed:
                parsed["date"] = date
                exits.append(parsed)
            else:
                unparsed.append(text[:100])
        elif msg_type == "test":
            pass  # skip test alerts
        elif msg_type != "empty":
            unparsed.append(text[:100])

    print(f"Entry signals parsed: {len(entries)}")
    print(f"Exit signals parsed: {len(exits)}")
    print(f"Unparsed: {len(unparsed)}")
    if unparsed:
        print("  Sample unparsed:")
        for u in unparsed[:3]:
            print(f"    {u}")

    # Date range
    all_dates = [e["date"][:10] for e in entries + exits if e.get("date")]
    if all_dates:
        print(f"\nDate range: {min(all_dates)} to {max(all_dates)}")
        trading_days = len(set(all_dates))
        print(f"Trading days with signals: {trading_days}")

    print("\n" + "="*70)
    print("ENTRY SIGNAL ANALYSIS")
    print("="*70)

    # Direction breakdown
    longs = [e for e in entries if e["direction"] == "LONG"]
    shorts = [e for e in entries if e["direction"] == "SHORT"]
    print(f"\nTotal entries: {len(entries)}")
    print(f"LONG: {len(longs)} ({len(longs)/len(entries)*100:.1f}%)" if entries else "")
    print(f"SHORT: {len(shorts)} ({len(shorts)/len(entries)*100:.1f}%)" if entries else "")

    # Per-symbol entry counts
    symbol_entries = defaultdict(lambda: {"long": 0, "short": 0, "total": 0})
    for e in entries:
        sym = e["symbol"]
        symbol_entries[sym]["total"] += 1
        symbol_entries[sym]["long" if e["direction"] == "LONG" else "short"] += 1

    print(f"\nUnique symbols: {len(symbol_entries)}")
    print("\nSignals per symbol:")
    for sym, counts in sorted(symbol_entries.items(), key=lambda x: -x[1]["total"]):
        print(f"  {sym:15s} Total: {counts['total']:3d}  |  L: {counts['long']:3d}  S: {counts['short']:3d}")

    # Entry time distribution
    print("\nEntry time distribution:")
    time_buckets = defaultdict(int)
    for e in entries:
        if e["time"]:
            hour = e["time"].split(":")[0]
            time_buckets[hour] += 1
    for hour in sorted(time_buckets.keys()):
        bar = "#" * (time_buckets[hour] // 2)
        print(f"  {hour}:xx  {time_buckets[hour]:3d}  {bar}")

    # R:R distribution
    print("\nR:R ratio distribution:")
    rr_values = [e["rr_ratio"] for e in entries if e.get("rr_ratio") is not None]
    rr_buckets = defaultdict(int)
    for rr in rr_values:
        if rr < 0.8: rr_buckets["< 0.8"] += 1
        elif rr <= 1.0: rr_buckets["0.8-1.0"] += 1
        elif rr <= 1.2: rr_buckets["1.0-1.2"] += 1
        elif rr <= 1.5: rr_buckets["1.2-1.5"] += 1
        elif rr <= 2.0: rr_buckets["1.5-2.0"] += 1
        else: rr_buckets["> 2.0"] += 1
    for bucket in ["< 0.8", "0.8-1.0", "1.0-1.2", "1.2-1.5", "1.5-2.0", "> 2.0"]:
        count = rr_buckets.get(bucket, 0)
        bar = "#" * (count // 1 if count < 50 else count // 3)
        print(f"  {bucket:8s}  {count:3d}  {bar}")
    if rr_values:
        print(f"\n  Average R:R: 1:{sum(rr_values)/len(rr_values):.2f}")
        print(f"  Median R:R:  1:{sorted(rr_values)[len(rr_values)//2]:.2f}")

    # Risk as % of entry
    print("\nRisk as % of entry price:")
    risk_pcts = []
    for e in entries:
        if e["entry"] and e["risk"] and e["entry"] > 0:
            risk_pcts.append((e["risk"] / e["entry"]) * 100)
    if risk_pcts:
        print(f"  Min:    {min(risk_pcts):.2f}%")
        print(f"  Max:    {max(risk_pcts):.2f}%")
        print(f"  Avg:    {sum(risk_pcts)/len(risk_pcts):.2f}%")
        print(f"  Median: {sorted(risk_pcts)[len(risk_pcts)//2]:.2f}%")

    print("\n" + "="*70)
    print("EXIT / OUTCOME ANALYSIS")
    print("="*70)

    # Exit type breakdown
    exit_types = defaultdict(int)
    for e in exits:
        exit_types[e["exit_type"]] += 1

    print(f"\nTotal exits: {len(exits)}")
    for etype, count in sorted(exit_types.items(), key=lambda x: -x[1]):
        pct = count / len(exits) * 100 if exits else 0
        print(f"  {etype:12s}  {count:3d}  ({pct:.1f}%)")

    # Win/Loss
    wins = [e for e in exits if e["exit_type"].startswith("TP")]
    losses = [e for e in exits if e["exit_type"] == "SL"]
    time_exits = [e for e in exits if e["exit_type"] == "TIME_EXIT"]

    total_resolved = len(wins) + len(losses) + len(time_exits)
    if total_resolved > 0:
        print(f"\nWin Rate (TP hits): {len(wins)/total_resolved*100:.1f}% ({len(wins)}/{total_resolved})")
        print(f"Loss Rate (SL hits): {len(losses)/total_resolved*100:.1f}% ({len(losses)}/{total_resolved})")
        print(f"Time Exits: {len(time_exits)} ({len(time_exits)/total_resolved*100:.1f}%)")

    # TP1 vs TP1.5 vs TP2 breakdown
    tp_breakdown = defaultdict(int)
    for e in wins:
        tp_breakdown[e["exit_type"]] += 1
    if tp_breakdown:
        print("\n  TP breakdown:")
        for tp, count in sorted(tp_breakdown.items()):
            print(f"    {tp}: {count}")

    # Time exit analysis
    if time_exits:
        te_pos = [e for e in time_exits if e.get("pnl_pct") and e["pnl_pct"] > 0]
        te_neg = [e for e in time_exits if e.get("pnl_pct") and e["pnl_pct"] < 0]
        print(f"\n  Time Exit breakdown: {len(te_pos)} profitable, {len(te_neg)} loss")

    # PnL
    print("\nP&L Analysis:")
    pnl_pcts = [e["pnl_pct"] for e in exits if e.get("pnl_pct") is not None]
    if pnl_pcts:
        total_pnl = sum(pnl_pcts)
        print(f"  Cumulative PnL: {total_pnl:+.2f}%")
        print(f"  Average/trade:  {total_pnl/len(pnl_pcts):+.3f}%")
        print(f"  Best trade:     {max(pnl_pcts):+.2f}%")
        print(f"  Worst trade:    {min(pnl_pcts):+.2f}%")

        win_pcts = [p for p in pnl_pcts if p > 0]
        loss_pcts = [p for p in pnl_pcts if p < 0]
        if win_pcts:
            print(f"  Avg win:        {sum(win_pcts)/len(win_pcts):+.3f}%")
        if loss_pcts:
            print(f"  Avg loss:       {sum(loss_pcts)/len(loss_pcts):+.3f}%")
        if win_pcts and loss_pcts:
            avg_w = sum(win_pcts) / len(win_pcts)
            avg_l = abs(sum(loss_pcts) / len(loss_pcts))
            print(f"  Win/Loss ratio: {avg_w/avg_l:.2f}")
            expectancy = (len(win_pcts)/len(pnl_pcts) * avg_w) - (len(loss_pcts)/len(pnl_pcts) * avg_l)
            print(f"  Expectancy/trade: {expectancy:+.3f}%")

    # PER-SYMBOL PERFORMANCE
    print("\n" + "="*70)
    print("PER-SYMBOL PERFORMANCE (sorted by total PnL)")
    print("="*70)

    symbol_perf = defaultdict(lambda: {"wins": 0, "losses": 0, "te": 0, "pnl_pcts": []})
    for e in exits:
        sym = e.get("symbol")
        if not sym:
            continue
        if e["exit_type"].startswith("TP"):
            symbol_perf[sym]["wins"] += 1
        elif e["exit_type"] == "SL":
            symbol_perf[sym]["losses"] += 1
        elif e["exit_type"] == "TIME_EXIT":
            symbol_perf[sym]["te"] += 1
        if e.get("pnl_pct") is not None:
            symbol_perf[sym]["pnl_pcts"].append(e["pnl_pct"])

    print(f"\n{'Symbol':15s} {'W':>3s} {'L':>3s} {'TE':>3s} {'WR%':>6s} {'CumPnL%':>9s} {'AvgPnL%':>8s} {'Grade':>6s}")
    print("-" * 70)
    for sym, perf in sorted(symbol_perf.items(), key=lambda x: sum(x[1]["pnl_pcts"]) if x[1]["pnl_pcts"] else 0, reverse=True):
        total = perf["wins"] + perf["losses"] + perf["te"]
        wr = perf["wins"] / total * 100 if total > 0 else 0
        total_pnl = sum(perf["pnl_pcts"]) if perf["pnl_pcts"] else 0
        avg_pnl = total_pnl / len(perf["pnl_pcts"]) if perf["pnl_pcts"] else 0
        # Grade: A (>80% WR + positive PnL), B (>60% WR), C (>40%), D (<40%)
        grade = "A" if wr >= 80 and total_pnl > 0 else "B" if wr >= 60 and total_pnl > 0 else "C" if wr >= 40 else "D"
        print(f"{sym:15s} {perf['wins']:3d} {perf['losses']:3d} {perf['te']:3d} {wr:5.1f}% {total_pnl:+8.2f}% {avg_pnl:+7.2f}% {grade:>6s}")

    # DIRECTION PERFORMANCE
    print("\n" + "="*70)
    print("DIRECTION PERFORMANCE")
    print("="*70)

    for direction in ["LONG", "SHORT"]:
        dir_exits = [e for e in exits if e.get("direction") == direction]
        if not dir_exits:
            continue
        dir_wins = len([e for e in dir_exits if e["exit_type"].startswith("TP")])
        dir_losses = len([e for e in dir_exits if e["exit_type"] == "SL"])
        dir_te = len([e for e in dir_exits if e["exit_type"] == "TIME_EXIT"])
        dir_total = dir_wins + dir_losses + dir_te
        dir_wr = dir_wins / dir_total * 100 if dir_total > 0 else 0
        dir_pnls = [e["pnl_pct"] for e in dir_exits if e.get("pnl_pct") is not None]
        dir_total_pnl = sum(dir_pnls) if dir_pnls else 0
        dir_avg = dir_total_pnl / len(dir_pnls) if dir_pnls else 0
        print(f"\n{direction}:")
        print(f"  Trades: {dir_total}  |  W: {dir_wins}  L: {dir_losses}  TE: {dir_te}")
        print(f"  Win Rate: {dir_wr:.1f}%  |  Total PnL: {dir_total_pnl:+.2f}%  |  Avg: {dir_avg:+.3f}%")

    # DAILY PERFORMANCE
    print("\n" + "="*70)
    print("DAILY PERFORMANCE")
    print("="*70)

    daily_perf = defaultdict(lambda: {"wins": 0, "losses": 0, "te": 0, "pnl": 0.0, "trades": 0})
    for e in exits:
        date = e.get("date", "")[:10]
        if not date:
            continue
        daily_perf[date]["trades"] += 1
        if e["exit_type"].startswith("TP"):
            daily_perf[date]["wins"] += 1
        elif e["exit_type"] == "SL":
            daily_perf[date]["losses"] += 1
        elif e["exit_type"] == "TIME_EXIT":
            daily_perf[date]["te"] += 1
        if e.get("pnl_pct") is not None:
            daily_perf[date]["pnl"] += e["pnl_pct"]

    print(f"\n{'Date':12s} {'#':>3s} {'W':>3s} {'L':>3s} {'TE':>3s} {'PnL%':>8s}")
    print("-" * 42)
    winning_days = losing_days = flat_days = 0
    for date in sorted(daily_perf.keys()):
        d = daily_perf[date]
        indicator = "+" if d["pnl"] > 0 else "-" if d["pnl"] < 0 else " "
        print(f"{date:12s} {d['trades']:3d} {d['wins']:3d} {d['losses']:3d} {d['te']:3d} {d['pnl']:+7.2f}% {indicator}")
        if d["pnl"] > 0: winning_days += 1
        elif d["pnl"] < 0: losing_days += 1
        else: flat_days += 1

    total_days = winning_days + losing_days
    if total_days > 0:
        print(f"\nWinning days: {winning_days}/{total_days} ({winning_days/total_days*100:.1f}%)")
        print(f"Losing days:  {losing_days}/{total_days} ({losing_days/total_days*100:.1f}%)")

    # DAY OF WEEK
    print("\n" + "="*70)
    print("DAY-OF-WEEK PERFORMANCE")
    print("="*70)

    dow_perf = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    for e in exits:
        date_str = e.get("date", "")[:10]
        if not date_str: continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            dow = dt.weekday()
            if dow > 4: continue
            dow_perf[dow]["trades"] += 1
            if e["exit_type"].startswith("TP"):
                dow_perf[dow]["wins"] += 1
            elif e["exit_type"] == "SL":
                dow_perf[dow]["losses"] += 1
            if e.get("pnl_pct") is not None:
                dow_perf[dow]["pnl"] += e["pnl_pct"]
        except ValueError:
            pass

    print(f"\n{'Day':>5s} {'#':>4s} {'W':>4s} {'L':>4s} {'WR%':>6s} {'PnL%':>8s}")
    for dow in range(5):
        d = dow_perf[dow]
        total = d["wins"] + d["losses"]
        wr = d["wins"] / total * 100 if total > 0 else 0
        print(f"  {day_names[dow]:3s} {d['trades']:4d} {d['wins']:4d} {d['losses']:4d} {wr:5.1f}% {d['pnl']:+7.2f}%")

    # CONSECUTIVE LOSSES (max drawdown streak)
    print("\n" + "="*70)
    print("STREAK ANALYSIS")
    print("="*70)

    max_win_streak = max_loss_streak = 0
    cur_win = cur_loss = 0
    for e in exits:
        if e["exit_type"].startswith("TP"):
            cur_win += 1
            cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        elif e["exit_type"] == "SL":
            cur_loss += 1
            cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)
        else:
            cur_win = 0
            cur_loss = 0

    print(f"\nMax winning streak: {max_win_streak}")
    print(f"Max losing streak:  {max_loss_streak}")

    # SIGNALS PER DAY distribution
    print("\n" + "="*70)
    print("SIGNALS PER DAY DISTRIBUTION")
    print("="*70)

    entry_per_day = defaultdict(int)
    for e in entries:
        date = e.get("date", "")[:10]
        if date:
            entry_per_day[date] += 1

    if entry_per_day:
        counts = list(entry_per_day.values())
        print(f"\n  Min signals/day: {min(counts)}")
        print(f"  Max signals/day: {max(counts)}")
        print(f"  Avg signals/day: {sum(counts)/len(counts):.1f}")
        bucket_dist = defaultdict(int)
        for c in counts:
            if c <= 3: bucket_dist["1-3"] += 1
            elif c <= 6: bucket_dist["4-6"] += 1
            elif c <= 10: bucket_dist["7-10"] += 1
            elif c <= 15: bucket_dist["11-15"] += 1
            else: bucket_dist["16+"] += 1
        for b in ["1-3", "4-6", "7-10", "11-15", "16+"]:
            print(f"  {b:>5s} signals: {bucket_dist.get(b, 0)} days")


if __name__ == "__main__":
    main()
