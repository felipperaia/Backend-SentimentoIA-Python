from datetime import datetime

from pydantic import BaseModel, Field


class PrivacyConsentPreferences(BaseModel):
    cookies_analiticos: bool | None = None
    cookies_personalizacao: bool | None = None
    cookies_treinamento_ia: bool | None = None

    # Campos legados aceitos para compatibilidade.
    analytics: bool | None = None
    marketing: bool | None = None


class PrivacyConsentUpsertRequest(BaseModel):
    consent: bool | None = None
    preferences: PrivacyConsentPreferences | None = None
    version: str | None = None

    # Campos legados aceitos para compatibilidade.
    session_id: str | None = None
    analytics: bool | None = None
    marketing: bool | None = None

    def normalized_preferences(self) -> dict[str, bool]:
        consent_default = bool(self.consent) if self.consent is not None else None
        nested = self.preferences or PrivacyConsentPreferences()

        analytics_value = self.analytics
        if analytics_value is None:
            analytics_value = nested.analytics
        if analytics_value is None:
            analytics_value = nested.cookies_analiticos
        if analytics_value is None:
            analytics_value = consent_default

        personalization_value = self.marketing
        if personalization_value is None:
            personalization_value = nested.marketing
        if personalization_value is None:
            personalization_value = nested.cookies_personalizacao
        if personalization_value is None:
            personalization_value = consent_default

        training_value = nested.cookies_treinamento_ia
        if training_value is None:
            training_value = consent_default if consent_default is not None else False

        return {
            "cookies_analiticos": bool(analytics_value),
            "cookies_personalizacao": bool(personalization_value),
            "cookies_treinamento_ia": bool(training_value),
        }

    def resolved_consent(self) -> bool:
        if self.consent is not None:
            return bool(self.consent)
        normalized = self.normalized_preferences()
        return any(normalized.values())


class PrivacyConsentResponse(BaseModel):
    consent: bool
    preferences: dict[str, bool] = Field(default_factory=dict)
    version: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None
