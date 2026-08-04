"""Kafka write exit — Fabric pushes queued changes to the HIS.

Active only when INTEGRATION_MODE=kafka, in place of the sibling writeback/http/. The
hospital consumes `hospilot.sync.write` and acks on `hospilot.sync.ack` in the same
shape the HTTP $confirm produces, so hospilot-backend's ack consumer is unchanged and
Fabric leaves the ack loop entirely.

  write_publisher.py     the drain loop: peek → publish → remove
  proposal_publisher.py  the envelope + topic (borrows messaging.producer's connection)

No snapshot or soft lock is created in this mode — Kafka's durable log replaces them.
"""
