# hsk_mcp_module/models.py

from pydantic import BaseModel, Field
from typing import List, Optional

class HskSearchRequest(BaseModel):
    """Model for Hâkimler ve Savcılar Kurulu (HSK) İkinci Daire disiplin kararı arama isteği."""
    keywords: Optional[str] = Field("", description="""
        Karar tam metninde aranacak Türkçe kelimeler (hepsi aynı kararda geçmelidir, diakritik duyarsız).
        Örnekler: "rüşvet", "mesleğin onurunu zedeleyen", "özel hayat"
    """)
    madde: Optional[str] = Field(None, description="""
        Yaptırım maddesi filtresi. Değerler: "uyarma", "ayliktan_kesme", "kinama",
        "kademe_ilerlemesi_durdurma", "derece_yukselmesi_durdurma", "yer_degistirme",
        "meslekten_cikarma", "ceza_tayinine_yer_olmadigi", "islemden_kaldirma".
        Boş bırakılırsa tüm maddeler taranır.
    """)
    page: int = Field(1, ge=1, le=100, description="Sonuç sayfası (1-100).")
    pageSize: int = Field(10, ge=1, le=50, description="Sayfa başına sonuç (1-50).")

class HskDecisionSummary(BaseModel):
    """Tek bir HSK İkinci Daire disiplin kararının özet kaydı."""
    madde: Optional[str] = Field(None, description="Yaptırım maddesi başlığı (ör. 'MESLEKTEN ÇIKARMA (MADDE 69)').")
    fikra: Optional[str] = Field(None, description="Fıkra kodu (ör. A-1, B-2, 1; fıkrası ayrılmamışsa 'tümü').")
    esas_no: Optional[str] = Field(None, description="Esas numarası (kaynakta anonimdir: '.....').")
    karar_no: Optional[str] = Field(None, description="Karar numarası (kaynakta anonimdir: '.....').")
    ozet: Optional[str] = Field(None, description="Kararın ilk ~250 karakteri.")
    uuid: Optional[str] = Field(None, description="PDF dosya kimliği (hsk.gov.tr/Eklentiler/Dosyalar/<uuid>.pdf).")
    sira: Optional[int] = Field(None, description="PDF içindeki karar sırası (1-indeksli).")
    document_id: Optional[str] = Field(None, description="Belge kimliği: 'hsk:disiplin:<uuid>:<sira>' formatında.")

class HskSearchResult(BaseModel):
    """HSK İkinci Daire disiplin kararı arama sonucu."""
    decisions: List[HskDecisionSummary] = Field(default_factory=list, description="Bulunan kararların listesi.")
    total_results: int = Field(0, description="Toplam eşleşen karar sayısı.")
    page: int = Field(1, description="Geçerli sonuç sayfası.")
    pageSize: int = Field(10, description="Sayfa başına sonuç sayısı.")
    query: Optional[str] = Field(None, description="Aramada kullanılan anahtar kelimeler.")
    taranan_pdf: int = Field(0, description="Taranan toplam PDF sayısı.")
    bilgi_notu: Optional[str] = Field(None, description="Bilgi notu (ör. kaynağın anonimliği).")

class HskDocumentMarkdown(BaseModel):
    """HSK disiplin kararının (PDF'den) sayfalanmış Markdown hali."""
    source_id: Optional[str] = Field(None, description="Kaynak belge kimliği (hsk:disiplin:<uuid>:<sira>).")
    source_url: Optional[str] = Field(None, description="Kararın yer aldığı PDF adresi.")
    madde: Optional[str] = Field(None, description="Yaptırım maddesi başlığı.")
    fikra: Optional[str] = Field(None, description="Fıkra kodu.")
    markdown_chunk: Optional[str] = Field(None, description="5.000 karakterlik Markdown dilimi.")
    current_page: int = Field(description="Geçerli sayfa numarası (1-indeksli).")
    total_pages: int = Field(description="Toplam sayfa sayısı.")
    is_paginated: bool = Field(description="İçeriğin birden çok sayfaya bölünüp bölünmediği.")
    error_message: Optional[str] = Field(None, description="Hata mesajı (varsa).")