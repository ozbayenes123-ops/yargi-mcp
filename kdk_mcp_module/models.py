# kdk_mcp_module/models.py

from pydantic import BaseModel, Field
from typing import List, Optional

class KdkSearchRequest(BaseModel):
    """Model for Kamu Denetçiliği Kurumu (Ombudsmanlık) karar arama isteği."""
    keywords: Optional[str] = Field("", description="""
        Karar konusunda (evrak konusu) aranacak Türkçe kelimeler.
        Örnekler: "eğitim hakkı", "tapu iptali", "öğretmen ataması"
    """)
    karar_turu: Optional[str] = Field(None, description="""
        Karar türü filtresi. Değerler: "Tavsiye Kararı", "Ret Kararı",
        "Kısmen Tavsiye Kısmen Ret Kararı", "Kısmen Ret Kısmen Tavsiye Kararı" vb.
    """)
    sikayet_konu: Optional[str] = Field(None, description="""
        Şikâyet konusu kategorisi filtresi. Değerler örnek: "Eğitim-öğretim, gençlik ve spor",
        "Sağlık", "Mülkiyet hakkı", "Kamu personel rejimi", "Adalet, milli savunma ve güvenlik",
        "Ekonomi, maliye ve vergi", "Çalışma ve sosyal güvenlik", "Mahalli idarelerce yürütülen hizmetler" vb.
    """)
    page: int = Field(1, ge=1, le=100, description="Sonuç sayfası (1-100).")
    pageSize: int = Field(10, ge=1, le=50, description="Sayfa başına sonuç (1-50).")

class KdkDecisionSummary(BaseModel):
    """Tek bir KDK kararının özet kaydı."""
    karar_no: Optional[str] = Field(None, description="Karar numarası (ör. 2026/12850).")
    basvuru_no: Optional[str] = Field(None, description="Başvuru numarası (ör. 2026/14662).")
    karar_tarihi: Optional[str] = Field(None, description="Karar tarihi (ISO format).")
    karar_turu: Optional[str] = Field(None, description="Karar türü (Tavsiye/Ret vb.).")
    konu: Optional[str] = Field(None, description="Karar konusu (evrak konusu) açıklaması.")
    idare: Optional[str] = Field(None, description="Şikâyet edilen kurum/idare adı.")
    sikayet_konu: Optional[str] = Field(None, description="Şikâyet konusu kategorisi.")
    sikayet_alt_konu: Optional[str] = Field(None, description="Şikâyet konusu alt kategorisi.")
    ozet: Optional[str] = Field(None, description="Karar özeti (KB B özeti).")
    evrak_id: Optional[int] = Field(None, description="Belge kimliği (evrak ID); tam metin için get_kdk_document_markdown'a verilir.")
    yayin_url: Optional[str] = Field(None, description="Yayınlanan karar PDF yol bilgisi.")
    document_id: Optional[str] = Field(None, description="Belge kimliği: 'kdk:<evrak_id>' formatında.")

class KdkSearchResult(BaseModel):
    """KDK karar arama sonucu."""
    decisions: List[KdkDecisionSummary] = Field(default_factory=list, description="Bulunan kararların listesi.")
    total_results: int = Field(0, description="Toplam eşleşen karar sayısı.")
    page: int = Field(1, description="Geçerli sonuç sayfası.")
    pageSize: int = Field(10, description="Sayfa başına sonuç sayısı.")
    query: Optional[str] = Field(None, description="Aramada kullanılan anahtar kelimeler.")

class KdkDocumentMarkdown(BaseModel):
    """KDK kararının tam metninin sayfalanmış Markdown hali."""
    source_id: Optional[str] = Field(None, description="Kaynak belge kimliği (kdk:<evrak_id>).")
    source_url: Optional[str] = Field(None, description="Kararın orijinal görüntüleme/indirme adresi.")
    karar_no: Optional[str] = Field(None, description="Karar numarası.")
    basvuru_no: Optional[str] = Field(None, description="Başvuru numarası.")
    karar_turu: Optional[str] = Field(None, description="Karar türü.")
    idare: Optional[str] = Field(None, description="Şikâyet edilen kurum/idare adı.")
    konu: Optional[str] = Field(None, description="Karar konusu.")
    markdown_chunk: Optional[str] = Field(None, description="5.000 karakterlik Markdown dilimi.")
    current_page: int = Field(description="Geçerli sayfa numarası (1-indeksli).")
    total_pages: int = Field(description="Toplam sayfa sayısı.")
    is_paginated: bool = Field(description="İçeriğin birden çok sayfaya bölünüp bölünmediği.")
    error_message: Optional[str] = Field(None, description="Hata mesajı (varsa).")