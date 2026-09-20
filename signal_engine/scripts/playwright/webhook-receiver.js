/**
 * Webhook Receiver & Alert Router
 * Receives alerts from Playwright monitor or Pine Script strategy
 * Routes to Slack, Discord, Telegram, Email
 * Manages alert state and prevents duplicates
 */

const http = require('http');
const fs = require('fs');
const path = require('path');

// ============ CONFIGURATION ============
const CONFIG = {
  PORT: 3000,
  WEBHOOK_SECRET: process.env.WEBHOOK_SECRET || 'your-secret-key',

  // Channels
  SLACK_WEBHOOK: process.env.SLACK_WEBHOOK_URL || null,
  DISCORD_WEBHOOK: process.env.DISCORD_WEBHOOK_URL || null,
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || null,
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || null,
  EMAIL_FROM: process.env.EMAIL_FROM || null,
  EMAIL_TO: process.env.EMAIL_TO || null,

  // File storage
  ALERTS_LOG: './alerts_received.jsonl',
  DEDUP_WINDOW: 60000, // 60 seconds — prevent duplicate alerts
};

// ============ ALERT DEDUPLICATION ============
class AlertDeduplicator {
  constructor(windowMs = CONFIG.DEDUP_WINDOW) {
    this.windowMs = windowMs;
    this.recentAlerts = new Map(); // key: fingerprint, value: timestamp
  }

  /**
   * Generate fingerprint for alert
   * Prevents duplicate entry signals within window
   */
  getFingerprint(alert) {
    const key = `${alert.symbol}_${alert.action}_${alert.direction}`;
    return key;
  }

  isDuplicate(alert) {
    const fp = this.getFingerprint(alert);
    const now = Date.now();
    const lastTime = this.recentAlerts.get(fp);

    if (lastTime && now - lastTime < this.windowMs) {
      return true; // Duplicate
    }

    this.recentAlerts.set(fp, now);
    return false;
  }

  cleanup() {
    // Remove old entries (memory management)
    const now = Date.now();
    for (const [key, time] of this.recentAlerts.entries()) {
      if (now - time > this.windowMs * 2) {
        this.recentAlerts.delete(key);
      }
    }
  }
}

