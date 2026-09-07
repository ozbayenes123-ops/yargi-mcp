# -*- coding: utf-8 -*-
"""Tests for the natural-language Bedesten query parser."""

import pytest

from bedesten_mcp_module.query_parser import (
    parse_search_query,
    _apply_and_semantics,
    _looks_like_boolean_query,
)


# --- Court type detection -------------------------------------------------

def test_yargitay_detection():
    q = parse_search_query("Yargıtay muvazaa tapu iptali")
    assert q.court_types == ["YARGITAYKARARI"]
    assert any("YARGITAYKARARI" in d for d in q.detections)


def test_danistay_detection():
    q = parse_search_query("danıştay vergi uyuşmazlığı")
    assert q.court_types == ["DANISTAYKARAR"]


def test_istinaf_detection():
    q = parse_search_query("istinaf \"sözleşme ihlali\"")
    assert q.court_types == ["ISTINAFHUKUK"]


def test_yerel_hukuk_detection():
    q = parse_search_query("tüketici mahkemesi ayıplı mal")
    assert q.court_types == ["YERELHUKUK"]


def test_kyb_detection():
    q = parse_search_query("kanun yararına bozma usulsüz tebligat")
    assert q.court_types == ["KYB"]


def test_default_courts_when_none_detected():
    q = parse_search_query("marka lisansı")
    assert q.court_types == ["YARGITAYKARARI", "DANISTAYKARAR"]


# --- Chamber detection ----------------------------------------------------

def test_yargitay_chamber():
    q = parse_search_query("Yargıtay 1. Hukuk Dairesi mülkiyet 2023")
    assert q.birim_adi == "H1"
    assert q.court_types == ["YARGITAYKARARI"]
    assert "mülkiyet" in q.phrase


def test_danistay_chamber():
    q = parse_search_query("danıştay 3. daire vergi 2022-2024")
    assert q.birim_adi == "D3"
    assert q.court_types == ["DANISTAYKARAR"]


def test_chamber_implies_court():
    q = parse_search_query("4. Ceza Dairesi hırsızlık")
    assert q.birim_adi == "C4"
    assert q.court_types == ["YARGITAYKARARI"]


def test_general_assembly():
    q = parse_search_query("hukuk genel kurulu tazminat")
    assert q.birim_adi == "HGK"


# --- Dates ----------------------------------------------------------------

def test_year_detection():
    q = parse_search_query("tapu iptali 2023")
    assert q.karar_tarihi_start == "2023-01-01T00:00:00.000Z"
    assert q.karar_tarihi_end == "2023-12-31T23:59:59.999Z"
    assert "2023" not in q.phrase


def test_year_range_detection():
    q = parse_search_query("tapu iptali 2021-2023")
    assert q.karar_tarihi_start == "2021-01-01T00:00:00.000Z"
    assert q.karar_tarihi_end == "2023-12-31T23:59:59.999Z"


def test_explicit_date_detection():
    q = parse_search_query("tapu iptali 12.05.2021")
    assert q.karar_tarihi_start == "2021-05-12T00:00:00.000Z"
    assert q.karar_tarihi_end == "2021-05-12T23:59:59.999Z"


# --- Case numbers ---------------------------------------------------------

def test_esas_karar_labels():
    q = parse_search_query("Esas No: 2023/1234 Karar No: 2025/5678")
    assert q.esas_no == "2023/1234"
    assert q.karar_no == "2025/5678"


def test_esas_karar_e_k():
    q = parse_search_query("E.2023/1234 K.2024/567")
    assert q.esas_no == "2023/1234"
    assert q.karar_no == "2024/567"
    assert '"2023/1234"' in q.phrase
    assert '"2024/567"' in q.phrase


# --- Phrase cleaning & operators (regression for the fixed bugs) ----------

def test_quoted_and_operator_order_preserved():
    q = parse_search_query('"marka" AND "finansal kiralama"')
    assert q.phrase == '"marka" AND "finansal kiralama"'


def test_boolean_query_untouched():
    assert parse_search_query("muvazaa AND tapu AND iptal").phrase == "muvazaa AND tapu AND iptal"
    assert parse_search_query("ecrimisil OR haksız işgal").phrase == "ecrimisil OR haksız işgal"
    assert parse_search_query("muvazaa NOT miras").phrase == "muvazaa NOT miras"


def test_structural_tokens_removed_from_phrase():
    q = parse_search_query("Yargıtay 1. Hukuk Dairesi mülkiyet hakkı 2023")
    assert q.phrase == "mülkiyet hakkı"


def test_loose_multiword_gets_and_semantics():
    q = parse_search_query("marka hakkı finansal kiralama sözleşmesi")
    assert q.phrase == "+marka +hakkı +finansal +kiralama +sözleşmesi"
    assert any("AND semantiği" in d for d in q.detections)


def test_short_phrase_kept_loose():
    q = parse_search_query("marka lisansı")
    assert q.phrase == "marka lisansı"


def test_quoted_query_skips_and_semantics():
    q = parse_search_query('istinaf "sözleşme ihlali"')
    assert q.phrase == '"sözleşme ihlali"'


def test_case_number_query_skips_and_semantics():
    q = parse_search_query("E.2023/1234 K.2024/567")
    assert q.phrase == '"2023/1234" "2024/567"'


# --- Helpers --------------------------------------------------------------

def test_apply_and_semantics_rules():
    assert _apply_and_semantics("a b c") == "+a +b +c"
    assert _apply_and_semantics("a b") == "a b"  # fewer than 3 terms
    assert _apply_and_semantics("+a +b +c") == "+a +b +c"  # already required


def test_looks_like_boolean_query():
    assert _looks_like_boolean_query("muvazaa AND tapu")
    assert _looks_like_boolean_query('"muvazaa"')
    assert _looks_like_boolean_query("+muvazaa tapu")
    assert not _looks_like_boolean_query("muvazaa tapu iptal")


# --- Edge cases -----------------------------------------------------------

def test_empty_query():
    q = parse_search_query("   ")
    assert q.phrase == ""
    assert q.detections


def test_empty_query_none():
    q = parse_search_query("")
    assert q.phrase == ""
