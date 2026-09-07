# tbb_mcp_module/models.py

from pydantic import BaseModel, Field
from typing import List, Optional

class TbbSearchRequest(BaseModel):
    """Model for Türkiye Barolar Birliği (TBB) Disiplin Kurulu karar arama isteği."""
    keywords: Optional[str] = Field("", description="""
        Karar gerekçesinde / konusunda aranacak Türkçe kelimeler (öbek eşleşmesi).
        Örnekler: "meslekten çıkarma", "haksız rekabet", "2023/919"
    """)
    yil: Optional[int] = Field(None, ge=2005, le=2030, description="""
        Karar yılı filtresi (2005-güncel). Örnek: 2023.
    """)
    page: int = Field(1, ge=1, le=100, description="Sonuç sayfası (1-100; site sayfa başına 10 sonuç döndürür).")
    pageSize: int = Field(10, ge=1, le=10, description="Sayfa başına sonuç (site sabit 10'dur; bu değer yalnızca görüntüleme amaçlıdır).")

class TbbDecisionSummary(BaseModel):
    """Tek bir TBB Disiplin Kurulu kararının özet kaydı."""
    tarih: Optional[str] = Field(None, description="Karar tarihi (ör. 23.12.2023).")
    esas_no: Optional[str] = Field(None, description="Esas numarası (ör. 2023/919).")
    karar_no: Optional[str] = Field(None, description="Karar numarası (ör. 2023/1022).")
    ozet: Optional[str] = Field(None, description="Karar gerekçesi özeti (listede görünen metin).")
    detay_id: Optional[int] = Field(None, description="Detay belge kimliği; tam metin için get_tbb_document_markdown'a verilir.")
    document_id: Optional[str] = Field(None, description="Belge kimliği: 'tbb:<detay_id>' formatında.")

class TbbSearchResult(BaseModel):
    """TBB Disiplin Kurulu karar arama sonucu."""
    decisions: List[TbbDecisionSummary] = Field(default_factory=list, description="Bulunan kararların listesi.")
    total_results: Optional[int] = Field(None, description="Toplam eşleşen karar sayısı (son sayfa numarası × 10 tahmini).")
    page: int = Field(1, description="Geçerli sonuç sayfası.")
    pageSize: int = Field(10, description="Sayfa başına sonuç sayısı.")
    query: Optional[str] = Field(None, description="Aramada kullanılan anahtar kelimeler.")

class TbbDocumentMarkdown(BaseModel):
    """TBB kararının tam metninin sayfalanmış Markdown hali."""
    source_id: Optional[str] = Field(None, description="Kaynak belge kimliği (tbb:<detay_id>).")
    source_url: Optional[str] = Field(None, description="Kararın orijinal görüntüleme adresi.")
    tarih: Optional[str] = Field(None, description="Karar tarihi.")
    esas_no: Optional[str] = Field(None, description="Esas numarası.")
    karar_no: Optional[str] = Field(None, description="Karar numarası.")
    ozet: Optional[str] = Field(None, description="Karar özeti (blockquote).")
    markdown_chunk: Optional[str] = Field(None, description="5.000 karakterlik Markdown dilimi.")
    current_page: int = Field(description="Geçerli sayfa numarası (1-indeksli).")
    total_pages: int = Field(description="Toplam sayfa sayısı.")
    is_paginated: bool = Field(description="İçeriğin birden çok sayfaya bölünüp bölünmediği.")
    error_message: Optional[str] = Field(None, description="Hata mesajı (varsa).")