"""
Reverse AFL strategy signals: swap Buy <-> Short and Sell <-> Cover.

Only swaps the core AmiBroker signal assignments, NOT intermediate
variable names (rawBuy, rawShort, buySignal, etc.) -- those are just
names whose meaning flows from their assignment context.  Swapping
both intermediates AND finals would cancel out.
"""

import re


def _placeholder_swap(text: str, token_a: str, token_b: str) -> str:
    """Swap two tokens using placeholders to avoid collisions."""
    placeholder = f"__SWAP_{token_a}_{token_b}__"
    text = re.sub(rf'\b{token_a}\b', placeholder, text)
    text = re.sub(rf'\b{token_b}\b', token_a, text)
    text = text.replace(placeholder, token_b)
    return text


def reverse_afl(afl_content: str) -> str:
    """Return AFL with Buy/Short and Sell/Cover signals swapped.

    Handles:
    - Compound tokens first (BuyPrice/ShortPrice, SellPrice/CoverPrice)
    - Core signals (Buy/Short, Sell/Cover)
    - PlotShapes visualization (arrows, colors, Low/High)
    - Title annotation with [Reversed]
    """
    if not afl_content:
        return afl_content

    result = afl_content

    # --- Phase 1: Compound tokens (must come before simple tokens) ---
    result = _placeholder_swap(result, "BuyPrice", "ShortPrice")
    result = _placeholder_swap(result, "SellPrice", "CoverPrice")

    # --- Phase 2: Core signals ---
    result = _placeholder_swap(result, "Buy", "Short")
    result = _placeholder_swap(result, "Sell", "Cover")

    # --- Phase 3: PlotShapes visualization (line-scoped) ---
    out_lines = []
    for line in result.splitlines():
        if "PlotShapes" in line:
            line = _placeholder_swap(line, "shapeUpArrow", "shapeDownArrow")
            line = _placeholder_swap(line, "colorGreen", "colorRed")
            line = _placeholder_swap(line, "Low", "High")
        out_lines.append(line)
    result = "\n".join(out_lines)

    # --- Phase 4: Annotate the Title ---
    # Title may span multiple lines (continued with +). Find the complete
    # Title statement and insert [Reversed] before the final semicolon.
    if "[Reversed]" not in result:
        # Match from 'Title =' through the terminating ';'
        title_match = re.search(r'^(Title\s*=.*?);', result, flags=re.MULTILINE | re.DOTALL)
        if title_match:
            original = title_match.group(0)
            # Insert before the final semicolon
            annotated = original[:-1] + ' + " [Reversed]";'
            result = result.replace(original, annotated, 1)

    return result
