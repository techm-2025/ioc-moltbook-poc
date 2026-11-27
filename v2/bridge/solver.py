"""Solver for Moltbook's AI verification challenges.

Every post/comment the bridge creates returns an obfuscated math word problem that must be
answered within 5 minutes; unverified content stays hidden, and an account whose last 10
attempts all failed is suspended (https://www.moltbook.com/skill.md, "AI Verification
Challenges"). Each challenge has two numbers and one operation (+, -, *, /), e.g.

    "A] lO^bSt-Er S[wImS aT/ tW]eNn-Tyy mE^tE[rS aNd] SlO/wS bY^ fI[vE"   ->  20 - 5 = 15.00

calculate_answer() is imported by bridge_service.py (MoltbookClient.verify_content):
  1. LLM (primary): the LiteLLM proxy decodes the text into {a, op, b}; the arithmetic is
     done here, not by the model. Uses LITELLM_URL / LITELLM_API_KEY / LITELLM_MODEL
     (SOLVER_MODEL overrides the model).
  2. Rules (fallback and cross-check): symbols are deleted (not turned into spaces, which
     split words apart), doubled letters are collapsed ("twenntyy" -> "twenty"), words broken
     by spaces are re-joined, and the operation is found from keyword stems.
If both answer and disagree, the LLM answer is used and the disagreement is printed.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import List, Optional, Tuple

import requests

# --------------------------------------------------------------------------- rules

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
}


def _dedupe(s: str) -> str:
    """Collapse runs of the same letter: the obfuscation doubles letters at random."""
    return re.sub(r"(.)\1+", r"\1", s)


# Compare in collapsed form on both sides ("three" -> "thre", "seventeen" -> "seventen").
_DEDUPED_NUMBERS = {_dedupe(w): v for w, v in _NUMBER_WORDS.items()}
_MAX_WORD = max(len(k) for k in _DEDUPED_NUMBERS)

# Operation keyword stems, in collapsed form, by priority (first match wins).
_OPS: List[Tuple[str, Tuple[str, ...]]] = [
    ("/", ("divid", "split", "share", "halve", "quotient", "ratio")),
    ("*", ("times", "multipl", "product", "each")),
    ("-", ("minus", "subtract", "slow", "los", "drop", "decreas", "reduc", "fewer", "les",
           "fal", "fel", "sink", "sank", "shrink", "remov", "decelerat", "lower", "spend",
           "spent", "eat", "ate")),
    ("+", ("plus", "add", "ad", "gain", "increas", "acelerat", "sped", "grow", "grew", "rais",
           "rise", "rose", "more", "extra", "total", "sum", "combin", "join", "together")),
]
_EXACT_ONLY = {"ad", "les", "fal", "fel", "eat", "ate", "add"}  # too short to use as prefixes


def _tokens(text: str) -> List[str]:
    # Delete symbols outright: "tW]eNn-Tyy" must stay one word. Real spaces separate words.
    cleaned = re.sub(r"[^a-z0-9\s]", "", text.lower())
    return [_dedupe(t) for t in cleaned.split()]


@lru_cache(maxsize=4096)
def _segment(s: str) -> Optional[Tuple[int, ...]]:
    """Split a collapsed letter string fully into number words (longest first)."""
    if not s:
        return ()
    for n in range(min(_MAX_WORD, len(s)), 0, -1):
        head = s[:n]
        if head in _DEDUPED_NUMBERS:
            rest = _segment(s[n:])
            if rest is not None:
                return (_DEDUPED_NUMBERS[head],) + rest
    return None


def _combine(values: Tuple[int, ...]) -> Optional[int]:
    """Turn number-word values into one number; None if they do not form one ("five ten")."""
    total, cur, last = 0, 0, None
    for v in values:
        if v == 1000:
            total += (cur or 1) * 1000
            cur, last = 0, "k"
        elif v == 100:
            if last == "h":
                return None
            cur, last = (cur or 1) * 100, "h"
        elif v >= 20:
            if cur % 100:
                return None
            cur, last = cur + v, "t"
        elif v >= 10:
            if cur % 100:
                return None
            cur, last = cur + v, "teen"
        else:
            if last in ("u", "teen") or (cur % 10 and last != "h"):
                return None
            cur, last = cur + v, "u"
    return total + cur


def _parse_number(s: str) -> Optional[float]:
    if s.isdigit():
        return float(s)
    seg = _segment(s)
    if not seg:
        return None
    value = _combine(seg)
    return float(value) if value is not None else None


def extract_numbers(text: str) -> List[float]:
    toks = _tokens(text)
    nums: List[float] = []
    i = 0
    while i < len(toks):
        for k in (3, 2, 1):  # a number word may be broken into up to 3 pieces by spaces
            if i + k > len(toks):
                continue
            value = _parse_number(_dedupe("".join(toks[i:i + k])))
            if value is not None:
                nums.append(value)
                i += k
                break
        else:
            i += 1
    return nums


def detect_operation(text: str) -> str:
    toks = _tokens(text)
    # Candidate words: each token, and 2-3 adjacent tokens joined (words broken by spaces).
    cands = set(toks)
    for k in (2, 3):
        cands.update(_dedupe("".join(toks[i:i + k])) for i in range(len(toks) - k + 1))
    for op, stems in _OPS:
        for stem in stems:
            if stem in _EXACT_ONLY:
                if stem in cands or stem + "s" in cands or stem + "ed" in cands:
                    return op
            elif any(c.startswith(stem) for c in cands):
                return op
    return "+"


def _apply(a: float, op: str, b: float) -> Optional[float]:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a / b if b else None
    return None


def solve_rules(text: str) -> Optional[float]:
    nums = extract_numbers(text)
    if len(nums) < 2:
        return None
    return _apply(nums[0], detect_operation(text), nums[1])


# --------------------------------------------------------------------------- LLM

_SYSTEM_PROMPT = """You decode obfuscated math word problems used as anti-spam challenges.
The text uses random capitalisation, scattered symbols (] [ ^ / - etc.), doubled letters and
words broken into pieces. It always contains exactly two numbers (usually written as words)
and exactly one operation: addition, subtraction, multiplication or division.
Ignore the symbols, reconstruct the sentence, identify the two numbers in order and the operation.
Reply with JSON only: {"plain": "<decoded sentence>", "a": <first number>, "op": "+|-|*|/", "b": <second number>}"""


def _chat_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")


def solve_llm(text: str, timeout: int = 60) -> Tuple[Optional[float], str]:
    """Returns (answer, decoded sentence). (None, reason) when unavailable or unparseable."""
    key = os.getenv("LITELLM_API_KEY", "")
    url = os.getenv("LITELLM_URL", "")
    model = os.getenv("SOLVER_MODEL") or os.getenv("LITELLM_MODEL", "")
    if not (key and url and model):
        return None, "LLM not configured"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": text}],
        "temperature": 0,
        # reasoning models spend part of the budget on hidden reasoning before answering
        "max_completion_tokens": int(os.getenv("SOLVER_MAX_TOKENS", "4096")),
        "response_format": {"type": "json_object"},
        "user": "moltbook-challenge-solver",
    }
    session = requests.Session()
    session.trust_env = False
    try:
        res = session.post(_chat_url(url), json=body, timeout=timeout,
                           headers={"Authorization": f"Bearer {key}"})
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"] or ""
        match = re.search(r"\{.*\}", content, re.S)
        data = json.loads(match.group(0) if match else content)
        answer = _apply(float(data["a"]), str(data["op"]).strip(), float(data["b"]))
        return answer, str(data.get("plain", ""))
    except Exception as exc:  # network, HTTP, JSON or shape errors -> fall back to rules
        return None, f"LLM failed: {exc}"


# --------------------------------------------------------------------------- entry point

def calculate_answer(text: str) -> Optional[float]:
    llm, decoded = solve_llm(text)
    rules = solve_rules(text)
    print(f"[SOLVER] challenge: {text!r}")
    print(f"[SOLVER] llm={llm} ({decoded})  rules={rules}")
    if llm is not None and rules is not None and abs(llm - rules) > 0.005:
        print("[SOLVER] llm and rules disagree; using the llm answer")
    return llm if llm is not None else rules
