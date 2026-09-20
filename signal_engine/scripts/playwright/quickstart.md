# Quick Start — TCS Live Monitor (5 Minutes)

## The Problem You're Solving

Instead of:
```
📊 Check GoCharting manually every 5 minutes
↓
💭 Analyse candles + footprint (analysis paralysis)
↓
🤔 Query Claude for interpretation (slow, token expensive)
↓
📱 Receive insight minutes later
```

You now have:
```
📊 Playwright auto-extracts GoCharting (every 30 seconds)
↓
⚡ Real-time pattern detection (double-bottom, delta spikes, POC)
↓
🚨 Instant webhook → Slack/Discord/Telegram
↓
✅ Trade signal in <5 seconds (autonomous)
```

---

## 5-Minute Setup

### 1. Clone/Copy Files
```bash
mkdir ~/market-monitor && cd ~/market-monitor
# Copy these files to current directory:
# - market-monitor-live.js
# - webhook-receiver.js
# - tcs-live-strategy.pine
```

### 2. Install Playwright
```bash
npm install playwright
npx playwright install  # Downloads Chromium (~300MB, one-time)
```

### 3. Start Monitor
```bash
node market-monitor-live.js
```

You should see:
```
🚀 Initializing Playwright browser...
✅ Browser ready. Navigating to GoCharting...
📊 Market Monitor Started
✅ [09:22:15] Price: ₹2,103.9, POC: ₹2,122.5, Alerts: 2
```

### 4. Start Webhook Receiver (New Terminal)
```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/URL"
node webhook-receiver.js
```

You should see:
```
🚀 Webhook receiver listening on port 3000
📋 Configured channels:
  Slack:    ✅
```

### 5. Test Alert
```bash
curl -X POST http://localhost:3000/webhook \
  -H "Content-Type: application/json" \
  -d '{"symbol":"TCS","action":"BUY","direction":"LONG","entryPrice":2103.9,"slPrice":2098.4,"tpPrice":2113.9,"riskReward":"1:2.0","pattern":"DOUBLE_BOTTOM_REVERSAL","timeframe":"5m","volumeRatio":"1.45","time":"2026-09-18 15:13:04"}'
```

Check Slack — you should receive:
```
🟢 TCS - BUY
Direction: LONG
Entry: ₹2,103.9 | SL: ₹2,098.4 | TP: ₹2,113.9 | R:R: 1:2.0
Pattern: DOUBLE_BOTTOM_REVERSAL
```

**Done!** Monitor is now running autonomously.

---

## What Gets Detected Automatically

1. **Double-Bottom Reversal** → Entry signal
2. **Volume Delta Spikes** → Momentum confirmation
3. **POC Distance** → Fair value magnet alert
4. **Support Breaks** → Risk escalation
5. **RSI Oversold** → Exhaustion confirmation

---

## Alert Example Flow

```
09:15 IST (Market opens)
  ↓
09:22 → Playwright extracts candles + footprint
  ↓
09:22 → Detects double-bottom at ₹2,104 (two lows within 0.5 points)
  ↓
09:22 → Volume spike (1.45x avg) + OBV reversal
  ↓
09:22 → HTF (15m) EMA bullish confirmation ✓
  ↓
09:22 → Generate alert: Entry ₹2,103.9 | SL ₹2,098.4 | TP ₹2,113.9
  ↓
09:23 → Send to webhook receiver
  ↓
09:23 → Broadcast to Slack + Discord + Telegram
  ↓
✅ Notification received on your phone (5 seconds total latency)
```

---

## Key Configuration Points

### GoCharting URL
In `market-monitor-live.js`, line 13:
```javascript
CHART_URL: 'https://gocharting.com/terminal/chart/MPBWi4qRQ', // Update this
```

### Alert Thresholds
In `market-monitor-live.js`, lines 24-28:
```javascript
THRESHOLDS: {
  volumeDeltaSpike: 50000,  // Alert if delta > 50K (adjust for your symbol)
  pocDistance: 15,          // Alert if price within 15 points of POC
  supportBreak: true,
  resistanceTouch: true,
  reversalPattern: true
}
```

### Notification Channels
Before starting webhook receiver:
```bash
# Slack
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."

# Discord
export DISCORD_WEBHOOK_URL="https://discordapp.com/api/webhooks/..."

# Telegram
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="987654321"
```

---

## Live Data

Monitor creates three log files:

### `market_data.jsonl` — Every bar
```json
{"timestamp":"2026-09-18T09:22:15.000Z","candles":[{"o":2106.9,"h":2107.4,"l":2102.6,"c":2103.9,"v":210910}],"pocLevel":2122.5,"currentPrice":2103.9,"activeAlerts":2}
{"timestamp":"2026-09-18T09:22:45.000Z","candles":[...],...}
```

### `market_alerts.json` — All alerts
```json
[
  {
    "id": "delta-spike-1726658535000",
    "timestamp": "2026-09-18T09:22:15.000Z",
    "type": "delta-spike",
    "message": "⚡ Delta spike: 92600K (5.2x avg)",
    "data": {...}
  },
  ...
]
```

