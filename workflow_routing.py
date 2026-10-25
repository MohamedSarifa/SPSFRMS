"""Database-free complaint classification and canonical department routing."""

import re
import unicodedata

# Kept out of the active classifier and routing map. This value appears only
# in legacy/manual data and requires review before any department can act on it.
MULTI_DEPARTMENT_LEGACY_CATEGORY = "Multiple Departments"

CATEGORY_DEPARTMENTS = {
    "Road Damage": "Road Maintenance",
    "Garbage": "Waste Management",
    "Water Leakage": "Water Supply",
    "Water Supply": "Water Supply",
    "Street Light": "Street Light",
    "Drainage": "Drainage",
    "Electricity": "Electricity",
    "Traffic": "Police",
    "Fire": "Fire Department",
    "Public Health": "Public Health",
    "Other": "Public Health",
}

LEGACY_DEPARTMENT_NAMES = {
    "Road Maintenance": {"Road Damage"},
    "Waste Management": {"Garbage"},
    "Water Supply": {"Water Leakage"},
    "Street Light": set(),
    "Public Health": {"Other"},
    "Electricity": set(),
    "Police": {"Traffic"},
    "Fire Department": {"Fire"},
}

# Phrase weights emphasize actionable, issue-specific evidence. Low weights are
# retained for useful legacy synonyms, but cannot independently select a route.
CLASSIFICATION_RULES = {
    "Drainage": [
        ("drainage channel is blocked", 6), ("blocked drainage", 6),
        ("clogged drainage", 6), ("drainage water on road", 6),
        ("drainage water onto road", 6), ("dirty water collecting on street", 6),
        ("dirty water pooling on street", 6), ("stagnant water on street", 5),
        ("sewage water", 6), ("wastewater leakage", 6), ("waste water leakage", 6),
        ("sewage", 4), ("wastewater", 4), ("waste water", 4),
        ("drainage", 4), ("drain", 4), ("sewer", 4), ("manhole", 3),
        ("waterlogging", 4), ("storm drain", 4), ("blocked drain", 6),
        ("clogged drain", 6), ("drain blocked", 6), ("drain overflow", 6),
        ("overflowing drain", 6), ("drain overflowing", 6),
        ("dirty water is collecting on the street", 6),
        ("water overflowing onto road", 5), ("overflowing water onto road", 5),
    ],
    "Water Leakage": [
        ("leaking drinking water pipeline", 6), ("leaking water pipeline", 6),
        ("water pipeline leak", 6), ("water pipe burst", 6), ("burst water pipe", 6),
        ("water is leaking", 5), ("water leak", 4), ("water leaking", 4),
        ("water leakage", 4), ("leaking pipe", 4), ("burst pipe", 4),
        ("pipe burst", 4), ("pipeline burst", 5),
    ],
    "Water Supply": [
        ("drinking water shortage", 6), ("no municipal drinking water", 6),
        ("no municipal water supply", 6), ("no drinking water supply", 6),
        ("no water supply", 6), ("water supply shortage", 6),
        ("not received drinking water", 6), ("not received municipal drinking water", 6),
        ("bad smell in the supplied water", 6), ("bad smell in supplied water", 6),
        ("bad smell in drinking water", 6), ("dirty drinking water", 6),
        ("contaminated drinking water", 6), ("unsafe drinking water", 6),
        ("contaminated water", 5), ("drinking water pipeline", 5),
        ("water supplied to our houses", 5),
        ("municipal drinking water", 4), ("drinking water", 4),
        ("water supply", 4), ("tap water", 3), ("no water", 3),
        ("low water pressure", 4),
    ],
    "Public Health": [
        ("disease outbreak", 6), ("food poisoning", 6), ("mosquito breeding", 6),
        ("health risk", 5), ("health hazard", 5), ("unsafe sanitation", 5),
        ("mosquitoes are breeding", 6),
        ("drinking water quality", 5), ("bad smell in the supplied water", 5),
        ("bad smell in supplied water", 5), ("bad smell in drinking water", 5),
        ("water supplied to our houses has a bad smell", 6),
        ("contaminated drinking water", 4), ("contaminated water", 4),
        ("unsafe drinking water", 4), ("dirty drinking water", 4),
        ("epidemic", 5), ("dengue", 5), ("malaria", 5), ("public health", 4),
    ],
    "Street Light": [
        ("street light", 4), ("streetlight", 4), ("lamp post", 3), ("lamp", 2),
    ],
    "Road Damage": [
        ("road surface damage", 6), ("damaged road surface", 6), ("damaged road", 6),
        ("road repair", 5), ("repair road", 5), ("broken road", 6),
        ("road cracks", 5), ("road crack", 5), ("road damage", 6),
        ("potholes", 6), ("pothole", 6), ("footpath damage", 4),
        ("footpath", 2), ("pavement", 2), ("speed breaker", 2),
    ],
    "Garbage": [
        ("garbage", 4), ("waste", 2), ("trash", 4), ("dump", 2), ("litter", 3), ("rubbish", 3),
    ],
    "Electricity": [
        ("live wire", 6), ("electric shock", 6), ("power outage", 4),
        ("transformer", 4), ("electricity", 3), ("electric", 2),
        ("wire", 1), ("current", 1),
    ],
    "Traffic": [
        ("traffic signal", 5), ("traffic jam", 4), ("traffic", 3),
        ("vehicle", 1), ("congestion", 3),
    ],
    "Fire": [
        ("fire", 6), ("smoke", 3), ("burning", 4), ("flame", 5),
    ],
}

