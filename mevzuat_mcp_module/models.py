# mevzuat_mcp_module/models.py

from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl


class MevzuatSearchRequest(BaseModel):
    """Request model for searching Turkish legislation (Mevzuat) via the Bedesten API."""

    mevzuatAdi: str = Field("", description="Keywords to search in legislation title.")
    mevzuatNo: str = Field("", description="Legislation number, e.g. '5237' for the Turkish Penal Code.")
    mevzuatTurList: List[str] = Field(default_factory=list, description="Legislation types, e.g. ['KANUN'], ['YONETMELIK'].")
    resmiGazeteTarihiStart: str = Field("", description="Start date YYYY-MM-DD of publication in the Official Gazette.")
    resmiGazeteTarihiEnd: str = Field("", description="End date YYYY-MM-DD of publication in the Official Gazette.")
    page: int = Field(1, ge=1, description="Page number.")
    pageSize: int = Field(10, ge=1, le=50, description="Results per page.")


class MevzuatSummary(BaseModel):
    """Summary of a legislation record from search results."""

    mevzuat_id: str = Field("", description="Internal Bedesten document ID, used to fetch full content.")
    mevzuat_no: Optional[int] = Field(None, description="Legislation number (e.g. 5237).")
    mevzuat_adi: str = Field("", description="Legislation title.")
    mevzuat_tur: str = Field("", description="Legislation type (e.g. KANUN, YONETMELIK).")
    mevzuat_tertip: Optional[int] = Field(None, description="Tertip (collection) number.")
    resmi_gazete_tarihi: Optional[str] = Field(None, description="Publication date in the Official Gazette.")
    resmi_gazete_sayisi: Optional[str] = Field(None, description="Official Gazette issue number.")
    url: Optional[str] = Field(None, description="Canonical mevzuat.gov.tr URL of the record.")
    mukerrer: Optional[str] = Field(None, description="Whether the gazette issue was a repeat edition (HAYIR/EVET).")


class MevzuatSearchResult(BaseModel):
    """Response model for legislation search results."""

    results: List[MevzuatSummary] = Field(default_factory=list)
    total_results: int = Field(0, description="Total number of matching records.")
    page: int = Field(1, description="Current page.")
    pageSize: int = Field(10, description="Results per page.")


class MevzuatDocumentMarkdown(BaseModel):
    """Legislation full text converted to paginated Markdown."""

    mevzuat_id: str = Field("", description="Internal Bedesten document ID.")
    title: str = Field("", description="Legislation title.")
    source_url: Optional[str] = Field(None, description="Canonical mevzuat.gov.tr URL.")
    markdown_chunk: Optional[str] = Field(None, description="A chunk of the Markdown content.")
    current_page: int = Field(1, description="Current Markdown chunk page.")
    total_pages: int = Field(1, description="Total Markdown chunk pages.")
    is_paginated: bool = Field(False, description="True when content spans multiple chunks.")
    error_message: Optional[str] = Field(None, description="Error message, if retrieval failed.")


class MevzuatInTextMatch(BaseModel):
    """A match found inside legislation full text (for mevzuat_icinde_ara)."""

    mevzuat_id: str = Field("", description="Internal Bedesten document ID.")
    mevzuat_adi: str = Field("", description="Legislation title.")
    source_url: Optional[str] = Field(None, description="Canonical mevzuat.gov.tr URL.")
    match_count: int = Field(0, description="Number of occurrences of the search term.")
    snippets: List[str] = Field(default_factory=list, description="Context snippets around matches.")


class MevzuatMaddeTreeNode(BaseModel):
    """A node of the legislation article tree (chapters/articles hierarchy)."""

    madde_id: Optional[str] = Field(None, description="Internal article ID.")
    madde_no: Optional[str] = Field(None, description="Article number, e.g. 'MADDE 1'.")
    madde_baslik: str = Field("", description="Article/chapter heading.")
    children: List["MevzuatMaddeTreeNode"] = Field(default_factory=list, description="Child nodes.")


class MevzuatTypesResult(BaseModel):
    """Response model for legislation types enumeration."""

    types: List[dict] = Field(default_factory=list, description="Legislation types with document counts.")
