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

COMPANY_ARTICLE_TOKENS = {
    "o",
    "a",
    "os",
    "as",
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


def _ascii_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    # Mantem palavras como McDonald's => mcdonalds, evitando tokenizar em "mcdonald" + "s".
    return re.sub(r"(?<=[a-z0-9])[`´'’](?=[a-z0-9])", "", ascii_text.lower())


def _ascii_tokens(value: str) -> list[str]:
    ascii_text = _ascii_text(value)
    return re.findall(r"[a-z0-9]+", ascii_text)


def _canonical_tokens(tokens: list[str]) -> list[str]:
    if not tokens:
        return []

    without_suffix = [token for token in tokens if token not in COMPANY_SUFFIX_TOKENS]
    without_connectors = [token for token in without_suffix if token not in COMPANY_CONNECTOR_TOKENS]
    preferred = without_connectors or without_suffix or tokens

    # Limita o slug canonico para manter consistencia nas variacoes de nome.
    return preferred[:3]


def normalize_company_slug(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    slug = re.sub(r"[^a-z0-9]+", "-", _ascii_text(text)).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80]


def slugify_company(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""

    tokens = _ascii_tokens(text)
    canonical = _canonical_tokens(tokens)
    slug = "-".join(canonical).strip("-")

    if not slug:
        slug = normalize_company_slug(text)

    return slug[:80]


def build_company_slug_candidates(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    tokens = _ascii_tokens(text)
    without_suffix = [token for token in tokens if token not in COMPANY_SUFFIX_TOKENS]
    without_connectors = [token for token in without_suffix if token not in COMPANY_CONNECTOR_TOKENS]
    without_articles = [token for token in without_suffix if token not in COMPANY_ARTICLE_TOKENS]
    compact_without_noise = [
        token
        for token in without_connectors
        if token not in COMPANY_ARTICLE_TOKENS
    ]

    candidates: list[str] = []

    def add(candidate: str) -> None:
        normalized = normalize_company_slug(candidate)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    sequences = [
        tokens,
        without_suffix,
        without_articles,
        without_connectors,
        compact_without_noise,
    ]

    add(text)
    add(slugify_company(text))

    for sequence in sequences:
        if not sequence:
            continue
        add("-".join(sequence))
        if len(sequence) >= 1:
            add(sequence[0])
        if len(sequence) >= 2:
            add("-".join(sequence[:2]))
        if len(sequence) >= 3:
            add("-".join(sequence[:3]))

    if len(without_articles) == 1:
        base = without_articles[0]
        for article in sorted(COMPANY_ARTICLE_TOKENS):
            add(f"{article}-{base}")

    return candidates


def normalize_company_filter(company_id: str | None = None, company_slug: str | None = None) -> str | None:
    if company_slug:
        slug = normalize_company_slug(company_slug)
        if slug:
            return slug
    if company_id:
        slug = slugify_company(company_id)
        if slug:
            return slug
    return None
