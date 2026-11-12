"""Runtime settings, read from environment (retail_agent/.env is loaded by `adk web`)."""

from __future__ import annotations

import os

# Model IDs get retired; keep them out of code so a retirement is a one-line .env change.
MODEL_ID = os.environ.get("MODEL_ID", "gemini-3.8-flash")

# MCP Toolbox server that holds the BigQuery credentials.
TOOLBOX_URL = os.environ.get("TOOLBOX_URL", "http://127.0.0.1:5000")