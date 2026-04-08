Log directory for OpenAlgo Flask application.

Active log files (written when LOG_TO_FILE=True in .env):
  log/openalgo_YYYY-MM-DD.log   — Flask app logs (daily rotation, 14-day retention)
  log/strategies/{id}_*.log     — Per-strategy execution logs (10 files max, 7-day retention)

See also:
  signal_engine/logs/signal_engine_YYYY-MM-DD.log  — Signal engine (loguru, 30-day retention)
  signal_engine/logs/openalgoctl.log               — openalgoctl.sh combined stdout (5MB cap)
