"""
Intent classifier for Pakistani social posts (TRD §1.3, FR-PK-29…38).

Loads the phrase bank at runtime (hot-reloadable); no category inventory
is hard-coded. Pure stdlib. Handles English, Roman Urdu, and Urdu script.
"""

from __future__ import annotations

import re
import unicodedata

# ----------------------------------------------------------------------
# Language markers (TRD FR-PK-31)
# ----------------------------------------------------------------------
ROMAN_URDU_MARKERS = (
    "chahiye", "hai", "hain", "mein", "kya", "koi", "wala", "wale",
    "liye", "karna", "chahye", "raha", "rahi", "karta", "karti", "hoga",
)

NOISE_WORDS = (
    "bhai", "sir", "janab", "plz", "please", "kindly",
    "aoa", "assalam", "alaikum", "salam", "wslm", "wsalam",
)

# Scam red-flag patterns (pk_localisation §3; TRD FR-PK-38)
SCAM_PATTERNS = [
    re.compile(r"(half[-\s]?price|90%\s*off).{0,60}(advance|payment\s*pehle|pehle\s*payment)", re.I),
    re.compile(r"(advance|payment).{0,60}(half[-\s]?price|90%\s*off)", re.I),
    re.compile(r"(no\s*cod|cod\s*(available\s*)?nahi|cod\s*refused)", re.I),
    re.compile(r"(video\s*call|meeting).{0,40}(nahi|no|refus|mana)", re.I),
    re.compile(r"(registration\s*fee|registration\s*charges).{0,40}(job|naukri|kaam|vacancy)", re.I),
    re.compile(r"(army|police|fauj|officer).{0,60}(advance|payment\s*bhej)", re.I),
    re.compile(r"(send\s*advance|advance\s*bhej).{0,40}(parcel|kal|tomorrow)", re.I),
]

PRICE_INQUIRY_PATTERNS = [
    re.compile(r"rate\s*kya\s*hai", re.I),
    re.compile(r"\bprice\s*\?", re.I),
    re.compile(r"\bdemand\s*\?", re.I),
    re.compile(r"kitne\s*ka", re.I),
]

# Provider-reply cues for comment threads (FR-PK-04)
PROVIDER_REPLY_PATTERNS = [
    re.compile(r"\bdm\s*me\b", re.I),
    re.compile(r"(rate|charges|fees?)\s*(hain|hai|is|:)", re.I),
    re.compile(r"(service|kam|kaam)\s*(available|mil\s*jay)", re.I),
    re.compile(r"(call|whatsapp|rabta)\s*(karein|karen|me)", re.I),
]

# Pakistani mobile: 03XX-XXXXXXX / +92 3XX XXXXXXX (TRD FR-PK-48 hard key)
PHONE_RE = re.compile(r"(?:\+92|0092)?[\s-]?(\(?0?3\d{2}\)?)[\s-]?(\d{7})\b")

# High-precision area patterns (FR-PK-33), checked before city aliases
AREA_PATTERNS = [
    (re.compile(r"\bdha\s*phase\s*(\d+)", re.I), "DHA Phase {0}"),
    (re.compile(r"\bbahria\s*town\s*(karachi|lahore)?", re.I), "Bahria Town"),
    (re.compile(r"\bjohar\s*town\b", re.I), "Johar Town"),
    (re.compile(r"\b([a-iA-I])-(\d{1,2})\b"), "{0}-{1}"),  # G-13, E-11 ...
]

SERIOUS_BUYER_RE = re.compile(r"sirf\s*serious\s*buyer\s*rabta\s*kare", re.I)

_bank_cache: dict | None = None


def get_bank(bank_path: str | None = None) -> dict:
    """Hot-reloadable phrase-bank accessor (FR-PK-29)."""
    global _bank_cache
    if _bank_cache is None:
        from config import load_phrase_bank
        _bank_cache = load_phrase_bank(bank_path)
    return _bank_cache


def reload_bank(bank_path: str | None = None) -> dict:
    global _bank_cache
    from config import load_phrase_bank
    _bank_cache = load_phrase_bank(bank_path)
    return _bank_cache