HIGH_PRIORITY_WORDS = [
    "fire", "accident", "emergency", "electric shock", "live wire", "collapse",
    "explosion", "gas leak", "injured", "injury", "trapped", "rescue",
]
MEDIUM_PRIORITY_WORDS = [
    "broken", "leak", "blocked", "damage", "traffic", "pothole", "overflow", "overflowing",
    "difficulty", "unsafe", "sewage", "not working", "contaminated",
]
CLASSIFICATION_THRESHOLD = 3


def normalize_complaint_text(text):
    """Normalize Unicode, case, punctuation, and whitespace for all rule matching."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def contains_word(text, word):
    """Match a complete normalized phrase rather than a substring."""
    normalized_text = normalize_complaint_text(text)
    normalized_word = normalize_complaint_text(word)
    if not normalized_word:
        return False
    phrase = r"\s+".join(re.escape(part) for part in normalized_word.split())
    return re.search(r"(?<!\w)" + phrase + r"(?!\w)", normalized_text) is not None


def matched_rule_terms(text, rules):
    """Return longest non-overlapping rule phrases so synonyms do not double-count."""
    candidates = []
    for phrase, weight in rules:
        normalized_phrase = normalize_complaint_text(phrase)
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in normalized_phrase.split()) + r"(?!\w)"
        match = re.search(pattern, text)
        if match:
            candidates.append((match.start(), match.end(), phrase, weight))
    candidates.sort(key=lambda match: (-(match[1] - match[0]), -match[3], match[0]))
    selected = []
    for candidate in candidates:
        start, end, _, _ = candidate
        if not any(start < existing_end and end > existing_start for existing_start, existing_end, _, _ in selected):
            selected.append(candidate)
    return [(phrase, weight) for _, _, phrase, weight in sorted(selected)]


def analyze_complaint(text):
    """Return a deterministic primary issue and every strongly supported route."""
    normalized = normalize_complaint_text(text)
    category_order = {category: index for index, category in enumerate(CLASSIFICATION_RULES)}
    matches_by_category = {
        category: matched_rule_terms(normalized, rules)
        for category, rules in CLASSIFICATION_RULES.items()
    }
    scores = {category: sum(weight for _, weight in matches)
              for category, matches in matches_by_category.items()}
    detected_categories = [
        category for category, score in scores.items()
        if score >= CLASSIFICATION_THRESHOLD
    ]
    detected_categories.sort(key=lambda category: (-scores[category], category_order[category]))

    # Keep a stable primary category for the existing complaint schema, while
    # preserving every other strong issue for linked-department review.
    category = detected_categories[0] if detected_categories else "Other"
    primary_department = CATEGORY_DEPARTMENTS[category]
    departments = []
    for detected_category in detected_categories or [category]:
        department = CATEGORY_DEPARTMENTS[detected_category]
        if department not in departments:
            departments.append(department)
    secondary_categories = detected_categories[1:]
    secondary_departments = [name for name in departments if name != primary_department]

    matched_categories = detected_categories
    keyword_categories = matched_categories or [
        name for name, matches in matches_by_category.items() if matches
    ]
    keywords = list(dict.fromkeys(
        phrase for name in keyword_categories for phrase, _ in matches_by_category[name]
    ))
    needs_review = len(detected_categories) != 1
    if not detected_categories:
        reason = "No specific issue rule matched; routed to the Public Health review queue for human confirmation."
    elif needs_review:
        reason = (
            "Multiple supported issue types were detected. Routed primarily to "
            f"{primary_department}; related departments {', '.join(secondary_departments) or primary_department} "
            "are linked for human review."
        )
    else:
        reason = (
            f"The description matched {', '.join(keywords)}; "
            f"{category} reports go to {primary_department}."
        )

    emergency = any(contains_word(normalized, word) for word in HIGH_PRIORITY_WORDS)
    if emergency:
        priority = "High"
    elif any(contains_word(normalized, word) for word in MEDIUM_PRIORITY_WORDS):
        priority = "Medium"
    else:
        priority = "Low"
    return {
        "keywords": keywords,
        "category": category,
        "categories": detected_categories,
        "secondary_categories": secondary_categories,
        "priority": priority,
        "department": primary_department,
        "departments": departments,
        "secondary_departments": secondary_departments,
        "reason": reason,
        "needs_review": needs_review,
        "emergency": emergency,
    }
