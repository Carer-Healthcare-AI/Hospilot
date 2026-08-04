"""Kafka transport — Hospilot-internal only.

Everything published from here is consumed by hospilot-backend (see
agentic-framework/messaging/, which this package is named to match):

  producer.py     the single shared connection — no topics, no payload shapes
  data_events.py  hospilot.data.{entity} (a record changed) and hospilot.sync.ack
                  (the HIS accepted or rejected a write we proposed)

Nothing HIS-facing lives here by design. Fabric does publish one topic outward to the
hospital in kafka mode, but that belongs to the write leg and sits in
writeback/kafka/proposal_publisher.py — it borrows the connection from producer.py and
owns its own topic and envelope.

The whole package no-ops when KAFKA_BOOTSTRAP_SERVERS is unset, which is the normal
local-dev setup: the REST APIs still serve, only the change stream is off.
"""
