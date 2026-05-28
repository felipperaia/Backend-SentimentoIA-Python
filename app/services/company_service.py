from __future__ import annotations

from typing import Any

from app.database import get_db
from app.services.company_utils import normalize_company_filter, slugify_company


class CompanyService:
    @staticmethod
    def _extract_company_from_job(job: dict[str, Any]) -> tuple[str, str] | None:
        company_name = (
            str(job.get("company_name") or "").strip()
            or str(job.get("query") or "").strip()
            or str(job.get("brand") or "").strip()
            or str(job.get("entity") or "").strip()
        )
        if not company_name:
            return None

        company_slug = normalize_company_filter(
            company_slug=str(job.get("company_slug") or "").strip() or None,
            company_id=company_name,
        )
        if not company_slug:
            company_slug = slugify_company(company_name)
        if not company_slug:
            return None

        return company_slug, company_name

    @staticmethod
    def _collect_from_primary(db: Any, user_id: str) -> list[dict[str, str]]:
        companies: dict[str, dict[str, str]] = {}

        for collection_name in ["search_jobs", "searchjobs"]:
            if collection_name not in db.list_collection_names():
                continue

            cursor = db[collection_name].find(
                {"user_id": user_id},
                {
                    "company_slug": 1,
                    "company_name": 1,
                    "query": 1,
                    "brand": 1,
                    "entity": 1,
                },
            )
            for job in cursor:
                parsed = CompanyService._extract_company_from_job(job)
                if not parsed:
                    continue
                slug, name = parsed
                if slug not in companies:
                    companies[slug] = {
                        "company_id": slug,
                        "name": name,
                        "slug": slug,
                    }

        # Compatibilidade com lotes de ingestao que ainda nao viraram search_jobs.
        if "comment_batches" in db.list_collection_names():
            cursor = db.comment_batches.find({"user_id": user_id}, {"brand": 1, "company_name": 1, "company_slug": 1})
            for batch in cursor:
                candidate_name = str(batch.get("company_name") or batch.get("brand") or "").strip()
                if not candidate_name:
                    continue
                slug = normalize_company_filter(
                    company_slug=str(batch.get("company_slug") or "").strip() or None,
                    company_id=candidate_name,
                )
                if not slug:
                    slug = slugify_company(candidate_name)
                if not slug or slug in companies:
                    continue
                companies[slug] = {
                    "company_id": slug,
                    "name": candidate_name,
                    "slug": slug,
                }

        return list(companies.values())

    @staticmethod
    def list_companies_for_user(user_id: str) -> list[dict[str, str]]:
        db = get_db()
        if db is None:
            raise RuntimeError("Banco de dados indisponivel")

        items = CompanyService._collect_from_primary(db=db, user_id=user_id)
        return sorted(items, key=lambda item: str(item.get("name") or "").lower())
