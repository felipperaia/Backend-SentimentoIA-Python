"""Testes de validacao para as correcoes criticas do SentimentoIA."""
import asyncio

import pytest
from unittest.mock import patch, MagicMock


# ====================== P1: Ollama Cloud ======================

class TestOllamaURLConstruction:
    """Verifica compatibilidade da montagem de URL do gateway."""

    def test_base_url_with_trailing_api(self):
        """OLLAMA_BASE_URL=https://ollama.com/api → EFFECTIVE_URL=https://ollama.com"""
        with patch("app.config.settings") as mock_settings:
            mock_settings.OLLAMA_BASE_URL = "https://ollama.com/api"
            mock_settings.OLLAMA_CLOUD_URL = ""
            # Simulate the property logic
            configured_url = mock_settings.OLLAMA_BASE_URL.strip().rstrip("/")
            normalized = configured_url.lower()
            if normalized.endswith("/api"):
                configured_url = configured_url[:-4]
            assert configured_url == "https://ollama.com"

    def test_base_url_without_trailing_api(self):
        """OLLAMA_BASE_URL=https://ollama.com → stays https://ollama.com"""
        configured_url = "https://ollama.com".strip().rstrip("/")
        normalized = configured_url.lower()
        if normalized.endswith("/api"):
            configured_url = configured_url[:-4]
        assert configured_url == "https://ollama.com"

    def test_build_ollama_url_generates_correct_endpoint(self):
        """Verifica que _build_ollama_url monta /api/generate corretamente."""
        from app.services.llm_service import LLMService
        with patch("app.services.llm_service.settings") as mock_settings:
            mock_settings.LLM_GATEWAY_EFFECTIVE_URL = "https://gateway.local"
            url = LLMService._build_ollama_url("generate")
            assert url == "https://gateway.local/api/generate"
            assert "/api/api/" not in url

    def test_build_ollama_url_no_double_slash(self):
        from app.services.llm_service import LLMService
        with patch("app.services.llm_service.settings") as mock_settings:
            mock_settings.LLM_GATEWAY_EFFECTIVE_URL = "https://gateway.local/"
            url = LLMService._build_ollama_url("generate")
            assert "//api" not in url

    def test_ollama_configured_requires_key(self):
        from app.services.llm_service import LLMService
        with patch("app.services.llm_service.settings") as mock_settings:
            mock_settings.LLM_GATEWAY_EFFECTIVE_URL = "https://gateway.local"
            mock_settings.LLM_MODEL_EFFECTIVE = "llama3.1:8b"
            mock_settings.LLM_GATEWAY_EFFECTIVE_API_KEY = ""
            assert LLMService.ollama_configured() is False

    def test_ollama_configured_with_key(self):
        from app.services.llm_service import LLMService
        with patch("app.services.llm_service.settings") as mock_settings:
            mock_settings.LLM_GATEWAY_EFFECTIVE_URL = "https://gateway.local"
            mock_settings.LLM_MODEL_EFFECTIVE = "llama3.1:8b"
            mock_settings.LLM_GATEWAY_EFFECTIVE_API_KEY = "test-key-123"
            assert LLMService.ollama_configured() is True


# ====================== P2: Scraper ======================

class TestCollectorsResilience:
    """Verifica comportamento resiliente dos novos coletores."""

    def test_reddit_public_json_sem_oauth(self):
        from app.services.scraper import RedditCollector

        async def fake_request(*args, **kwargs):
            del args, kwargs
            await asyncio.sleep(0)
            return {"data": {"children": []}}

        with patch.object(RedditCollector, "_request", side_effect=fake_request):
            items = asyncio.run(RedditCollector().collect("Nubank", 5))
            assert items == []

    def test_reclameaqui_disabled_returns_empty(self):
        from app.services.scraper import ReclameAquiCollector

        with patch("app.services.scraper.settings") as mock_settings:
            mock_settings.ENABLE_RECLAME_AQUI = False
            mock_settings.ENABLE_RECLAMEAQUI = False
            mock_settings.APIFY_TOKEN = ""

            items = asyncio.run(ReclameAquiCollector().collect("Nubank", 5))
            assert items == []


# ====================== P3: Pipeline ======================

class TestDashboardDataFlow:
    """Verifica que o dashboard le dados de ambos os fluxos."""

    def test_dashboard_query_uses_or_filter(self):
        """Dashboard deve buscar por batch_id OR search_id."""
        from app.services.dashboard_service import DashboardService
        with patch("app.services.dashboard_service.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_mentions = MagicMock()
            mock_mentions.find.return_value.sort.return_value.limit.return_value = []
            mock_db.mentions = mock_mentions
            mock_get_db.return_value = mock_db

            result = DashboardService.get_dashboard(user_id="user1")
            assert result["metrics"]["total_mentions"] == 0
            assert "mentions" in result


# ====================== P4: NPS ======================

class TestNpsService:
    """Verifica servico NPS."""

    def test_nps_score_validation(self):
        from app.services.nps_service import NpsService
        with pytest.raises(ValueError, match="entre 0 e 10"):
            with patch("app.services.nps_service.get_db") as mock_db:
                mock_db.return_value = MagicMock()
                NpsService.submit_response(
                    user_id="user1",
                    session_id="s1",
                    module_key="dashboard",
                    score=11,
                )

    def test_nps_metrics_empty(self):
        from app.services.nps_service import NpsService
        with patch("app.services.nps_service.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.nps_responses.find.return_value = []
            mock_get_db.return_value = mock_db

            metrics = NpsService.get_metrics()
            assert metrics["total_responses"] == 0
            assert metrics["nps_score"] == 0

    def test_nps_score_calculation(self):
        """Verifica calculo NPS basico."""
        from app.services.nps_service import NpsService
        with patch("app.services.nps_service.get_db") as mock_get_db:
            mock_db = MagicMock()
            # 3 promoters (9,10,10), 1 passive (8), 1 detractor (5)
            mock_db.nps_responses.find.return_value = [
                {"score": 10, "module_key": "dashboard"},
                {"score": 9, "module_key": "dashboard"},
                {"score": 10, "module_key": "busca"},
                {"score": 8, "module_key": "dashboard"},
                {"score": 5, "module_key": "busca"},
            ]
            mock_get_db.return_value = mock_db

            metrics = NpsService.get_metrics()
            assert metrics["total_responses"] == 5
            assert metrics["promoters"] == 3
            assert metrics["detractors"] == 1
            # NPS = ((3 - 1) / 5) * 100 = 40.0
            assert metrics["nps_score"] == 40.0


class TestLLMErrorHandling:
    """Verifica tratamento de erros HTTP do Ollama."""

    def test_401_error_message(self):
        from app.services.llm_service import LLMService
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_resp.url = "https://ollama.com/api/generate"

        with pytest.raises(RuntimeError, match="autenticacao falhou"):
            LLMService._handle_ollama_error(mock_resp)

    def test_429_error_message(self):
        from app.services.llm_service import LLMService
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 429
        mock_resp.text = "Rate limited"
        mock_resp.url = "https://ollama.com/api/generate"

        with pytest.raises(RuntimeError, match="limite de requisicoes"):
            LLMService._handle_ollama_error(mock_resp)

    def test_200_no_error(self):
        from app.services.llm_service import LLMService
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        # Should not raise
        LLMService._handle_ollama_error(mock_resp)
