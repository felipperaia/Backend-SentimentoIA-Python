# backend-api-python

## Objetivo

Repositorio dedicado ao backend FastAPI e ao worker Python, preservando acoplamentos existentes sem refactor.

## Conteudo copiado

- apps/api/app
- apps/api/tests
- apps/api/requirements.txt
- apps/api/.env.example
- apps/worker/app
- apps/worker/tests
- apps/worker/.env.example
- packages/prompts
- .gitignore

## Nao incluido

- apps/api/.env
- apps/worker/.env
- venv/.venv/venv311
- __pycache__ e .pytest_cache
- node_modules, dist, logs

## Observacoes tecnicas

- O worker depende do caminho apps/api dentro do mesmo repositorio.
- O chat do backend busca prompts em packages/prompts por caminho relativo.
