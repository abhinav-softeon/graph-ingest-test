"""Stand-in for developer_assistant's sail_core.logger.logger shim.

The copied graph_core/ files all do `from sail_core.logger.logger import
get_logger` — this local package matches that exact dotted import path (same
name, same nesting) so none of those copied files need any edit for logging.
Behavior here is plain stdlib logging instead of sail_core's real backend.
"""
from __future__ import annotations

import logging
import os

_CONFIGURED = False


def _configure_once() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("GRAPH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_once()
    return logging.getLogger(name)
