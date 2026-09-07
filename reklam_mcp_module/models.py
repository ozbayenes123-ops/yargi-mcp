# reklam_mcp_module/models.py

from pydantic import BaseModel, Field
from typing import List, Optional

class ReklamBultenSummary(BaseModel):
    """Reklam Kurulu (Ticaret Bakanlığı) basın bülteni özet kaydı."""
    toplanti_no: int = Field(description="Toplantı (bülten) sayısı (ör. 370).")
    toplanti_tarihi: Optional[str] = Field(None, description="Toplantı tarihi (ör. 11.06.2026).")
    yil: Optional[int] = Field(None, description="Bülten yılı (sayfadaki yıl başlığından).")
    pdf_url: Optional[str] = Field(None, description="Bülten PDF/DOCX adresi.")
    document_id: Optional[str] = Field(None, description="Belge kimliği: 'reklam:<toplanti_no>' formatında.")

class ReklamBultenListe(BaseModel):
    """Reklam Kurulu basın bültenleri listesi sonucu."""
    bultenler: List[ReklamBultenSummary] = Field(default_factory=list, description="Bültenler (en yeni ilk).")
    yil: Optional[int] = Field(None, description="Uygulanan yıl filtresi (boşsa tüm yıllar).")
    total: int = Field(0, description="Toplam bülten sayısı.")
    error_message: Optional[str] = Field(None, description="Hata mesajı (varsa).")

class ReklamKararEslestirme(BaseModel):
    """Bülten içi aramada eşleşen tek karar."""
    karar_no: Optional[str] = Field(None, description="Karar sıra numarası (bültendeki numaralandırma).")
    dosya_no: Optional[str] = Field(None, description="Dosya numarası (ör. 2023/1234).")
    sikayet_edilen: Optional[str] = Field(None, description="Şikayet edilen firma / kişi.")
    kategori: Optional[str] = Field(None, description="İlgili kategori (ör. 'Gıda', 'Kozmetik').")
    alinti: Optional[str] = Field(None, description="Eşleşme çevresinden ~400 karakterlik alıntı.")
    alaka_skoru: int = Field(0, description="Eşleşme sayısı (alaka skoru).")

class ReklamBultenIciAramaSonucu(BaseModel):
    """Reklam Kurulu bülteni içi anahtar kelime arama sonucu."""
    toplanti_no: int = Field(description="Taranan bülten (toplantı) sayısı.")
    keywords: Optional[str] = Field(None, description="Aranan anahtar kelimeler.")
    total: int = Field(0, description="Eşleşen karar sayısı.")
    kararlar: List[ReklamKararEslestirme] = Field(default_factory=list, description="Eşleşen kararlar (alaka sırasıyla).")
    error_message: Optional[str] = Field(None, description="Hata mesajı (varsa).")

class ReklamBultenMarkdown(BaseModel):
    """Reklam Kurulu basın bülteninin sayfalanmış Markdown hali."""
    source_id: Optional[str] = Field(None, description="Kaynak belge kimliği (reklam:<toplanti_no>).")
    source_url: Optional[str] = Field(None, description="Bülten PDF/DOCX adresi.")
    toplanti_no: Optional[int] = Field(None, description="Toplantı sayısı.")
    toplanti_tarihi: Optional[str] = Field(None, description="Toplantı tarihi.")
    karar_sayisi: Optional[int] = Field(None, description="Bültendeki karar sayısı.")
    markdown_chunk: Optional[str] = Field(None, description="5.000 karakterlik Markdown dilimi.")
    current_page: int = Field(description="Geçerli sayfa numarası (1-indeksli).")
    total_pages: int = Field(description="Toplam sayfa sayısı.")
    is_paginated: bool = Field(description="İçeriğin birden çok sayfaya bölünüp bölünmediği.")
    error_message: Optional[str] = Field(None, description="Hata mesajı (varsa).")