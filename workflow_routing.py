"""Canonical complaint-category to department routing used by the application."""

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
