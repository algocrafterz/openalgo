/**
 * TCS Live Market Monitor using Playwright
 * Runs during market hours (09:15 - 15:30 IST)
 * Extracts candlestick + footprint data autonomously
 * Sends notifications when key levels are hit
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

// ============ CONFIGURATION ============
const CONFIG = {
  CHART_URL: 'https://gocharting.com/terminal/chart/MPBWi4qRQ',
  TIMEFRAME: '5m',
  POLL_INTERVAL: 30000, // 30 seconds between checks
  SESSION_START: 9 * 60 + 15, // 09:15 IST
  SESSION_END: 15 * 60 + 30, // 15:30 IST
  ALERTS_FILE: './market_alerts.json',
  DATA_LOG_FILE: './market_data.jsonl',
  HEADLESS: true,

  // Alert thresholds
  THRESHOLDS: {
    volumeDeltaSpike: 50000, // Alert if delta > 50K
    pocDistance: 15, // Alert if price within 15 points of POC
    supportBreak: true, // Alert on support breakage
    resistanceTouch: true, // Alert on resistance hit
    reversalPattern: true // Alert on double-bottom/double-top
  }
};

// ============ STATE MANAGEMENT ============
class MarketState {
  constructor() {
    this.candles = [];
    this.footprints = [];
    this.currentPrice = null;
    this.pocLevel = null;
    this.supportLevels = [];
    this.resistanceLevels = [];
    this.alerts = [];
    this.lastAnalysisTime = null;
    this.sessionStartPrice = null;
  }

  addCandle(candle) {
    this.candles.push({
      timestamp: new Date().toISOString(),
      ...candle
    });
  }

  addFootprint(footprint) {
    this.footprints.push({
      timestamp: new Date().toISOString(),
      ...footprint
    });
  }

  getLastNCandles(n = 10) {
    return this.candles.slice(-n);
  }

  getLastNFootprints(n = 10) {
    return this.footprints.slice(-n);
  }

  addAlert(type, message, data = {}) {
    const alert = {
      id: `${type}-${Date.now()}`,
      timestamp: new Date().toISOString(),
      type,
      message,
      data,
      acknowledged: false
    };
    this.alerts.push(alert);
    this.logAlert(alert);
    return alert;
  }

  logAlert(alert) {
    const existing = this.readAlertsFile();
    existing.push(alert);
    fs.writeFileSync(CONFIG.ALERTS_FILE, JSON.stringify(existing, null, 2));
  }

  readAlertsFile() {
    if (fs.existsSync(CONFIG.ALERTS_FILE)) {
      return JSON.parse(fs.readFileSync(CONFIG.ALERTS_FILE, 'utf8'));
    }
    return [];
  }

  logData() {
    const record = {
      timestamp: new Date().toISOString(),
      candles: this.getLastNCandles(3),
      footprints: this.getLastNFootprints(3),
      pocLevel: this.pocLevel,
      currentPrice: this.currentPrice,
      activeAlerts: this.alerts.filter(a => !a.acknowledged).length
    };
    fs.appendFileSync(CONFIG.DATA_LOG_FILE, JSON.stringify(record) + '\n');
  }
}

// ============ DOM DATA EXTRACTION ============
class GoChartingExtractor {
  async extractCandleData(page) {
    /**
     * Extracts visible candlestick data:
     * - OHLCV (Open, High, Low, Close, Volume)
     * - Candle color (green/red)
     * - Wicks and body size
     */
    return page.evaluate(() => {
      const candles = [];

      // Method 1: Extract from visible price labels
      const priceElements = Array.from(document.querySelectorAll('span, div')).filter(el => {
        const text = (el.innerText || el.textContent || '').trim();
        return /^[0-9]{4,5}[\.,][0-9]{1,3}$/.test(text);
      });

      // Method 2: Look for candle-related data attributes
      const candleElements = document.querySelectorAll('[class*="candle"], [data-candle], svg [class*="bar"]');

      candleElements.forEach((el, idx) => {
        const rect = el.getBoundingClientRect();
        const computedStyle = window.getComputedStyle(el);
        const color = computedStyle.fill || computedStyle.backgroundColor;

        // Infer candle properties from position and size
        const candle = {
          index: idx,
          xPos: rect.x,
          yPos: rect.y,
          height: rect.height,
          width: rect.width,
          color: color.includes('rgb(34, 177, 76)') || color.includes('green') ? 'green' : 'red',
          // Note: For production, use canvas pixel-to-price conversion
          attributes: {
            class: el.className,
            dataAttrs: Object.fromEntries(
              Object.entries(el.dataset || {})
            )
          }
        };
        candles.push(candle);
      });

      return {
        candlesFound: candles.length,
        candles: candles.slice(-10), // Last 10 candles
        pageHeight: document.documentElement.scrollHeight
      };
    });
  }

  async extractFootprintData(page) {
    /**
     * Extracts footprint cluster data:
     * - Buy/sell volume clusters
     * - Delta per level
     * - POC (Point of Control)
     */
    return page.evaluate(() => {
      const footprintData = {
        clusters: [],
        deltaSeries: [],
        pocLevel: null
      };

      // Extract delta indicators
      const deltaElements = Array.from(document.querySelectorAll('span, div')).filter(el => {
        const text = (el.innerText || el.textContent || '').trim();
        // Look for patterns like "+37.2K", "-70.4K"
        return /^[+-]\d+[\.,]\d+K$/.test(text);
      });

      deltaElements.forEach(el => {
        const text = el.innerText || el.textContent;
        const value = parseFloat(text.replace(/K$/i, '')) * 1000;
        footprintData.deltaSeries.push({
          value,
          text,
          xPos: el.getBoundingClientRect().x,
          color: text.startsWith('+') ? 'green' : 'red'
        });
      });

      // Extract POC markers
      const pocElements = Array.from(document.querySelectorAll('[class*="poc"], [class*="profile"]'));
      if (pocElements.length > 0) {
        const pocText = pocElements[0].innerText || pocElements[0].textContent;
        footprintData.pocLevel = parseFloat(pocText);
      }

      // Extract cluster cells (colored buy/sell nodes)
      const clusterCells = document.querySelectorAll('[class*="cluster"], [class*="cell"]');
      clusterCells.forEach(cell => {
        const style = window.getComputedStyle(cell);
        footprintData.clusters.push({
          color: style.backgroundColor,
          opacity: style.opacity,
          xPos: cell.getBoundingClientRect().x,
          yPos: cell.getBoundingClientRect().y
        });
      });

      return footprintData;
    });
  }

  async extractOHLCV(page) {
    /**
     * Extracts current candle OHLCV from header display
     */
    return page.evaluate(() => {
      const priceDisplay = document.querySelectorAll('span, div');
      const ohlcv = {
        open: null,
        high: null,
        low: null,
        close: null,
        volume: null
      };

      let textContent = '';
      priceDisplay.forEach(el => {
        textContent += el.innerText || el.textContent || '';
      });

      // Look for patterns: O: 2,106.9 H: 2,107.4 L: 2,102.6 C: 2,103.9 V: 210.91K
      const openMatch = textContent.match(/O[:\s]+([0-9,]+[\.,]\d+)/);
      const highMatch = textContent.match(/H[:\s]+([0-9,]+[\.,]\d+)/);
      const lowMatch = textContent.match(/L[:\s]+([0-9,]+[\.,]\d+)/);
      const closeMatch = textContent.match(/C[:\s]+([0-9,]+[\.,]\d+)/);
      const volumeMatch = textContent.match(/V[:\s]+([0-9,]+[\.,]\d+)K/);

      return {
        open: openMatch ? parseFloat(openMatch[1].replace(/,/g, '')) : null,
        high: highMatch ? parseFloat(highMatch[1].replace(/,/g, '')) : null,
        low: lowMatch ? parseFloat(lowMatch[1].replace(/,/g, '')) : null,
        close: closeMatch ? parseFloat(closeMatch[1].replace(/,/g, '')) : null,
        volume: volumeMatch ? parseFloat(volumeMatch[1].replace(/,/g, '')) * 1000 : null
      };
    });
  }
}

