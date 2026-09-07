# aihm_mcp_module/models.py

from typing import List, Optional

from pydantic import BaseModel, Field


class AihmSearchRequest(BaseModel):
    """Request model for searching European Court of Human Rights case law (HUDOC)."""

    keywords: str = Field("", description="Keywords searched in the full text (use English legal terms).")
    docname: str = Field("", description="Exact case name, e.g. 'A.G. v. SWITZERLAND'.")
    appno: str = Field("", description="Application number, e.g. '15345/20'.")
    ecli: str = Field("", description="ECLI identifier, e.g. 'ECLI:CE:ECHR:2026:0723JUD001534520'.")
    language: str = Field("ENG", description="Language ISO code, e.g. ENG, TUR, FRA.")
    collection: str = Field("", description="Document collection: GRANDCHAMBER, CHAMBER, COMMITTEE, ADMISSIBILITY or empty for all.")
    importance: int = Field(0, ge=0, le=4, description="Importance level (0=all, 1=Key case, 4=low).")
    date_from: str = Field("", description="Start date YYYY-MM-DD (judgment date).")
    date_to: str = Field("", description="End date YYYY-MM-DD (judgment date).")
    page: int = Field(1, ge=1, description="Page number.")
    pageSize: int = Field(10, ge=1, le=50, description="Results per page.")


class AihmDecisionSummary(BaseModel):
    """Summary of an ECHR case from search results."""

    itemid: str = Field("", description="HUDOC item ID, used to fetch the full document.")
    docname: str = Field("", description="Case name.")
    appno: Optional[str] = Field(None, description="Application number(s).")
    doctype: Optional[str] = Field(None, description="Document type, e.g. HEJUD.")
    typedescription: Optional[str] = Field(None, description="Human-readable document type.")
    originatingbody: Optional[str] = Field(None, description="Originating body / chamber.")
    kpdate: Optional[str] = Field(None, description="Judgment date (ISO).")
    languageisocode: Optional[str] = Field(None, description="Language of the document.")
    documentcollectionid2: Optional[str] = Field(None, description="Collection, e.g. GRANDCHAMBER.")
    importance: Optional[str] = Field(None, description="Importance level.")
    conclusion: Optional[str] = Field(None, description="Conclusion / violations found.")
    ecli: Optional[str] = Field(None, description="ECLI identifier.")
    respondent: Optional[str] = Field(None, description="Respondent country code, e.g. TUR.")
    is_placeholder: bool = Field(False, description="True when the record is a placeholder without full text.")


class AihmSearchResult(BaseModel):
    """Response model for ECHR case search results."""

    results: List[AihmDecisionSummary] = Field(default_factory=list)
    total_results: int = Field(0, description="Total number of matching cases.")
    page: int = Field(1, description="Current page.")
    pageSize: int = Field(10, description="Results per page.")


class AihmDocumentMarkdown(BaseModel):
    """ECHR case full text converted to paginated Markdown."""

    itemid: str = Field("", description="HUDOC item ID.")
    docname: str = Field("", description="Case name.")
    markdown_chunk: Optional[str] = Field(None, description="A chunk of the Markdown content.")
    current_page: int = Field(1, description="Current Markdown chunk page.")
    total_pages: int = Field(1, description="Total Markdown chunk pages.")
    is_paginated: bool = Field(False, description="True when content spans multiple chunks.")
    error_message: Optional[str] = Field(None, description="Error message, if retrieval failed.")
