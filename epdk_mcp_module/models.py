# epdk_mcp_module/models.py

from pydantic import BaseModel, Field
from typing import List, Optional

class EpdkSearchRequest(BaseModel):
    """Model for Enerji Piyasası Düzenleme Kurumu (EPDK) Kurul kararı arama isteği."""
    keywords: Optional[str] = Field("", description="""
        Karar başlığı/açıklamasında aranacak Türkçe kelimeler (tümü aynı kayıtta aranır).
        Örnekler: "TORETOSAF", "bağlantı bedeli", "tarife"
    """)
    piyasa: Optional[str] = Field(None, description="""
        Piyasa (sektör) filtresi. Değerler: "elektrik" (varsayılan), "dogalgaz", "petrol",
        "lpg", "denetim", "enerji_donusumu".
    """)
    page: int = Field(1, ge=1, le=100, description="Sonuç sayfası (1-100).")
    pageSize: int = Field(10, ge=1, le=50, description="Sayfa başına sonuç (1-50).")

class EpdkKararSummary(BaseModel):
    """Tek bir EPDK Kurul kararının özet kaydı."""
    baslik: Optional[str] = Field(None, description="Karar başlığı/açıklaması (EPDK listesindeki konu metni).")
    karar_no: Optional[str] = Field(None, description="Karar numarası (ör. 13976).")
    karar_tarihi: Optional[str] = Field(None, description="Karar tarihi (ör. 27.11.2025).")
    rg_tarihi: Optional[str] = Field(None, description="Resmî Gazete yayım tarihi (varsa).")
    rg_sayisi: Optional[str] = Field(None, description="Resmî Gazete sayı numarası (varsa).")
    piyasa: Optional[str] = Field(None, description="Piyasa (elektrik/dogalgaz/petrol/lpg/denetim/enerji_donusumu).")
    kategori: Optional[str] = Field(None, description="Kategori yolu (ör. 'Elektrik Piyasası Tarife Kurul Kararları > TORETOSAF').")
    mulga: Optional[str] = Field(None, description="Mülga (yürürlükten kaldırılmış) bilgisi (varsa).")
    dosya_sayisi: Optional[int] = Field(0, description="Karara ilişkin ek belge (dosya) sayısı.")
    content_id: Optional[str] = Field(None, description="İndirilecek belgenin kimliği (DownloadDocument?id=).")
    document_id: Optional[str] = Field(None, description="Belge kimliği: 'epdk:<content_id>' formatında.")

class EpdkSearchResult(BaseModel):
    """EPDK Kurul kararı arama sonucu."""
    decisions: List[EpdkKararSummary] = Field(default_factory=list, description="Bulunan kararların listesi.")
    total_results: int = Field(0, description="Toplam eşleşen karar sayısı.")
    page: int = Field(1, description="Geçerli sonuç sayfası.")
    pageSize: int = Field(10, description="Sayfa başına sonuç sayısı.")
    query: Optional[str] = Field(None, description="Aramada kullanılan anahtar kelimeler.")
    piyasa: Optional[str] = Field(None, description="Taranan piyasa.")
    cache_yasi: Optional[str] = Field(None, description="Kullanılan liste önbelleğinin yaşı (ilk arama piyasa listesini indirir, sonrakiler önbellekten gelir).")

class EpdkDocumentMarkdown(BaseModel):
    """EPDK karar belgesinin (docx/pdf) sayfalanmış Markdown hali."""
    source_id: Optional[str] = Field(None, description="Kaynak belge kimliği (epdk:<content_id>).")
    source_url: Optional[str] = Field(None, description="Belgenin orijinal indirme adresi.")
    baslik: Optional[str] = Field(None, description="Belge başlığı.")
    karar_no: Optional[str] = Field(None, description="Karar numarası.")
    karar_tarihi: Optional[str] = Field(None, description="Karar tarihi.")
    belge_turu: Optional[str] = Field(None, description="Belge türü (docx/pdf/excel).")
    markdown_chunk: Optional[str] = Field(None, description="5.000 karakterlik Markdown dilimi.")
    current_page: int = Field(description="Geçerli sayfa numarası (1-indeksli).")
    total_pages: int = Field(description="Toplam sayfa sayısı.")
    is_paginated: bool = Field(description="İçeriğin birden çok sayfaya bölünüp bölünmediği.")
    error_message: Optional[str] = Field(None, description="Hata mesajı (varsa).")