# ----------------------------------------------------------------------
# Normalization (TRD FR-PK-22/23/30)
# ----------------------------------------------------------------------
_ALEF_RE = re.compile(r"[\u0623\u0625\u0622]")          # أ إ آ -> ا
_HAMZA_RE = re.compile(r"[\u0624\u0626]")               # ؤ ئ -> ء
_KAF_RE = re.compile(r"\u0643")                         # ك -> ک
_YEH_RE = re.compile(r"[\u064A\u06D2]")                 # ي ے -> ی
_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0670]")
_TATWEEL_RE = re.compile(r"\u0640")
_ZW_RE = re.compile(r"[\u200C\u200D]")


def normalize_urdu_script(text: str) -> str:
    text = _ALEF_RE.sub("\u0627", text)
    text = _HAMZA_RE.sub("\u0621", text)
    text = _KAF_RE.sub("\u06A9", text)
    text = _YEH_RE.sub("\u06CC", text)
    text = _DIACRITICS_RE.sub("", text)
    text = _TATWEEL_RE.sub("", text)
    text = _ZW_RE.sub("", text)
    return text


def normalize_text(text: str) -> str:
    """Verbatim original is stored separately; this is the scoring copy."""
    text = unicodedata.normalize("NFKC", text)
    text = normalize_urdu_script(text)
    text = text.lower()
    # Punctuation (commas, !, ?, quotes, …) becomes spaces so phrase
    # matching is not blocked by it; decimals like 2.5 are preserved.
    text = re.sub(r"[^\w\s\u0600-\u06FF.]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_noise(text: str) -> str:
    """Remove politeness/honorific noise from the scoring copy only."""
    for word in NOISE_WORDS:
        text = re.sub(rf"(?<!\S){re.escape(word)}(?!\S)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_language(text: str) -> str:
    """en | roman_urdu | urdu | mixed | undetected (TRD FR-PK-31)."""
    if not text.strip():
        return "undetected"
    norm = normalize_text(text)
    has_urdu = bool(re.search(r"[\u0600-\u06FF]", text))
    tokens = re.findall(r"[a-zA-Z]+", norm)
    latin_words = len(tokens)
    roman_hits = sum(1 for t in tokens if t in ROMAN_URDU_MARKERS)
    latin_substantial = latin_words >= 4
    if has_urdu and latin_substantial:
        return "mixed"
    if has_urdu:
        return "urdu"
    if roman_hits >= 2 or (roman_hits >= 1 and latin_words >= 2):
        return "roman_urdu"
    if latin_words:
        return "en"
    return "undetected"


# ----------------------------------------------------------------------
# Intent matching (TRD FR-PK-32)
# ----------------------------------------------------------------------
def _phrase_matches(norm_text: str, phrase: str) -> tuple[bool, int, bool]:
    """(full_match, best_contiguous_run, run_is_generic_filler) for one phrase.

    Partials only count when phrase tokens appear as a CONTIGUOUS
    subsequence in the text — scattered tokens ("kaam ... chahiye") are
    not evidence of the phrase's intent. Runs made entirely of generic
    filler words ("looking for", "need a") carry no category signal and
    are flagged so callers can ignore them.
    """
    phrase_n = normalize_text(phrase)
    padded = f" {norm_text} "
    if f" {phrase_n} " in padded or norm_text == phrase_n:
        return True, len(phrase_n.split()), False
    ptoks = phrase_n.split()
    best, best_sub = 0, ""
    for n in range(2, len(ptoks) + 1):
        for i in range(len(ptoks) - n + 1):
            sub = " ".join(ptoks[i:i + n])
            if (f" {sub} " in padded or norm_text.startswith(sub + " ")
                    or norm_text.endswith(" " + sub)):
                if n > best:
                    best, best_sub = n, sub
    generic = bool(best_sub) and all(t in FILLER_WORDS for t in best_sub.split())
    return False, best, generic


# Generic filler words: a run made only of these is not category evidence.
FILLER_WORDS = frozenset({
    "looking", "for", "need", "want", "a", "an", "the", "to", "get",
    "buy", "find", "koi", "kya", "hai", "mein", "of", "in", "on",
})


def classify_intent(text: str, bank: dict) -> dict:
    """Return {primary, secondary, scores{cat: weighted}, matched_phrases}."""
    norm = strip_noise(normalize_text(text))
    cat_scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for cat, data in (bank.get("categories") or {}).items():
        weight = float(data.get("weight", 1))
        phrases = (data.get("phrases_en", []) + data.get("phrases_roman_urdu", [])
                   + data.get("phrases_urdu", []))
        full = 0
        partial_tokens = 0
        hits: list[str] = []
        for phrase in phrases:
            is_full, tok_hits, generic = _phrase_matches(norm, phrase)
            if is_full:
                full += 1
                hits.append(phrase)
            elif tok_hits >= max(2, len(normalize_text(phrase).split()) // 2) \
                    and not generic:
                partial_tokens += tok_hits
        score = weight * (full + 0.25 * min(partial_tokens, 4))
        if full >= 1 or partial_tokens >= 2:  # minimum match threshold (FR-PK-32)
            cat_scores[cat] = score
            matched[cat] = hits
    ranked = sorted(cat_scores.items(), key=lambda kv: kv[1], reverse=True)
    primary = ranked[0][0] if ranked else "unclear"
    secondary = None
    if len(ranked) > 1 and ranked[1][1] >= 0.40 * ranked[0][1]:
        secondary = ranked[1][0]
    return {
        "primary": primary,
        "secondary": secondary,
        "scores": cat_scores,
        "matched_phrases": matched,
    }


# ----------------------------------------------------------------------
# City extraction (TRD FR-PK-33)
# ----------------------------------------------------------------------
def _match_alias(text: str, alias: str) -> bool:
    if re.search(r"[a-zA-Z]", alias):
        return re.search(rf"(?<!\S){re.escape(alias)}(?!\S)", text, re.I) is not None
    return alias in text


def extract_city(text: str, bank: dict, source_cities: list[str] | None = None) -> dict:
    """{city, area, how} — stated city beats source geography; never invented."""
    norm = strip_noise(normalize_text(text))
    area = None
    area_city_hint = None
    for pattern, template in AREA_PATTERNS:
        m = pattern.search(text)
        if m:
            groups = [g for g in m.groups() if g]
            area = template.format(*groups) if groups else template
            if area.lower().startswith("bahria town") and groups:
                area_city_hint = groups[0].capitalize()
            break

    cities = bank.get("cities", {})
    found: list[str] = []
    for city, data in cities.items():
        for alias in data.get("aliases", []):
            if _match_alias(norm, alias):
                found.append(city)
                break
    # Isb/Rwp pairs tag both (TRD FR-PK-33)
    pair = {"Islamabad", "Rawalpindi"}
    if pair <= set(found):
        found = sorted(pair)
    if area_city_hint and area_city_hint in cities and area_city_hint not in found:
        found.append(area_city_hint)

    city = found[0] if found else None
    how = "area_pattern" if (area and city) else ("alias" if city else
          ("source_scope" if source_cities else "none"))
    if not city and source_cities:
        city = source_cities[0]
    return {"city": city, "area": area, "how": how}


# ----------------------------------------------------------------------
# Budget extraction (TRD FR-PK-34)
# ----------------------------------------------------------------------
def _parse_amount(num_str: str, suffix: str | None) -> int | None:
    try:
        value = float(num_str.replace(",", ""))
    except ValueError:
        return None
    mult = 1
    s = (suffix or "").lower()
    if s in ("k", "thousand"):
        mult = 1_000
    elif s in ("lac", "lakh"):
        mult = 100_000
    elif s in ("crore", "cr"):
        mult = 10_000_000
    return int(round(value * mult))


def extract_budget(text: str, bank: dict) -> dict:
    """{budget_pkr, qualifier, raw} — PKR normalized. Marla = per-unit."""
    patterns = bank.get("budget_patterns", [])
    best: dict = {"budget_pkr": None, "qualifier": None, "raw": None}
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        chunk = m.group(0)
        qualifier = None
        for q in ("demand", "final", "budget", "per_marla"):
            if q.replace("_", " ") in chunk.lower():
                qualifier = q
                break
        num_m = re.search(r"[\d,]+(?:\.\d+)?", chunk)
        if not num_m:
            continue
        suffix = None
        after = chunk[num_m.end():].strip().lower()
        for s in ("thousand", "crore", "lakh", "lac", "cr", "k"):
            if after.startswith(s):
                suffix = s
                break
        amount = _parse_amount(num_m.group(0), suffix)
        if amount is not None and (best["budget_pkr"] is None or amount > best["budget_pkr"]):
            best = {"budget_pkr": amount, "qualifier": qualifier, "raw": chunk.strip()}
    return best


# ----------------------------------------------------------------------
# Urgency, price inquiry, phones, scam (TRD FR-PK-35/36/38)
# ----------------------------------------------------------------------
def extract_urgency(text: str, bank: dict, cap: int = 4) -> list[str]:
    norm = strip_noise(normalize_text(text))
    hits: list[str] = []
    for marker in bank.get("urgency_markers", []):
        m = normalize_text(marker)
        if m and f" {m} " in f" {norm} ":
            hits.append(marker)
        if len(hits) >= cap:
            break
    return hits


def has_price_inquiry(text: str) -> bool:
    return any(p.search(text) for p in PRICE_INQUIRY_PATTERNS)


def normalize_phone(text: str) -> str | None:
    """Canonical 11-digit 03XXXXXXXXX form, or None."""
    m = PHONE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if digits.startswith("92"):
        digits = "0" + digits[2:]
    elif digits.startswith("0092"):
        digits = "0" + digits[4:]
    if len(digits) == 11 and digits.startswith("03"):
        return digits
    return None


def contact_type_of(text: str, has_comments: bool = False) -> tuple[str, str | None]:
    phone = normalize_phone(text)
    if phone:
        ctype = "whatsapp" if re.search(r"whats?app", text, re.I) else "phone"
        return ctype, phone
    if re.search(r"\bdm\b.{0,30}(price|rate|detail)", text, re.I) or \
       re.search(r"inbox\s*(karein|karen|me|mein)", text, re.I):
        return "inbox", None
    if has_comments:
        return "inbox", None
    return "none", None


def is_scam_suspect(text: str) -> bool:
    return any(p.search(text) for p in SCAM_PATTERNS)


def is_provider_reply(text: str) -> bool:
    return any(p.search(text) for p in PROVIDER_REPLY_PATTERNS)


def classify_comment(text: str, bank: dict) -> dict:
    """Per-comment classification: intent + public phone + provider-reply flag."""
    return {
        "intent_primary": classify_intent(text, bank)["primary"],
        "contact_published": normalize_phone(text),
        "is_provider_reply": is_provider_reply(text),
    }


def classify_post(text: str, bank: dict, source_cities: list[str] | None = None,
                  has_comments: bool = False) -> dict:
    """Full per-post classification pass used by the pipeline."""
    intent = classify_intent(text, bank)
    city = extract_city(text, bank, source_cities)
    budget = extract_budget(text, bank)
    urgency = extract_urgency(text, bank)
    language = detect_language(text)
    ctype, cphone = contact_type_of(text, has_comments)
    return {
        "language_mode": language,
        "intent_primary": intent["primary"],
        "intent_secondary": intent["secondary"],
        "intent_scores": intent["scores"],
        "city_tag": city["city"],
        "area_tag": city["area"],
        "price_inquiry": has_price_inquiry(text),
        "budget_pkr": budget["budget_pkr"],
        "budget_qualifier": budget["qualifier"],
        "urgency_markers": urgency,
        "contact_published": cphone,   # public-post phones only (SHR-3)
        "contact_type": ctype,
        "serious_buyer": bool(SERIOUS_BUYER_RE.search(text)),
        "scam_suspect": is_scam_suspect(text),
        "text_normalized": strip_noise(normalize_text(text)),
    }
