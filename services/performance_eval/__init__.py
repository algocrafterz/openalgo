"""Read-only evaluation package for the Performance page data.

The server computes every statistic (`dataset.py`) and applies fixed rules to
them (`rules.py`). `prompt.py` writes a prompt a trader can paste into
claude.ai, and `ai_response.py` checks an answer pasted back. Nothing here calls
a model, a broker or the network, and nothing writes to a database or a file.
"""
