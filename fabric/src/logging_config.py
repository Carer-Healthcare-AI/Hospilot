"""
Centralised logging setup for Hospilot backend.
Run setup_logging() once at startup (main.py lifespan).
"""
import logging
import sys

# ANSI escape codes
_R  = "\033[0m"      # reset
_B  = "\033[1m"      # bold
_DM = "\033[2m"      # dim
_CY = "\033[36m"     # cyan
_GR = "\033[32m"     # green
_YL = "\033[33m"     # yellow
_RD = "\033[31m"     # red
_WH = "\033[97m"     # bright white
_MG = "\033[35m"     # magenta

_LEVEL_COLOR = {
    "DEBUG":    _DM,
    "INFO":     _CY,
    "WARNING":  _YL,
    "ERROR":    _RD,
    "CRITICAL": _B + _RD,
}

# Map Python logger names to short display names (max 9 chars). Every entry below is
# a logger Fabric actually creates; unmapped names fall back to name[:9].upper().
_NAME_MAP = {
    # ingest + transport
    "poller":                               "POLLER   ",   # change_poller, diff_poller, topic_map
    "kafka":                                "KAFKA    ",   # messaging.producer
    "kafka_consumer":                       "KAFKA_CON",
    "kafka_write":                           "KAFKA_WR ",  # writeback.kafka_write_publisher
    # request-serving APIs
    "normalized":                           "NORMALIZD",
    "fhir_api":                             "FHIR_API ",
    "sync_api":                             "SYNC_API ",
    # upstream clients
    "fhir_client":                          "FHIR_CLNT",
    "rest_client":                          "REST_CLNT",
    "sync_client":                          "SYNC_CLNT",
    # service layer
    "financial":                            "FINANCIAL",
    "lab_service":                          "LAB_SVC  ",
    "pharmacy_service":                     "PHARM_SVC",
    "bundle":                               "BUNDLE   ",
    # runtime
    "__main__":                             "APP      ",
    "uvicorn":                              "UVICORN  ",
    "uvicorn.error":                        "UVICORN  ",
}


class HospilotFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        time    = self.formatTime(record, "%H:%M:%S")
        level   = record.levelname.ljust(7)
        lcolor  = _LEVEL_COLOR.get(record.levelname, "")
        name    = _NAME_MAP.get(record.name, record.name[:9].upper().ljust(9))
        msg     = record.getMessage()

        # Highlight key tokens
        if record.levelname == "INFO":
            msg = _highlight(msg)

        line = f"{_DM}{time}{_R}  {lcolor}{level}{_R}  {_B}{_WH}{name}{_R}  {msg}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _highlight(msg: str) -> str:
    """Colour-accent arrows and check marks to make scan-reading faster."""
    msg = msg.replace("→", f"{_MG}→{_R}")
    msg = msg.replace("←", f"{_GR}←{_R}")
    msg = msg.replace("✓", f"{_GR}✓{_R}")
    msg = msg.replace("✗", f"{_RD}✗{_R}")
    msg = msg.replace("⏳", f"{_YL}⏳{_R}")
    msg = msg.replace("▶", f"{_CY}▶{_R}")
    return msg


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(HospilotFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "aiokafka", "asyncio",
                  "uvicorn.access", "kafka"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
