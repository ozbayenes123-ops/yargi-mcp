# resmigazete_mcp_module/models.py

from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl


class FihristItem(BaseModel):
    """A single article/publication entry inside a gazette fihrist section."""

    title: str = Field("", description="Title of the article.")
    url: Optional[str] = Field(None, description="URL of the article page (.htm).")
    pdf_url: Optional[str] = Field(None, description="URL of the PDF version, when available.")
    is_pdf: bool = Field(False, description="True when only a PDF is available for this item.")


class FihristSection(BaseModel):
    """A section (Bölüm) of a gazette fihrist."""

    name: str = Field("", description="Section heading, e.g. YÜRÜTME VE İDARE BÖLÜMÜ.")
    items: List[FihristItem] = Field(default_factory=list)


class FihristResult(BaseModel):
    """Fihrist (table of contents) of a Resmî Gazete issue for a given date."""

    date: str = Field("", description="Issue date in YYYY-MM-DD format.")
    source_url: Optional[str] = Field(None, description="URL of the fihrist page.")
    sections: List[FihristSection] = Field(default_factory=list)
    mukerrer: List["FihristResult"] = Field(default_factory=list, description="Mükerrer (extra) issues of the same date.")
    error_message: Optional[str] = Field(None, description="Error message, if retrieval failed.")


class GazetteArticleMarkdown(BaseModel):
    """A Resmî Gazete article converted to Markdown."""

    url: Optional[str] = Field(None, description="Source URL of the article.")
    title: str = Field("", description="Article title.")
    markdown_content: Optional[str] = Field(None, description="Article text in Markdown.")
    error_message: Optional[str] = Field(None, description="Error message, if retrieval failed.")


class GazetteSearchMatch(BaseModel):
    """A keyword match found in gazette fihrist titles."""

    date: str = Field("", description="Issue date in YYYY-MM-DD format.")
    section: str = Field("", description="Section heading where the match was found.")
    title: str = Field("", description="Matching article title.")
    url: Optional[str] = Field(None, description="URL of the article page.")
    pdf_url: Optional[str] = Field(None, description="URL of the PDF version, when available.")
    snippet: str = Field("", description="Context snippet around the match.")


class GazetteSearchResult(BaseModel):
    """Response model for keyword search across gazette fihrist titles."""

    matches: List[GazetteSearchMatch] = Field(default_factory=list)
    scanned_days: int = Field(0, description="Number of issue days scanned.")
    truncated: bool = Field(False, description="True when results were truncated at max_results.")
    error_message: Optional[str] = Field(None, description="Error message, if search failed.")
