from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Movie:
    id: str
    source_url: str
    guide_rapide_id: int
    title: str
    year: Optional[int]
    director: list[str]
    actors: list[str]
    runtime: str
    genres: list[str]
    synopsis: str
    rating: str
    voters: Optional[int]
    poster: str
    trailer_url: str
    writers: list[str]
    production_companies: list[str]
    critic_ratings: dict[str, str]
    content_rating: str
    box_office: str
    awards: str
    metascore: str
    imdb_id: str
    production_countries: list[str]
    dvd_release_date: str
    bluray_release_date: str
    release_type: str
    release_text: str
    released: str
    physical_available: bool
    checked_at: str


@dataclass
class ImdbMetadata:
    title: str = ""
    year: Optional[int] = None
    director: list[str] | None = None
    actors: list[str] | None = None
    poster: str = ""
    description: str = ""
    genres: list[str] | None = None
    rating: str = ""
    voters: Optional[int] = None
    runtime: str = ""
    trailer_url: str = ""
    writers: list[str] | None = None
    production_companies: list[str] | None = None
    critic_ratings: dict[str, str] | None = None
    content_rating: str = ""
    box_office: str = ""
    awards: str = ""
    metascore: str = ""
