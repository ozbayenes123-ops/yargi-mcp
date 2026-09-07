# spk_mcp_module/models.py

from pydantic import BaseModel, Field
from typing import List, Optional, Union

class SpkSearchRequest(BaseModel):
    """Model for Sermaye Piyasası Kurulu (SPK) mevzuat/ilke kararı/rehber arama isteği."""
    keywords: Optional[str] = Field("", description="""
        Aranacak Türkçe kelimeler (başlık, içerik veya her ikisinde).
        Örnekler: "açığa satış", "türev araç", "halka arz", "yatırım kuruluşları"
    """)
    search_field: Optional[str] = Field("all", description="""
        Arama alanı: "all" (hepsi), "title" (yalnızca başlık), "content" (yalnızca içerik).
    """)
    tur: Optional[str] = Field(None, description="""
        Belge türü filtresi. Değerler: "Kurul Kararı" (ilke kararları), "Rehber", "Tebliğ",
        "Yönetmelik", "Kanun", "Diğer Karar", "Diğer".
    """)
    bulten_yili: Optional[int] = Field(None, description="İlgili bülten yılı filtresi (ör. 2026).")
    bulten_no: Optional[int] = Field(None, description="İlgili bülten numarası filtresi (ör. 38).")
    page: int = Field(1, ge=1, le=100, description="Sonuç sayfası (1-100).")
    pageSize: int = Field(10, ge=1, le=50, description="Sayfa başına sonuç (1-50).")

class SpkDecisionSummary(BaseModel):
    """Tek bir SPK belgesinin (ilke kararı/rehber/tebliğ vb.) özet kaydı."""
    baslik: Optional[str] = Field(None, description="Belge başlığı (duyuru başlığı).")
    tur: Optional[str] = Field(None, description="Belge türü (ör. Kurul Kararı, Rehber, Tebliğ).")
    tarih: Optional[str] = Field(None, description="Kurul karar tarihi (dd.MM.yyyy).")
    toplanti_no: Optional[str] = Field(None, description="Kurul toplantı/karar numarası (ör. 36/1097).")
    resmi_gazete_tarihi: Optional[str] = Field(None, description="Resmî Gazete yayım tarihi (varsa).")
    resmi_gazete_sayisi: Optional[Union[str, int]] = Field(None, description="Resmî Gazete sayısı (varsa).")
    bulten_yili: Optional[int] = Field(None, description="İlgili SPK bülteninin yılı.")
    bulten_no: Optional[int] = Field(None, description="İlgili SPK bülteninin numarası.")
    kisim: Optional[str] = Field(None, description="Mevzuat kısmı (ör. VIII. KISIM).")
    bolum: Optional[str] = Field(None, description="Mevzuat bölümü.")
    baglantili_tebligler: Optional[str] = Field(None, description="Bağlantılı tebliğler (varsa).")
    ilgili_konular: Optional[str] = Field(None, description="İlgili konular (varsa).")
    content_source: Optional[str] = Field(None, description="İçerik kaynağı: IlkeKarari, Rehber veya Mevzuat.")
    content_id: Optional[int] = Field(None, description="İçerik kimliği (dosya indirme için).")
    document_id: Optional[str] = Field(None, description="Belge kimliği: 'spk:<kaynak>:<id>' formatında; tam metin için get_spk_document_markdown'a verilir.")
    ilgili_bulten: Optional[str] = Field(None, description="İlgili bülten (ör. 2026/38).")
    ilgili_bulten_document_id: Optional[str] = Field(None, description="İlgili bültenin belge kimliği (spk:bulten:2026/38).")

class SpkSearchResult(BaseModel):
    """SPK belge arama sonucu."""
    decisions: List[SpkDecisionSummary] = Field(default_factory=list, description="Bulunan belgelerin listesi.")
    total_results: int = Field(0, description="Toplam eşleşen belge sayısı.")
    page: int = Field(1, description="Geçerli sonuç sayfası.")
    pageSize: int = Field(10, description="Sayfa başına sonuç sayısı.")
    query: Optional[str] = Field(None, description="Aramada kullanılan anahtar kelimeler.")

class SpkBultenSummary(BaseModel):
    """Tek bir SPK haftalık bülteninin özet kaydı."""
    no: Optional[str] = Field(None, description="Bülten numarası (ör. 2026/38).")
    yil: Optional[int] = Field(None, description="Bülten yılı.")
    tarih: Optional[str] = Field(None, description="Bülten tarihi (varsa).")
    baslik: Optional[str] = Field(None, description="Bülten başlığı.")
    pdf_url: Optional[str] = Field(None, description="Bülten PDF adresi.")
    document_id: Optional[str] = Field(None, description="Bülten belge kimliği (spk:bulten:2026/38).")

class SpkDocumentMarkdown(BaseModel):
    """SPK belgesinin (ilke kararı/rehber/mevzuat/bülten) sayfalanmış Markdown hali."""
    source_id: Optional[str] = Field(None, description="Kaynak belge kimliği (spk:<kaynak>:<id>).")
    source_url: Optional[str] = Field(None, description="Belgenin kaynak adresi.")
    baslik: Optional[str] = Field(None, description="Belge başlığı.")
    tur: Optional[str] = Field(None, description="Belge türü.")
    tarih: Optional[str] = Field(None, description="Kurul karar tarihi (varsa).")
    toplanti_no: Optional[str] = Field(None, description="Kurul toplantı/karar numarası (varsa).")
    markdown_chunk: Optional[str] = Field(None, description="5.000 karakterlik Markdown dilimi.")
    current_page: int = Field(description="Geçerli sayfa numarası (1-indeksli).")
    total_pages: int = Field(description="Toplam sayfa sayısı.")
    is_paginated: bool = Field(description="İçeriğin birden çok sayfaya bölünüp bölünmediği.")
    error_message: Optional[str] = Field(None, description="Hata mesajı (varsa).")