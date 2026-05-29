import re
import unicodedata


COMPANY_SUFFIX_TOKENS = {
    "inc",
    "corp",
    "corporation",
    "company",
    "co",
    "ltda",
    "limitada",
    "s",
    "a",
    "sa",
    "srl",
    "llc",
    "plc",
    "technologies",
    "technology",
    "tecnologies",
    "tecnologia",
    "tecnologias",
}

COMPANY_CONNECTOR_TOKENS = {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "the",
    "and",
    "e",
}


def _ascii_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.findall(r"[a-z0-9]+", ascii_text)


def _canonical_tokens(tokens: list[str]) -> list[str]:
    if not tokens:
        return []

    without_suffix = [token for token in tokens if token not in COMPANY_SUFFIX_TOKENS]
    without_connectors = [token for token in without_suffix if token not in COMPANY_CONNECTOR_TOKENS]
    preferred = without_connectors or without_suffix or tokens

    # Limita o slug canonico para manter consistencia nas variacoes de nome.
    return preferred[:3]


def slugify_company(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""

    tokens = _ascii_tokens(text)
    canonical = _canonical_tokens(tokens)
    slug = "-".join(canonical).strip("-")

    if not slug:
        normalized = unicodedata.normalize("NFKD", text)
        ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")

    return slug[:80]


def normalize_company_filter(company_id: str | None = None, company_slug: str | None = None) -> str | None:
    if company_slug:
        slug = slugify_company(company_slug)
        if slug:
            return slug
    if company_id:
        slug = slugify_company(company_id)
        if slug:
            return slug
    return None
