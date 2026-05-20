# Base Factual do SentimentoIA

## Produto
SentimentoIA e uma plataforma SaaS para monitorar reputacao de marca e analisar sentimento de mencoes.

## Fontes de dados
- Reddit
- YouTube
- App Store
- Google Play
- Glassdoor
- Trustpilot
- Reclame Aqui
- Web aberta

## Fluxo de analise
1. Usuario executa busca por marca/tema.
2. Sistema coleta e normaliza mencoes.
3. Sistema calcula sinais de sentimento, criticidade e urgencia.
4. Dashboard consolida metricas e distribuicoes.
5. Insights priorizam riscos, oportunidades e acoes recomendadas.
6. Chat responde apenas com contexto autorizado do proprio usuario.

## Metricas principais
- total_comments: volume total analisado.
- sentiment_score: indicador agregado de sentimento.
- sentiment_distribution: positivo, negativo e neutro.
- criticality: nivel de criticidade (low, medium, high).
- top_themes: temas recorrentes.
- alerts: eventos relevantes para acompanhamento.

## Estrutura de insight
- executive_summary
- sentiment_overview
- priority (high|medium|low)
- resolution (pending|in_progress|resolved)
- risks
- opportunities
- recommended_actions
- decision_guidance

## Politica de isolamento por usuario
- Todo dado de chat, busca, insight e relatorio e isolado por usuario autenticado.
- O assistente so pode responder com dados do contexto autorizado do usuario atual.
- Dados de outros usuarios nunca devem ser acessados, inferidos ou exibidos.
