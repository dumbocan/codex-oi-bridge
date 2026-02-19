"""Web task parsing and lightweight step rewrites."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CLICK_TEXT_RE = re.compile(
    r"(?:click|haz\s+click|pulsa|presiona)[^\"'<>]{0,120}[\"'“”]([^\"'“”]{1,120})[\"'“”]",
    flags=re.IGNORECASE,
)
_SELECTOR_RE = re.compile(
    r"selector\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_CLICK_SELECTOR_RE = re.compile(
    r"(?:click|haz\s+click|pulsa|presiona)\s+(?:en\s+)?(?:el\s+)?"
    r"selector\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_CLICK_SELECTOR_UNQUOTED_RE = re.compile(
    r"(?:click|haz\s+click|pulsa|presiona)\s+(?:en\s+)?(?:el\s+)?"
    r"selector\s*[=:]?\s*([#.\[][^\s,;]{1,200})",
    flags=re.IGNORECASE,
)
_BULK_CLICK_IN_CARDS_RE = re.compile(
    r"bulk\s+click\s+(?:selector\s*)?[\"'“”]([^\"'“”]{1,160})[\"'“”]\s+"
    r"(?:in|on)\s+cards\s+[\"'“”]([^\"'“”]{1,120})[\"'“”]\s+"
    r"where\s+text\s+[\"'“”]([^\"'“”]{1,120})[\"'“”]",
    flags=re.IGNORECASE,
)
_BULK_CLICK_UNTIL_EMPTY_RE = re.compile(
    r"bulk\s+click\s+(?:selector\s*)?[\"'“”]([^\"'“”]{1,160})[\"'“”]\s+until\s+empty",
    flags=re.IGNORECASE,
)
_SELECT_LABEL_RE = re.compile(
    r"\b(?:select|selecciona)\b[^\n\r]{0,120}?"
    r"(?:label|texto|opci[oó]n|option)?\s*[=:]?\s*"
    r"[\"'“”]([^\"'“”]{1,120})[\"'“”][^\n\r]{0,120}?"
    r"(?:from|en)\s+(?:selector\s*[=:]?\s*)?"
    r"[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_SELECT_VALUE_RE = re.compile(
    r"\b(?:select|selecciona)\b[^\n\r]{0,80}?value\s*[=:]?\s*"
    r"[\"'“”]([^\"'“”]{1,120})[\"'“”][^\n\r]{0,80}?"
    r"(?:from|en)\s+(?:selector\s*[=:]?\s*)?"
    r"[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_FILL_SELECTOR_TEXT_RE = re.compile(
    r"(?:type|fill|escribe|rellena|teclea)\b[^\n\r]{0,80}?"
    r"(?:text|texto)?\s*[=:]?\s*[\"'“”]([^\"'“”]{1,240})[\"'“”][^\n\r]{0,120}?"
    r"(?:in|into|en)\s+(?:selector\s*[=:]?\s*)?[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_FILL_SELECTOR_TEXT_RE_ALT = re.compile(
    r"(?:type|fill|escribe|rellena|teclea)\b[^\n\r]{0,80}?"
    r"(?:in|into|en)\s+(?:selector\s*[=:]?\s*)?[\"'“”]([^\"'“”]{1,160})[\"'“”][^\n\r]{0,120}?"
    r"(?:text|texto)?\s*[=:]?\s*[\"'“”]([^\"'“”]{1,240})[\"'“”]",
    flags=re.IGNORECASE,
)
_WAIT_SELECTOR_RE = re.compile(
    r"(?:wait|espera)(?:\s+for)?\s+selector\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_WAIT_TEXT_RE = re.compile(
    r"(?:wait|espera)(?:\s+for)?\s+text\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class WebStep:
    kind: str
    target: str
    value: str = ""


def parse_steps(task: str) -> list[WebStep]:
    captures: list[tuple[int, int, WebStep]] = []

    for match in _BULK_CLICK_IN_CARDS_RE.finditer(task):
        packed = f"{match.group(2).strip()}||{match.group(3).strip()}"
        captures.append((match.start(), match.end(), WebStep("bulk_click_in_cards", match.group(1).strip(), packed)))
    for match in _BULK_CLICK_UNTIL_EMPTY_RE.finditer(task):
        captures.append((match.start(), match.end(), WebStep("bulk_click_until_empty", match.group(1).strip())))
    for match in _FILL_SELECTOR_TEXT_RE.finditer(task):
        captures.append(
            (
                match.start(),
                match.end(),
                WebStep("fill_selector", match.group(2).strip(), match.group(1).strip()),
            )
        )
    for match in _FILL_SELECTOR_TEXT_RE_ALT.finditer(task):
        captures.append(
            (
                match.start(),
                match.end(),
                WebStep("fill_selector", match.group(1).strip(), match.group(2).strip()),
            )
        )
    for match in _SELECT_VALUE_RE.finditer(task):
        captures.append(
            (
                match.start(),
                match.end(),
                WebStep("select_value", match.group(2).strip(), match.group(1).strip()),
            )
        )
    for match in _SELECT_LABEL_RE.finditer(task):
        captures.append(
            (
                match.start(),
                match.end(),
                WebStep("select_label", match.group(2).strip(), match.group(1).strip()),
            )
        )
    for match in _WAIT_SELECTOR_RE.finditer(task):
        captures.append((match.start(), match.end(), WebStep("wait_selector", match.group(1).strip())))
    for match in _WAIT_TEXT_RE.finditer(task):
        captures.append((match.start(), match.end(), WebStep("wait_text", match.group(1).strip())))
    for match in _CLICK_SELECTOR_RE.finditer(task):
        captures.append((match.start(), match.end(), WebStep("click_selector", match.group(1).strip())))
    for match in _CLICK_SELECTOR_UNQUOTED_RE.finditer(task):
        captures.append((match.start(), match.end(), WebStep("click_selector", match.group(1).strip())))

    if captures:
        captures.sort(key=lambda item: item[0])
        filtered: list[tuple[int, int, WebStep]] = []
        last_end = -1
        for start, end, step in captures:
            if start >= last_end:
                filtered.append((start, end, step))
                last_end = end
        tail_texts = _text_clicks_outside_spans(task, [(start, end) for start, end, _ in filtered])
        for start, _end, text in tail_texts:
            filtered.append((start, start, WebStep("click_text", text)))
        filtered.sort(key=lambda item: item[0])
        return [step for _, _, step in filtered]

    steps: list[WebStep] = []
    for match in _WAIT_SELECTOR_RE.finditer(task):
        steps.append(WebStep("wait_selector", match.group(1).strip()))
    for match in _WAIT_TEXT_RE.finditer(task):
        steps.append(WebStep("wait_text", match.group(1).strip()))
    for match in _BULK_CLICK_IN_CARDS_RE.finditer(task):
        packed = f"{match.group(2).strip()}||{match.group(3).strip()}"
        steps.append(WebStep("bulk_click_in_cards", match.group(1).strip(), packed))
    for match in _BULK_CLICK_UNTIL_EMPTY_RE.finditer(task):
        steps.append(WebStep("bulk_click_until_empty", match.group(1).strip()))
    for match in _FILL_SELECTOR_TEXT_RE.finditer(task):
        steps.append(WebStep("fill_selector", match.group(2).strip(), match.group(1).strip()))
    for match in _FILL_SELECTOR_TEXT_RE_ALT.finditer(task):
        steps.append(WebStep("fill_selector", match.group(1).strip(), match.group(2).strip()))
    for match in _SELECT_LABEL_RE.finditer(task):
        steps.append(WebStep("select_label", match.group(2).strip(), match.group(1).strip()))
    for match in _SELECT_VALUE_RE.finditer(task):
        steps.append(WebStep("select_value", match.group(2).strip(), match.group(1).strip()))
    for match in _SELECTOR_RE.finditer(task):
        steps.append(WebStep("click_selector", match.group(1).strip()))
    for match in _CLICK_TEXT_RE.finditer(task):
        steps.append(WebStep("click_text", match.group(1).strip()))
    return steps


def _text_clicks_outside_spans(task: str, spans: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for match in _CLICK_TEXT_RE.finditer(task):
        start, end = match.span()
        if any((start < s_end and end > s_start) for s_start, s_end in spans):
            continue
        found.append((start, end, match.group(1).strip()))
    return found
