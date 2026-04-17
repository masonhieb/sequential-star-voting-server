"""
Test data generation for the STAR voting server.
Produces randomised candidate names for quick ballot setup during testing.
"""

import random

FIRST_NAMES = [
    "Alice",
    "Aaron",
    "Beth",
    "Bob",
    "Carl",
    "Carol",
    "Dana",
    "Dave",
    "Diana",
    "Eve",
    "Frank",
    "Grace",
    "Hank",
    "Iris",
    "Jack",
    "Karen",
    "Leo",
    "Mia",
    "Nate",
    "Olive",
    "Pete",
    "Quinn",
    "Rose",
    "Sam",
    "Tara",
    "Uma",
    "Vince",
    "Wendy",
    "Xander",
    "Yara",
]

LAST_NAMES = [
    "Adams",
    "Abbott",
    "Baker",
    "Burns",
    "Clark",
    "Cross",
    "Davis",
    "Dixon",
    "Evans",
    "Foster",
    "Grant",
    "Hall",
    "Ingram",
    "James",
    "King",
    "Lane",
    "Moore",
    "Nash",
    "Owen",
    "Park",
    "Reed",
    "Stone",
    "Turner",
    "Underwood",
    "Vance",
    "Walsh",
    "Young",
    "Zhang",
    "Quinn",
    "Brooks",
    "Ryan",
]


def generate_test_candidates(n: int = 10) -> list[dict]:
    """Return n candidate dicts with unique random first+last name titles."""
    seen: set[str] = set()
    candidates: list[dict] = []
    attempts = 0
    while len(candidates) < n and attempts < n * 20:
        attempts += 1
        title = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        if title in seen:
            continue
        seen.add(title)
        candidates.append(
            {"title": title, "body": "", "author": None, "image_path": None}
        )
    return candidates