// ============ ANALYSIS ENGINE ============
class MarketAnalyzer {
  constructor(state) {
    this.state = state;
  }

  /**
   * Detect double-bottom reversal pattern
   */
  detectDoubleBottom() {
    const candles = this.state.getLastNCandles(5);
    if (candles.length < 5) return null;

    const lows = candles.map(c => c.low);
    const first = lows[0];
    const second = lows[4];

    // Both lows within 0.5 points
    if (Math.abs(first - second) < 0.5 && first === Math.min(...lows)) {
      // Middle point should be higher
      if (Math.max(...lows.slice(1, 4)) > first) {
        return {
          pattern: 'double-bottom',
          firstLow: first,
          secondLow: second,
          supportLevel: first,
          target: first + (Math.max(...lows.slice(1, 4)) - first) * 2
        };
      }
    }
    return null;
  }

  /**
   * Detect volume delta spike (mean reversion)
   */
  detectDeltaSpike() {
    const footprints = this.state.getLastNFootprints(10);
    if (footprints.length < 3) return null;

    const deltas = footprints.map(f => Math.abs(f.deltaSeries?.[0]?.value || 0));
    const avgDelta = deltas.slice(0, -3).reduce((a, b) => a + b, 0) / Math.max(1, deltas.length - 3);
    const latestDelta = deltas[deltas.length - 1];

    if (latestDelta > CONFIG.THRESHOLDS.volumeDeltaSpike) {
      return {
        type: 'delta-spike',
        value: latestDelta,
        avgHistorical: avgDelta,
        magnitude: (latestDelta / avgDelta).toFixed(2) + 'x'
      };
    }
    return null;
  }

