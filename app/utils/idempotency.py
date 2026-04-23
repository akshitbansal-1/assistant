import hashlib
import re


ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def fingerprint_for_text(*parts: str) -> str:
    normalized = " ".join(part.strip().lower() for part in parts if part)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def extract_issue_keys(text: str) -> list[str]:
    return sorted(set(match.group(1) for match in ISSUE_KEY_PATTERN.finditer(text or "")))
