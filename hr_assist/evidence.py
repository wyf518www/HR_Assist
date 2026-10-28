"""Source-backed quote alignment. Never fuzzy-match or repair substantive text."""
import re


def compact_layout(text):
    chars, positions = [], []
    for match in re.finditer(r'\S|\s+', text):
        value=match.group()
        if value.isspace():
            left=text[match.start()-1:match.start()]
            right=text[match.end():match.end()+1]
            # An ASCII word boundary can change meaning ("not able" !=
            # "notable"). Keep it; only normalize its whitespace width.
            if left and right and left.isascii() and right.isascii() and left.isalnum() and right.isalnum():
                chars.append(' ')
                positions.append(match.start())
            continue
        chars.append(value)
        positions.append(match.start())
    return ''.join(chars), positions


def locate_quote(text, quote):
    """Return the actual contiguous source slice, tolerating only layout whitespace.

    Keep punctuation, digits, word order and negations exact. A normalized match
    must be unique. Persist the source slice rather than the model's rendering.
    """
    if not quote.strip():
        return None
    if quote in text:
        return quote
    compact,positions = compact_layout(text)
    needle,_ = compact_layout(quote)
    start = compact.find(needle)
    if not needle or start < 0 or compact.find(needle, start + 1) >= 0:
        return None
    return text[positions[start]:positions[start + len(needle) - 1] + 1]
