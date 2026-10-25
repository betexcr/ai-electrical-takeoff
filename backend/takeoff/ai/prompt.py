"""Versioned, stable prompts for the AI stages (spec §5.9).

The text is built once from the taxonomy at import time and contains no timestamps or random ids, so the same
inputs always give byte-identical requests. Any change to the wording, or to the taxonomy it is built from,
needs a new prompt version and a re-run of ``tools/eval_ai_verifier.py``.
"""

from __future__ import annotations

from takeoff.groundtruth import taxonomy as T

VERIFY_PROMPT_VERSION = "ai-verify-prompt/1"
NOT_A_DEVICE = "not_a_device"
RATIONALE_MAX_CHARS = 300


def _taxonomy_lines() -> list[str]:
    lines = []
    for category, subs in T.CATEGORIES.items():
        if not subs:
            lines.append(f"- {category}: an electrical device drawn on the plan that has no legend entry; subtype null")
            continue
        lines.append(f"- {category}:")
        for s in subs:
            lines.append(f"    - {s.name}: {T.label(category, s.name)}")
    return lines


def _token_lines() -> list[str]:
    out = []
    for token, rule in T.TOKEN_RULES.items():
        meaning = "; ".join(f"{c} {' or '.join(subs)}" for c, subs in rule.items())
        out.append(f"- {token}: {meaning}")
    return out


VERIFY_SYSTEM = "\n".join([
    "You check an automatic detector's classification of symbols on residential electrical floor plans.",
    "",
    "For each item you get:",
    "- a 48 x 48 pt crop of the drawing at 300 DPI (about 200 x 200 px), centred on the point the detector found;",
    "- the detector's category and subtype for that point;",
    "- the text printed within 15 pt of the point, if any.",
    "Before the items you get reference icons cut from this sheet's own legend, each with its legend wording.",
    "",
    "Judge only the symbol at the centre of the crop. Other symbols, wires and text near the edges belong to other",
    "items. Compare the central symbol with the reference icons. Small text next to a symbol often fixes the",
    "subtype: switch letters, receptacle tags, SD or SD/MA, IAQ.",
    "",
    "Categories and subtypes (subtype name: legend wording):",
    *_taxonomy_lines(),
    "",
    "Text tags and the subtypes they usually mean:",
    *_token_lines(),
    "",
    "For every item answer with:",
    "- item_id: the item's id, copied exactly;",
    "- is_device: false when no electrical device symbol is at the centre (a wire, text, a door swing, an empty area);",
    f"- category: one of the categories above, or \"{NOT_A_DEVICE}\" when is_device is false;",
    "- subtype: a subtype name of that category from the list above, or null when you cannot tell or there is none;",
    "- uncertain: true when the crop does not let you decide with confidence;",
    f"- rationale: at most {RATIONALE_MAX_CHARS} characters on the visual evidence you used.",
    "",
    "Return exactly one verdict per item id given, each id exactly once, and no other ids. Do not agree with the",
    "detector by default: answer what the drawing shows.",
])
