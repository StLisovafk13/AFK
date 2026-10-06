"""Serialization helpers for generated HTML documents."""

import json
from typing import Any


def json_for_html(value: Any) -> str:
    """Serialize data for an inline script without introducing HTML delimiters.

    HTML parsers recognize closing script tags even inside JavaScript strings.
    JSON escapes preserve the original data when JavaScript reads it. Values
    still need HTML escaping if subsequently rendered through innerHTML.
    """
    return json.dumps(value, ensure_ascii=False).translate({
        ord("<"): "\\u003c",
        ord(">"): "\\u003e",
        ord("&"): "\\u0026",
        ord("\u2028"): "\\u2028",
        ord("\u2029"): "\\u2029",
    })
