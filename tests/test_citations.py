# -*- coding: utf-8 -*-
"""Tests for citation extraction from Turkish decision texts."""

from bedesten_mcp_module.citations import extract_citations


SAMPLE = """T.C. Yargıtay 1. Hukuk Dairesi
Esas No: 2024/1234 Karar No: 2025/5678

GEREKÇE

Dairemizin 20.11.2019 tarihli ve 2019/1500 E., 2019/6500 K. sayılı kararı
uyarınca muris muvazaasına dayalı tapu iptali ve tescil davalarında ...

Yargıtay HGK'nun E. 2018/400 K. 2018/900 sayılı kararı da bu yöndedir.

Nitekim 2020/1234 sayılı kararımız ile 2019/88 sayılı ilamına atıf yapılmıştır.

HÜKÜM
Yukarıdaki nedenlerle hükmün BOZULMASINA ... oy birliğiyle karar verildi.
"""


def test_extracts_pair_citations_number_first():
    citations = extract_citations(SAMPLE, maks_atif=10)
    pairs = [c for c in citations if c["atif_turu"] == "esas_karar"]
    nums = {(c["esas_no"], c["karar_no"]) for c in pairs}
    assert ("2019/1500", "2019/6500") in nums
    assert ("2018/400", "2018/900") in nums


def test_extracts_single_number_citations():
    citations = extract_citations(SAMPLE, maks_atif=10)
    singles = [c for c in citations if c["atif_turu"] == "karar_no"]
    assert ("2020/1234") in {c["karar_no"] for c in singles}
    assert ("2019/88") in {c["karar_no"] for c in singles}


def test_excludes_own_case_numbers():
    citations = extract_citations(SAMPLE, own_numbers=[("2024/1234", "2025/5678")], maks_atif=10)
    nums = {(c["esas_no"], c["karar_no"]) for c in citations}
    assert ("2024/1234", "2025/5678") not in nums


def test_context_snippet_present():
    citations = extract_citations(SAMPLE, maks_atif=10)
    c = next(x for x in citations if x["esas_no"] == "2019/1500")
    assert "muris muvazaası" in c["baglam"]
    assert len(c["baglam"]) > 20


def test_maks_atif_cap():
    text = " ".join(
        f"karar {2010 + i}/10{i} E. {2010 + i}/20{i} K. sayılıdır"
        for i in range(30)
    )
    citations = extract_citations(text, maks_atif=5)
    assert len(citations) <= 5


def test_deduplicates_repeated_citations():
    text = ("E. 2019/1500 K. 2019/6500 sayılı kararı ... " * 3) + "E. 2020/1 K. 2020/2 sayılı kararı"
    citations = extract_citations(text, maks_atif=10)
    pair_keys = {(c["esas_no"], c["karar_no"]) for c in citations if c["atif_turu"] == "esas_karar"}
    assert ("2019/1500", "2019/6500") in pair_keys
    assert len(pair_keys) == 2  # each unique citation appears once


def test_empty_text_returns_nothing():
    assert extract_citations("", maks_atif=10) == []


def test_header_numbers_not_treated_as_single_citations():
    text = "Esas No: 2024/1234 Karar No: 2025/5678 GEREKÇE 2020/1 sayılı kararı"
    singles = [c["karar_no"] for c in extract_citations(text, maks_atif=10) if c["atif_turu"] == "karar_no"]
    assert "2024/1234" not in singles
    assert "2025/5678" not in singles
    assert "2020/1" in singles


def test_pair_citation_not_duplicated_as_single():
    text = "E. 2018/400 K. 2018/900 sayılı kararı uyarınca"
    citations = extract_citations(text, maks_atif=10)
    karars = [c["karar_no"] for c in citations if c["atif_turu"] == "karar_no"]
    assert "2018/900" not in karars
    pairs = [c for c in citations if c["atif_turu"] == "esas_karar"]
    assert ("2018/400", "2018/900") in {(c["esas_no"], c["karar_no"]) for c in pairs}


def test_own_numbers_also_exclude_single_references():
    text = "GEREKÇE 2025/5678 sayılı kararımız ile 2019/88 sayılı ilamına"
    citations = extract_citations(text, own_numbers=[("2024/1234", "2025/5678")], maks_atif=10)
    singles = [c["karar_no"] for c in citations if c["atif_turu"] == "karar_no"]
    assert "2025/5678" not in singles
    assert "2019/88" in singles