### `alerts_received.jsonl` — Webhook log
```json
{"receivedAt":"2026-09-18T09:22:15.000Z","symbol":"TCS","action":"BUY","entryPrice":2103.9,...}
```

---

## Monitor Dashboard (Optional)

Watch real-time alerts in terminal:
```bash
watch -n 1 'tail -20 market_alerts.json | jq -r ".[].message"'
```

Or parse last hour of data:
```bash
jq 'select(.timestamp > (now - 3600) | todate) | .message' market_alerts.json
```

---

## Troubleshooting

### Q: Monitor says "Market closed"
**A:** Market hours are 09:15–15:30 IST (Saturday–Friday). Adjust `SESSION_START` / `SESSION_END` in config.

### Q: No alerts being generated
**A:** Check GoCharting URL loads manually. If it requires login, add credentials:
```javascript
await this.page.goto(CONFIG.CHART_URL);
// Add login if needed
```

### Q: Webhook not sending to Slack
**A:** Verify webhook URL:
```bash
curl -X POST "YOUR_SLACK_URL" \
  -H "Content-Type: application/json" \
  -d '{"text":"Test"}'
```

### Q: High CPU/Memory usage
**A:** Reduce poll frequency:
```javascript
POLL_INTERVAL: 60000, // 60 seconds instead of 30
```

---

## Production Checklist

- [ ] Monitor running: `node market-monitor-live.js`
- [ ] Webhook receiver running: `node webhook-receiver.js`
- [ ] Slack/Discord/Telegram webhooks configured
- [ ] Test alert received successfully
- [ ] GoCharting chart URL verified
- [ ] Market hours verified (09:15–15:30 IST)
- [ ] Data logs growing (`tail -f market_data.jsonl`)
- [ ] No console errors

---

## Running in Background (Linux/Mac)

### Using `screen` (simplest)
```bash
screen -S monitor
node market-monitor-live.js
# Ctrl+A then D to detach

screen -ls  # List sessions
screen -r monitor  # Reattach
```

### Using `nohup`
```bash
nohup node market-monitor-live.js > monitor.log 2>&1 &
nohup node webhook-receiver.js > webhook.log 2>&1 &
tail -f monitor.log
```

### Using `pm2` (recommended)
```bash
npm install -g pm2
pm2 start market-monitor-live.js --name "tcs-monitor"
pm2 start webhook-receiver.js --name "tcs-webhook"
pm2 logs
pm2 save  # Auto-restart on reboot
```

---

## API Reference

### Webhook JSON Schema
```json
{
  "symbol": "TCS",                          // Stock symbol
  "action": "BUY" | "SELL",                // Trade direction
  "direction": "LONG" | "SHORT",           // Position type
  "entryPrice": 2103.9,                    // Entry level
  "slPrice": 2098.4,                       // Stop loss
  "tpPrice": 2113.9,                       // Take profit
  "riskReward": "1:2.0",                   // R:R ratio
  "pattern": "DOUBLE_BOTTOM_REVERSAL",    // Pattern detected
  "timeframe": "5m",                       // Chart timeframe
  "volumeRatio": "1.45",                   // Volume vs average
  "time": "2026-09-18 15:13:04"            // IST timestamp
}
```

### Health Check
```bash
curl http://localhost:3000/health
# {"status":"healthy","uptime":3600,"alerts_logged":42}
```

---

## Extending the Monitor

### Add New Pattern Detection
In `MarketAnalyzer` class, add method:
```javascript
detectTriangleBreakout() {
  // Your logic here
  return {
    pattern: 'triangle-breakout',
    confidence: 'HIGH',
    target: 2115.0
  };
}
```

Then call in `generateInsight()`:
```javascript
const triangle = this.detectTriangleBreakout();
if (triangle) {
  signals.push(`Triangle breakout detected`);
}
```

### Add New Notification Channel
In `webhook-receiver.js`, add formatter:
```javascript
static telegram_group(alert) {
  // Format for group chat
  return { ... };
}

static async sendTelegramGroup(alert) {
  // Send logic
}
```

---

## Cost Savings vs. Claude Queries

| Scenario | Cost |
|----------|------|
| **Query Claude every 5 minutes (8 hours)** | ~96 queries × 3K tokens = 288K tokens |
| **Playwright automonitor (8 hours)** | 1 setup + manual review = <10K tokens total |
| **Savings** | ~98% reduction in token usage |

---

## Next Steps

1. ✅ Test locally (this guide)
2. ✅ Deploy to VPS or cloud (see DEPLOYMENT_GUIDE.md)
3. ✅ Connect Pine Script strategy to TradingView
4. ✅ Monitor live during market hours
5. ✅ Refine thresholds based on real alerts

---

## Support

- **Monitor not extracting data?** → Check GoCharting URL loads manually
- **Webhook not working?** → Run health check: `curl http://localhost:3000/health`
- **Alerts not triggering?** → Lower thresholds in `CONFIG.THRESHOLDS`
- **Performance issues?** → Increase `POLL_INTERVAL` (30s → 60s)

---

**Happy trading! 🚀📊**