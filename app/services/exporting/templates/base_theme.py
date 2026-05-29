from datetime import datetime


THEME_TOKENS = {
    "brand_primary": "#0F172A",
    "brand_secondary": "#2563EB",
    "surface": "#F8FAFC",
    "text": "#1E293B",
    "positive": "#16A34A",
    "neutral": "#334155",
    "negative": "#DC2626",
}


def format_period(period_from: datetime | None, period_to: datetime | None) -> str:
    from_label = period_from.strftime("%Y-%m-%d") if isinstance(period_from, datetime) else "-"
    to_label = period_to.strftime("%Y-%m-%d") if isinstance(period_to, datetime) else "-"
    return f"{from_label} ate {to_label}"


def to_percent(value: float) -> str:
    return f"{(float(value or 0.0) * 100):.2f}%"

