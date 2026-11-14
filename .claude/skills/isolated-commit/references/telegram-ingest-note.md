# Telegram source-channel ingestion (Stage 1, commit fa628a896)

- Code: signal_engine/ingest.py; hooks in listener.py (_serve, start_listener); config `telegram.ingest_sources` in config.yaml; tests in tests/test_telegram_ingest.py.
- Store: signal_engine/data/telegram_ingest.db, tables source_messages (PK channel_id, message_id) and ingest_state (watermark).
- A search of all local branches, origin/development/smi-assistant and all 101 tags found no earlier Telegram message-persistence code. Do not search again.
- Protected Content: the source channel disallows forwarding, so storage stays local only; never repost.
- Live path (new messages and edits) was unverified as of 2026-11-12 evening; check count and max(sent_at) growth after 09:15 IST.
