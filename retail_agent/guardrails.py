"""Input and output guardrails, enforced in code (ADK callbacks), not in prompts.

Input  (orchestrator before_model_callback):
    - PII masking: T.C. kimlik no, phone, card number, IBAN, e-mail never reach the model.
    - Prompt injection: known override phrases are blocked with a fixed answer; the model is not called.

Output (every agent):
    - after_tool_callback records what the tools actually returned (numbers, source ids).
    - after_model_callback checks the final answer against that evidence:
        * every number in the answer must appear in tool output (analytics),
        * every [n] citation must point to a returned source, and RAG answers must cite (policy/catalog).
      Violations are appended as a visible warning and logged, so they show up in evals.

Regex rules are a deterministic first line. In production I would add Model Armor (or an
LLM classifier) for injection and DLP for PII, keeping these rules as a cheap, testable baseline.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

logger = logging.getLogger(__name__)

BLOCKED_MESSAGE = "Bu isteği işleyemiyorum. Mağaza operasyonlarıyla ilgili bir soru sorabilirsiniz."
# "I found no information" answers legitimately have no citation
_NOT_FOUND = re.compile(r"bilgi\s+(bulamadım|bulunamadı|bulunmamaktadır|yok)")

# ---------------------------------------------------------------- PII


def _valid_tckn(digits: str) -> bool:
    """T.C. kimlik no checksum, so random 11-digit numbers (order ids etc.) are not masked."""
    d = [int(c) for c in digits]
    if len(d) != 11 or d[0] == 0:
        return False
    tenth = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    return d[9] == tenth and d[10] == sum(d[:10]) % 10


def _valid_luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, c in enumerate(digits):
        n = int(c)
        if i % 2 == parity:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IBAN = re.compile(r"\bTR\d{2}(?:\s?\d{4}){5}\s?\d{2}\b", re.IGNORECASE)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_TCKN = re.compile(r"(?<!\d)\d{11}(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+90[ -]?|0)?5\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)")


def mask_pii(text: str) -> tuple[str, list[str]]:
    """Return (masked text, list of PII types found). Order matters: longest patterns first."""
    found: list[str] = []

    def sub(pattern: re.Pattern[str], label: str, check: Any = None) -> None:
        nonlocal text

        def repl(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group())
            if check and not check(digits):
                return m.group()
            found.append(label)
            return f"[{label}]"

        text = pattern.sub(repl, text)

    sub(_EMAIL, "EPOSTA")
    sub(_IBAN, "IBAN")
    sub(_CARD, "KART", _valid_luhn)
    sub(_TCKN, "TCKN", _valid_tckn)
    sub(_PHONE, "TELEFON")
    return text, found


# ---------------------------------------------------------------- prompt injection

_INJECTION_PATTERNS = [
    r"(önceki|yukarıdaki|tüm|bütün)\s+(talimat|kural|komut|yönerge)\w*\s+(unut|yok\s*say|görmezden\s*gel|iptal)",
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
    r"(sistem|system)\s*(prompt|talimat|mesaj)\w*\s*\w*\s*(göster|yaz|ver|paylaş|nedir|reveal|show|print)",
    r"(you\s+are\s+now|artık\s+sen)\b.{0,40}\b(dan|developer\s+mode|geliştirici\s+mod|kısıtlamasız|sınırsız)",
    r"\bjailbreak\b",
]
_INJECTION = [re.compile(p) for p in _INJECTION_PATTERNS]


def _normalize(text: str) -> str:
    # Turkish dotted capital I lowercases to "i" + combining dot in Python; map it explicitly.
    return text.replace("İ", "i").lower()


def detect_injection(text: str) -> bool:
    t = _normalize(text)
    return any(p.search(t) for p in _INJECTION)


def input_guardrail(callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse | None:
    """Orchestrator before_model_callback: mask PII in every user message, block injections."""
    last_user_text: str | None = None
    for content in llm_request.contents:
        if content.role != "user" or not content.parts:
            continue
        for part in content.parts:
            if part.text:
                part.text, found = mask_pii(part.text)
                if found:
                    logger.info("guardrail: masked %s", sorted(set(found)))
                last_user_text = part.text

    if last_user_text and detect_injection(last_user_text):
        logger.warning("guardrail: blocked prompt injection: %r", last_user_text[:200])
        callback_context.state["temp:guardrail_blocked"] = True
        return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=BLOCKED_MESSAGE)]))
    return None


# ---------------------------------------------------------------- numbers

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[./]\d{1,2}[./]\d{4}\b")
_CITATION = re.compile(r"\[(\d{1,2})\]")
_SECTION = re.compile(r"\b(?:madde|pol-)\s*\d+(?:\.\d+)*", re.IGNORECASE)
_NUMBER = re.compile(r"%?\s?\d{1,3}(?:\.\d{3})+(?:,\d+)?|%?\s?\d+(?:,\d+)?")


def parse_tr_numbers(text: str) -> list[tuple[float, bool]]:
    """Extract numbers written the Turkish way: 1.281 / 39.044.323,59 / %55,64.

    Returns (value, is_percent). Dates, citation markers and section numbers are ignored.
    """
    text = _DATE.sub(" ", text)
    text = _CITATION.sub(" ", text)
    text = _SECTION.sub(" ", text)
    out = []
    for m in _NUMBER.finditer(text):
        token = m.group().replace(" ", "")
        is_pct = token.startswith("%")
        token = token.lstrip("%").replace(".", "").replace(",", ".")
        try:
            out.append((float(token), is_pct))
        except ValueError:
            continue
    return out


def _collect_numbers(obj: Any, acc: set[float]) -> None:
    """Numbers from structured tool output. Strings like "1281" or "0.5564" are machine format."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, str):
        try:
            acc.add(float(obj))
        except ValueError:
            # free text (passages, a specialist's answer seen by the orchestrator): Turkish format
            for v, pct in parse_tr_numbers(obj):
                acc.add(v)
                if pct:
                    acc.add(v / 100)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect_numbers(v, acc)