// ============ NOTIFICATION FORMATTERS ============
class NotificationFormatter {
  /**
   * Format for Slack
   */
  static slack(alert) {
    const color = alert.action === 'BUY' ? '#00ff00' : '#ff0000';
    const emoji = alert.action === 'BUY' ? '🟢' : '🔴';

    return {
      text: `${emoji} ${alert.symbol} ${alert.action} Signal`,
      blocks: [
        {
          type: 'header',
          text: {
            type: 'plain_text',
            text: `${emoji} ${alert.symbol} - ${alert.action}`,
            emoji: true
          }
        },
        {
          type: 'section',
          fields: [
            {
              type: 'mrkdwn',
              text: `*Direction*\n${alert.direction}`
            },
            {
              type: 'mrkdwn',
              text: `*Timeframe*\n${alert.timeframe}`
            },
            {
              type: 'mrkdwn',
              text: `*Entry*\n₹${alert.entryPrice}`
            },
            {
              type: 'mrkdwn',
              text: `*SL*\n₹${alert.slPrice}`
            },
            {
              type: 'mrkdwn',
              text: `*TP*\n₹${alert.tpPrice}`
            },
            {
              type: 'mrkdwn',
              text: `*R:R*\n${alert.riskReward}`
            }
          ]
        },
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text: `📊 *Pattern*: ${alert.pattern}\n🔊 *Volume*: ${alert.volumeRatio}x avg\n⏰ *Time*: ${alert.time}`
          }
        },
        {
          type: 'divider'
        }
      ]
    };
  }

  /**
   * Format for Discord
   */
  static discord(alert) {
    const color = alert.action === 'BUY' ? 0x00ff00 : 0xff0000;

    return {
      embeds: [
        {
          title: `${alert.symbol} - ${alert.action}`,
          description: `**Direction**: ${alert.direction}\n**Pattern**: ${alert.pattern}`,
          color: color,
          fields: [
            {
              name: 'Entry Price',
              value: `₹${alert.entryPrice}`,
              inline: true
            },
            {
              name: 'Stop Loss',
              value: `₹${alert.slPrice}`,
              inline: true
            },
            {
              name: 'Take Profit',
              value: `₹${alert.tpPrice}`,
              inline: true
            },
            {
              name: 'R:R',
              value: alert.riskReward,
              inline: true
            },
            {
              name: 'Timeframe',
              value: alert.timeframe,
              inline: true
            },
            {
              name: 'Volume Ratio',
              value: `${alert.volumeRatio}x avg`,
              inline: true
            }
          ],
          timestamp: new Date().toISOString(),
          footer: {
            text: `TCS Live Monitor | ${alert.time}`
          }
        }
      ]
    };
  }

  /**
   * Format for Telegram
   */
  static telegram(alert) {
    const emoji = alert.action === 'BUY' ? '🟢' : '🔴';
    const text = `${emoji} *${alert.symbol} ${alert.action}*

*Direction*: ${alert.direction}
*Pattern*: ${alert.pattern}

*Entry*: ₹${alert.entryPrice}
*SL*: ₹${alert.slPrice}
*TP*: ₹${alert.tpPrice}
*R:R*: ${alert.riskReward}

*Timeframe*: ${alert.timeframe}
*Volume*: ${alert.volumeRatio}x avg
*Time*: ${alert.time}`;

    return {
      chat_id: CONFIG.TELEGRAM_CHAT_ID,
      text: text,
      parse_mode: 'Markdown'
    };
  }

  /**
   * Format for Email
   */
  static email(alert) {
    const html = `
      <html>
        <body style="font-family: Arial, sans-serif;">
          <h2>${alert.symbol} - ${alert.action}</h2>
          <table border="1" cellpadding="10">
            <tr>
              <td><strong>Direction</strong></td>
              <td>${alert.direction}</td>
            </tr>
            <tr>
              <td><strong>Pattern</strong></td>
              <td>${alert.pattern}</td>
            </tr>
            <tr>
              <td><strong>Entry Price</strong></td>
              <td>₹${alert.entryPrice}</td>
            </tr>
            <tr>
              <td><strong>Stop Loss</strong></td>
              <td>₹${alert.slPrice}</td>
            </tr>
            <tr>
              <td><strong>Take Profit</strong></td>
              <td>₹${alert.tpPrice}</td>
            </tr>
            <tr>
              <td><strong>Risk:Reward</strong></td>
              <td>${alert.riskReward}</td>
            </tr>
            <tr>
              <td><strong>Timeframe</strong></td>
              <td>${alert.timeframe}</td>
            </tr>
            <tr>
              <td><strong>Volume Ratio</strong></td>
              <td>${alert.volumeRatio}x average</td>
            </tr>
            <tr>
              <td><strong>Time</strong></td>
              <td>${alert.time}</td>
            </tr>
          </table>
          <p><small>Generated at ${new Date().toISOString()}</small></p>
        </body>
      </html>
    `;

    return {
      from: CONFIG.EMAIL_FROM,
      to: CONFIG.EMAIL_TO,
      subject: `${alert.symbol} ${alert.action} - ${alert.pattern}`,
      html: html
    };
  }
}

