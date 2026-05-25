import re
import unicodedata


def slugify_company(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""

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
