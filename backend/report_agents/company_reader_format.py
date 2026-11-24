"""Reader-facing citations; facts and structured evidence IDs stay intact."""
import re


def reader_text(text):
    # Only internal bracketed citation/placeholder tokens; retain real E- refs.
    return re.sub(r'\[(?:I-[0-9a-f]{8,64}|Evidence\s+Gap)\]', '', text, flags=re.I)