// ============ NOTIFICATION DISPATCHER ============
class NotificationDispatcher {
  /**
   * Send to Slack
   */
  static async sendSlack(alert) {
    if (!CONFIG.SLACK_WEBHOOK) {
      console.log('⚠️  Slack not configured');
      return false;
    }

    try {
      const payload = NotificationFormatter.slack(alert);
      const response = await fetch(CONFIG.SLACK_WEBHOOK, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      console.log('✅ Slack notification sent');
      return true;
    } catch (err) {
      console.error('❌ Slack error:', err.message);
      return false;
    }
  }

  /**
   * Send to Discord
   */
  static async sendDiscord(alert) {
    if (!CONFIG.DISCORD_WEBHOOK) {
      console.log('⚠️  Discord not configured');
      return false;
    }

    try {
      const payload = NotificationFormatter.discord(alert);
      const response = await fetch(CONFIG.DISCORD_WEBHOOK, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      console.log('✅ Discord notification sent');
      return true;
    } catch (err) {
      console.error('❌ Discord error:', err.message);
      return false;
    }
  }

  /**
   * Send to Telegram
   */
  static async sendTelegram(alert) {
    if (!CONFIG.TELEGRAM_BOT_TOKEN || !CONFIG.TELEGRAM_CHAT_ID) {
      console.log('⚠️  Telegram not configured');
      return false;
    }

    try {
      const payload = NotificationFormatter.telegram(alert);
      const url = `https://api.telegram.org/bot${CONFIG.TELEGRAM_BOT_TOKEN}/sendMessage`;
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      console.log('✅ Telegram notification sent');
      return true;
    } catch (err) {
      console.error('❌ Telegram error:', err.message);
      return false;
    }
  }

  /**
   * Route to all configured channels
   */
  static async broadcast(alert) {
    console.log(`\n📢 Broadcasting alert: ${alert.symbol} ${alert.action}`);

    const results = await Promise.all([
      this.sendSlack(alert),
      this.sendDiscord(alert),
      this.sendTelegram(alert)
    ]);

    return results;
  }
}

// ============ WEBHOOK SERVER ============
const deduplicator = new AlertDeduplicator();

const server = http.createServer(async (req, res) => {
  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

  if (req.method === 'OPTIONS') {
    res.writeHead(200);
    res.end();
    return;
  }

  if (req.method === 'POST' && req.url === '/webhook') {
    let body = '';

    req.on('data', chunk => {
      body += chunk.toString();
    });

    req.on('end', async () => {
      try {
        const alert = JSON.parse(body);

        // Validate required fields
        if (!alert.symbol || !alert.action) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Missing symbol or action' }));
          return;
        }

        // Check for duplicates
        if (deduplicator.isDuplicate(alert)) {
          console.log('⏭️  Duplicate alert skipped (within dedup window)');
          res.writeHead(202, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ status: 'duplicate', skipped: true }));
          return;
        }

        // Log to file
        const logEntry = {
          receivedAt: new Date().toISOString(),
          ...alert
        };
        fs.appendFileSync(CONFIG.ALERTS_LOG, JSON.stringify(logEntry) + '\n');

        // Broadcast to all channels
        await NotificationDispatcher.broadcast(alert);

        // Success response
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'processed', id: Date.now() }));

      } catch (err) {
        console.error('❌ Webhook error:', err.message);
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });

    return;
  }

  // Health check
  if (req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'healthy',
      uptime: process.uptime(),
      alerts_logged: fs.existsSync(CONFIG.ALERTS_LOG) ?
        fs.readFileSync(CONFIG.ALERTS_LOG, 'utf8').split('\n').filter(l => l).length : 0
    }));
    return;
  }

  // Not found
  res.writeHead(404);
  res.end();
});

// ============ STARTUP ============
server.listen(CONFIG.PORT, () => {
  console.log(`🚀 Webhook receiver listening on port ${CONFIG.PORT}`);
  console.log(`POST ${CONFIG.PORT}/webhook — receive alerts`);
  console.log(`GET ${CONFIG.PORT}/health — health check`);
  console.log('\n📋 Configured channels:');
  console.log(`  Slack:    ${CONFIG.SLACK_WEBHOOK ? '✅' : '❌'}`);
  console.log(`  Discord:  ${CONFIG.DISCORD_WEBHOOK ? '✅' : '❌'}`);
  console.log(`  Telegram: ${CONFIG.TELEGRAM_BOT_TOKEN ? '✅' : '❌'}`);
  console.log(`  Email:    ${CONFIG.EMAIL_FROM ? '✅' : '❌'}\n`);
});

// Periodic cleanup
setInterval(() => {
  deduplicator.cleanup();
}, 5 * 60 * 1000); // Every 5 minutes

process.on('SIGINT', () => {
  console.log('\n👋 Webhook receiver shutdown');
  server.close();
  process.exit(0);
});