def _grounded(value: float, is_pct: bool, evidence: set[float]) -> bool:
    if is_pct and value == 100:
        return True  # "Toplam %100" row
    if not is_pct and value.is_integer() and value <= 31:
        return True  # list numbering, "ilk 5", days of month
    if not is_pct and value.is_integer() and 1900 <= value <= 2100:
        return True  # years
    candidates = [value / 100, value] if is_pct else [value]
    for c in candidates:
        for e in evidence:
            if abs(c - e) <= 0.01:  # exact, up to kuruş
                return True
            if is_pct and abs(c - e) <= 0.00006:  # 0.556398 shown as %55,64
                return True
            if c.is_integer() and abs(c - e) < 1:  # 2.340.977 shown for 2340977.77
                return True
    return False


def ungrounded_numbers(answer: str, evidence: set[float]) -> list[str]:
    bad = []
    for value, is_pct in parse_tr_numbers(answer):
        if not _grounded(value, is_pct, evidence):
            bad.append(f"%{value:g}" if is_pct else f"{value:g}")
    return bad


def citation_problems(answer: str, source_ids: set[int], searched: bool) -> list[str]:
    cited = {int(n) for n in _CITATION.findall(answer)}
    problems = [f"[{n}] dönen kaynaklar arasında yok" for n in sorted(cited - source_ids)]
    if searched and source_ids and not cited and not _NOT_FOUND.search(_normalize(answer)):
        problems.append("cevapta kaynak gösterimi yok")
    return problems


# ---------------------------------------------------------------- callbacks


def _key(ctx: CallbackContext, name: str) -> str:
    # temp: state lives for one invocation only; agent name keeps specialists from mixing evidence
    return f"temp:{ctx.agent_name}:{name}"


def record_tool_evidence(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: Any
) -> dict | None:
    """after_tool_callback: remember what the tool actually returned."""
    numbers: set[float] = set(tool_context.state.get(_key(tool_context, "numbers"), []))
    _collect_numbers(tool_response, numbers)
    _collect_numbers(args, numbers)
    tool_context.state[_key(tool_context, "numbers")] = sorted(numbers)
    tool_context.state[_key(tool_context, "called")] = True

    ids: set[int] = set(tool_context.state.get(_key(tool_context, "source_ids"), []))
    if isinstance(tool_response, dict) and "sources" in tool_response:
        ids.update(s["source_id"] for s in tool_response["sources"])
        tool_context.state[_key(tool_context, "searched")] = True
    # AgentTool returns the specialist's answer as a string (shown as {"result": ...} in events)
    text = tool_response if isinstance(tool_response, str) else ""
    if isinstance(tool_response, dict) and isinstance(tool_response.get("result"), str):
        text = tool_response["result"]
    if text:
        ids.update(int(n) for n in _CITATION.findall(text))
    tool_context.state[_key(tool_context, "source_ids")] = sorted(ids)
    return None  # keep the original tool response


def output_guardrail(callback_context: CallbackContext, llm_response: LlmResponse) -> LlmResponse | None:
    """after_model_callback: check the final answer against recorded tool evidence."""
    content = llm_response.content
    if not content or not content.parts or any(p.function_call for p in content.parts):
        return None  # intermediate step (tool call), not a final answer
    text = "".join(p.text or "" for p in content.parts if not p.thought)
    if not text.strip() or callback_context.state.get("temp:guardrail_blocked"):
        return None

    state = callback_context.state
    evidence = set(state.get(_key(callback_context, "numbers"), []))
    problems = []
    bad_numbers = ungrounded_numbers(text, evidence)
    if bad_numbers:
        problems.append("araç sonuçlarında olmayan sayılar: " + ", ".join(bad_numbers[:8]))
    problems += citation_problems(
        text,
        set(state.get(_key(callback_context, "source_ids"), [])),
        bool(state.get(_key(callback_context, "searched"))),
    )
    if not problems:
        return None

    logger.warning("guardrail[%s]: %s", callback_context.agent_name, "; ".join(problems))
    state[_key(callback_context, "flags")] = json.dumps(problems, ensure_ascii=False)
    warning = "\n\n⚠️ Otomatik kontrol: " + "; ".join(problems) + ". Lütfen bu cevabı dikkatle değerlendirin."
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text + warning)]))