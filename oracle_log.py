"""Oracle — Event Log

Purpose:
    Append-only text log (logs/oracle.log) plus a helper returning its tail.

Role in Oracle:
    Legacy diagnostics layer. Claude retries/failures and errors shown to the engineer are
    written here, and the wizard's chat reads the tail as troubleshooting context.

Dependencies:
    config (LOGS_DIR).

Consumers:
    claude_ga_generator, design_module, oracle_wizard.

Status:
    Legacy / Transitional.

Migration:
    Retained; can later be replaced by the standard logging module behind the same two functions.

Details (original module notes, retained):
    Shared event log for Oracle. Every Claude call retry/failure and every
    error shown to the user gets appended here (full detail, not a short
    preview), so both the user and the Ask Claude chat -- which reads this
    file's tail as troubleshooting context -- have something real to diagnose
    from after the fact.
"""

from datetime import datetime

from config import LOGS_DIR

LOG_PATH = LOGS_DIR / "oracle.log"


def log_event(kind, message):
    timestamp = datetime.now().isoformat(timespec="seconds")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {kind}\n{message}\n{'-' * 60}\n")


def read_recent_log(max_chars=4000):
    """Tail of the log file, for feeding into the chat's context."""
    if not LOG_PATH.exists():
        return ""
    text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]
