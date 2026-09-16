"""Wire models. Every request body is validated here and nowhere else."""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from .. import config


class CrawlOptions(BaseModel):
    max_pages: int = Field(default=100, ge=1, le=config.MAX_CRAWL_PAGES)
    max_jobs: int = Field(default=3000, ge=1, le=config.MAX_CRAWL_JOBS)
    delay_seconds: float = Field(default=config.MIN_CRAWL_DELAY_SECONDS,
                                 ge=config.MIN_CRAWL_DELAY_SECONDS, le=60)
    include_details: bool = True


class Preferences(BaseModel):
    exclude_restricted_eligibility: bool = True
    # Longest work term the applicant will take. None follows the resume's own
    # stated availability; 0 turns the filter off. A posting is excluded only
    # when the *shortest* term it accepts exceeds this.
    max_term_months: Optional[int] = Field(default=None, ge=0, le=24)
    target_roles: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("target_roles", "locations", "exclude_keywords")
    @classmethod
    def clean_terms(cls, terms: list[str]) -> list[str]:
        if any(len(term) > 200 for term in terms):
            raise ValueError("Each preference must be at most 200 characters.")
        return [term.strip() for term in terms if term.strip()]


class RankOptions(BaseModel):
    limit: int = Field(default=100, ge=1, le=100)
    preferences: Preferences = Field(default_factory=Preferences)


class AIRankOptions(RankOptions):
    # 0 means "assess every eligible posting"; the default caps spend at the
    # postings retrieval considers most promising.
    shortlist: int = Field(default=config.DEFAULT_SHORTLIST, ge=0, le=config.MAX_CRAWL_JOBS)
    refine_pairs: int = Field(default=6, ge=0, le=20)
    concurrency: int = Field(default=config.DEFAULT_CONCURRENCY, ge=1, le=8)


class Job(BaseModel):
    id: str = Field(default="", max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    company: str = Field(default="", max_length=1000)
    location: str = Field(default="", max_length=1000)
    description: str = Field(default="", max_length=100000)
    requirements: str = Field(default="", max_length=50000)
    deadline: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=4000)
    status: str = Field(default="", max_length=200)
    is_open: Optional[bool] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def valid_job_url(cls, value: str) -> str:
        if value:
            parts = urlsplit(value)
            if (parts.scheme != "https" or parts.hostname != "waterlooworks.uwaterloo.ca"
                    or parts.username or parts.password or parts.port not in (None, 443)):
                raise ValueError("Job URL must be an HTTPS WaterlooWorks URL.")
        return value


class ImportJobs(BaseModel):
    jobs: list[Job] = Field(min_length=1, max_length=config.MAX_CRAWL_JOBS)