  /**
   * Calculate distance to POC (Point of Control)
   */
  analyzeDistanceToPOC() {
    if (!this.state.pocLevel || !this.state.currentPrice) return null;

    const distance = this.state.pocLevel - this.state.currentPrice;
    const percentDistance = ((distance / this.state.currentPrice) * 100).toFixed(2);

    return {
      pocLevel: this.state.pocLevel,
      currentPrice: this.state.currentPrice,
      distance,
      percentDistance,
      expectedDirection: distance > 0 ? 'UP' : 'DOWN',
      urgency: Math.abs(distance) < CONFIG.THRESHOLDS.pocDistance ? 'HIGH' : 'NORMAL'
    };
  }

  /**
   * Identify support/resistance breakouts
   */
  detectSupportResistanceBreak() {
    const candles = this.state.getLastNCandles(3);
    if (candles.length < 2) return null;

    const previousLow = candles[0].low;
    const currentLow = candles[candles.length - 1].low;

    if (currentLow < previousLow) {
      return {
        type: 'support-break',
        level: previousLow,
        breakAmount: (previousLow - currentLow).toFixed(2),
        severity: currentLow < previousLow - 1 ? 'severe' : 'minor'
      };
    }
    return null;
  }

  /**
   * Composite analysis: Returns actionable insights
   */
  generateInsight() {
    const doubleBottom = this.detectDoubleBottom();
    const deltaSpike = this.detectDeltaSpike();
    const pocAnalysis = this.analyzeDistanceToPOC();
    const supportBreak = this.detectSupportResistanceBreak();

    const signals = [];
    const confidence = [];

    if (doubleBottom) {
      signals.push(`🎯 Double-bottom reversal detected at ₹${doubleBottom.supportLevel}`);
      confidence.push('HIGH');
    }

    if (deltaSpike) {
      signals.push(`⚡ Delta spike: ${deltaSpike.value}K (${deltaSpike.magnitude}x avg)`);
      confidence.push('MEDIUM');
    }

    if (pocAnalysis && pocAnalysis.urgency === 'HIGH') {
      signals.push(`📊 Price ${pocAnalysis.expectedDirection} bias: ${Math.abs(pocAnalysis.distance).toFixed(2)} points from POC`);
      confidence.push('MEDIUM');
    }

    if (supportBreak) {
      signals.push(`⚠️ Support break: ${supportBreak.breakAmount} points below ₹${supportBreak.level}`);
      confidence.push('HIGH');
    }

    return {
      signals,
      confidence: Math.max(...confidence.map(c => ({ HIGH: 3, MEDIUM: 2, LOW: 1 }[c] || 0))),
      timestamp: new Date().toISOString(),
      actionable: signals.length > 0
    };
  }
}

// ============ NOTIFICATION SYSTEM ============
class NotificationManager {
  /**
   * Send desktop notification (Node.js environment)
   */
  static notifyConsole(title, message, data = {}) {
    const timestamp = new Date().toLocaleTimeString('en-IN');
    console.log(`\n${'='.repeat(60)}`);
    console.log(`[${timestamp}] ${title}`);
    console.log(`${message}`);
    if (Object.keys(data).length > 0) {
      console.log('Data:', JSON.stringify(data, null, 2));
    }
    console.log('='.repeat(60));
  }

  /**
   * Send webhook notification (integrate with Slack/Discord/Telegram)
   */
  static async notifyWebhook(webhookUrl, payload) {
    try {
      const response = await fetch(webhookUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!response.ok) console.error('Webhook notification failed');
    } catch (err) {
      console.error('Webhook error:', err.message);
    }
  }

  /**
   * Send email notification
   */
  static async notifyEmail(recipient, subject, html) {
    // Implementation: Use nodemailer or AWS SES
    console.log(`📧 Email to ${recipient}: ${subject}`);
  }

