"""Inbound ingest — how Fabric learns that upstream data changed.

One module per INTEGRATION_MODE, and exactly one of them runs:

  change_api (default) → change_poller.py   poll the DB's $changed-resources feed
  polling              → diff_poller.py     poll each per-resource API, diff fields
  kafka                → kafka_consumer.py  consume hospilot.changes.* pushed by the DB

All three end the same way: mapped events handed to messaging.data_events for
hospilot-backend to consume. Publishing itself lives in messaging/, not here; the
outbound write leg lives in writeback/.

Background subsystem started in main.py's lifespan (only when Kafka is configured).
Not part of the request-driven service layer; depends downward on service.* + clients.*
"""
