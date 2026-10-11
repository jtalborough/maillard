"""Shared utilities for AI modules."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

_logger = logging.getLogger(__name__)


def parse_llm_json(text: str) -> dict[str, Any] | None:
    """Parse JSON from LLM response, handling code fences and extra text.

    Tries three strategies in order:
    1. Direct JSON parse
    2. Extract from markdown code fences
    3. Find first JSON object via regex

    Returns None if all strategies fail.
    """
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    _logger.debug("Failed to parse LLM JSON: %s", text[:200])
    return None