  /**
   * Format alert for multi-channel delivery
   */
  static formatAlert(alert) {
    return {
      // Slack format
      slack: {
        text: alert.message,
        blocks: [
          {
            type: 'header',
            text: { type: 'plain_text', text: alert.type.toUpperCase() }
          },
          {
            type: 'section',
            text: { type: 'mrkdwn', text: alert.message }
          },
          {
            type: 'context',
            elements: [
              {
                type: 'mrkdwn',
                text: `_${alert.timestamp}_`
              }
            ]
          }
        ]
      },
      // Discord format
      discord: {
        embeds: [
          {
            title: alert.type,
            description: alert.message,
            timestamp: alert.timestamp,
            color: alert.type.includes('reversal') ? 0x00ff00 : 0xff0000
          }
        ]
      },
      // Telegram format
      telegram: {
        text: `*${alert.type}*\n${alert.message}\n_${alert.timestamp}_`,
        parse_mode: 'Markdown'
      }
    };
  }
}

// ============ MAIN MONITOR LOOP ============
class LiveMarketMonitor {
  constructor() {
    this.state = new MarketState();
    this.extractor = new GoChartingExtractor();
    this.analyzer = new MarketAnalyzer(this.state);
    this.browser = null;
    this.page = null;
  }

  async initialize() {
    console.log('🚀 Initializing Playwright browser...');
    this.browser = await chromium.launch({
      headless: CONFIG.HEADLESS
    });
    this.page = await this.browser.newPage();
    await this.page.goto(CONFIG.CHART_URL, { waitUntil: 'networkidle' });
    console.log('✅ Browser ready. Navigating to GoCharting...');
  }

  isMarketOpen() {
    const now = new Date();
    const indiaTime = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const minutes = indiaTime.getHours() * 60 + indiaTime.getMinutes();
    return minutes >= CONFIG.SESSION_START && minutes <= CONFIG.SESSION_END;
  }

  async pollMarketData() {
    if (!this.isMarketOpen()) {
      console.log('⏸️  Market closed. Resuming at 09:15 IST.');
      return false;
    }

    try {
      // Extract raw data
      const ohlcv = await this.extractor.extractOHLCV(this.page);
      const candleData = await this.extractor.extractCandleData(this.page);
      const footprintData = await this.extractor.extractFootprintData(this.page);

      // Update state
      this.state.currentPrice = ohlcv.close;
      this.state.pocLevel = footprintData.pocLevel;

      if (ohlcv.close) {
        this.state.addCandle(ohlcv);
      }

      if (footprintData.deltaSeries.length > 0) {
        this.state.addFootprint({
          deltaSeries: footprintData.deltaSeries,
          clusters: footprintData.clusters,
          pocLevel: footprintData.pocLevel
        });
      }

      // Analyze and generate alerts
      const insight = this.analyzer.generateInsight();

      if (insight.actionable) {
        insight.signals.forEach((signal, idx) => {
          this.state.addAlert(
            `signal-${idx}`,
            signal,
            { insight, timestamp: new Date().toISOString() }
          );

          NotificationManager.notifyConsole(
            `MARKET SIGNAL (Confidence: ${insight.confidence}/3)`,
            signal,
            { insight }
          );
        });
      }

      // Log data for analysis
      this.state.logData();

      console.log(`✅ [${new Date().toLocaleTimeString('en-IN')}] Price: ₹${ohlcv.close}, POC: ₹${this.state.pocLevel}, Alerts: ${this.state.alerts.length}`);

      return true;
    } catch (err) {
      console.error('❌ Poll error:', err.message);
      return false;
    }
  }

  async runMonitor() {
    await this.initialize();

    console.log('📊 Market Monitor Started');
    console.log(`Polling interval: ${CONFIG.POLL_INTERVAL}ms`);
    console.log(`Market hours: 09:15–15:30 IST\n`);

    let cycleCount = 0;

    const monitorLoop = setInterval(async () => {
      cycleCount++;
      const success = await this.pollMarketData();

      if (!success || !this.isMarketOpen()) {
        if (cycleCount > 100) { // Stop after ~50 minutes of market closed
          console.log('🛑 Market session ended. Stopping monitor.');
          clearInterval(monitorLoop);
          await this.cleanup();
        }
      }
    }, CONFIG.POLL_INTERVAL);
  }

  async cleanup() {
    console.log('\n📋 Final Summary:');
    console.log(`Total candles analyzed: ${this.state.candles.length}`);
    console.log(`Total alerts generated: ${this.state.alerts.length}`);
    console.log(`Alerts file: ${CONFIG.ALERTS_FILE}`);
    console.log(`Data log: ${CONFIG.DATA_LOG_FILE}`);

    await this.browser.close();
    console.log('✅ Browser closed. Monitor complete.');
  }
}

// ============ EXECUTION ============
(async () => {
  const monitor = new LiveMarketMonitor();
  await monitor.runMonitor();
})().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});