import re


_PROTECTED_TERM_CASING = {
    "comvida": "ComVida",
    "dna": "DNA",
    "facebook": "Facebook",
    "hr": "HR",
    "ir": "IR",
    "wish": "WISH",
}
_TECHNICAL_TOKEN = re.compile(
    r"https?://\S+|www\.\S+|[A-Za-z]:[\\/]\S+|(?<!\w)/(?:[^\s/]+/)+[^\s/]+"
)
_SLASH_SEPARATOR = re.compile(r"(?<=\w)\s*/\s*(?=\w)")
_WORD = re.compile(r"\b[\w'-]+\b")


def display_task_text(value):
    """Return consistent task copy without changing stored configuration or snapshots."""
    if not value:
        return value

    original = " ".join(str(value).split())
    acronyms = {
        match.group(0).casefold(): match.group(0)
        for match in _WORD.finditer(original)
        if len(match.group(0)) > 1 and match.group(0).isupper()
    }
    protected = {**_PROTECTED_TERM_CASING, **acronyms}

    parts = []
    cursor = 0
    for match in _TECHNICAL_TOKEN.finditer(original):
        parts.append(_normalize_words(original[cursor : match.start()], protected))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(_normalize_words(original[cursor:], protected))
    normalized = "".join(parts).strip()

    for index, character in enumerate(normalized):
        if character.isalpha():
            return normalized[:index] + character.upper() + normalized[index + 1 :]
    return normalized


def _normalize_words(value, protected):
    value = _SLASH_SEPARATOR.sub(" / ", value.lower())

    def restore(match):
        return protected.get(match.group(0).casefold(), match.group(0))

    return re.sub(r"\bn\s*/\s*a\b", "N/A", _WORD.sub(restore, value))
