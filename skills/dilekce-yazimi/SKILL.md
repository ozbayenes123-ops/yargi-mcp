---
name: dilekce-yazimi
description: Use when drafting Turkish legal petitions with PII masking.
version: 1.0.0
author: ozbayenes123-ops
license: MIT
metadata:
  hermes:
    tags: [turkish-law, dilekce, hmk, cmk, pii-masking, legal-drafting]
    related_skills: [turkish-legal-tooling, turkish-law-research, docx]
---

# Dilekçe Yazımı (Türkiye)

Generate legally-grounded Turkish petitions from the template library at `C:\Users\ozbayenes123-ops\Desktop\Dilekce_Kutuphanesi\sablonlar\`, in compliant structure (HMK/CMK/İİK/TMK), natural Turkish, with mandatory PII masking.

## When to Use

- User asks for a Turkish court petition (dava, cevap, istinaf, temyiz, ceza savunması, tutukluluk itirazı).
- User needs an official application to a public institution (kurum dilekçesi, bilgi edinme).
- User mentions drafting family law (boşanma), enforcement (icra itirazı), or constitutional complaint (AYM/AİHM başvurusu) paperwork.
- Any case-document processing where personal identifiers must be masked before external/AI exposure.

## Hard Rules (never break)

1. **PII Masking (critical — user's core requirement).** Mask personal data before drafting, and especially before anything leaves the local workflow (external service, model, API). Use consistent placeholder labels the USER keeps in a local reversible alias table:
   - Name/Surname → [DAVACI], [DAVALI], [VEKIL], [SANIK], [TANIK_A]
   - T.C. Kimlik No → [TCKN]
   - Address/Phone/Email/IBAN/Vergi No → [ADRES], [TEL], [EMAIL], [IBAN], [VKN]
   - Dossier/Evrak nos → [DOSYA_NO]
   Never copy raw personal numbers/names from case documents into external/API/model content. If the user pastes PII, propose masked reformulation first.

2. **Statute grounding.** Never invent a statute number. Verify via `mcp__makale__fetch_article`, `mcp__yargi__get_mevzuat_document`, `mcp__yargi__mevzuat_ara` before citing. If uncertain, say so plainly.

3. **Structure compliance (HMK 119 minimum blocks for court pleadings).** Mahkeme mercii, taraflar/TCKN/adres, KONU, AÇIKLAMALAR (numbered vakıa + delil pairing), DELİLLER (mapped to vakıa), HUKUKİ SEBEPLER, NETİCE-İ TALEP, tarih/imza, EKLER.

4. **Style (TBB el kitabı s. 130-131).** Concise, clear, non-literary, legally accurate. Strongest argument first, strongest saved for last. No hypothetical speculation — ground in file/document fact.

5. **Natural modern Turkish.** Idiomatic legal usage; "talep ederim/arz ederim" closing is correct (HMK 114).

## Template Library

Base: `C:\Users\ozbayenes123-ops\Desktop\Dilekce_Kutuphanesi\`
- `sablonlar\` — 13 templates: 01 dava, 02 cevap, 03 istinaf, 04 tutuklamaya itiraz, 05 tutukluluk devamı/tahliye reddi itirazı, 06 delil toplatma, 07 soruşturmayı genişletme, 08 esas hakkında savunma, 09 AYM bireysel başvuru, 10 anlaşmalı boşanma+protokol, 11 çekişmeli boşanma, 12 ödeme emrine itiraz, 13 adi dilekçe (kurum başvurusu).
- `kaynaklar\` + `metinler\` — TBB Savunma Stratejisi ve Dilekçe Hazırlama, AÖF ADL102U Hukuk Dili, Adalet Bakanlığı Resmî Yazışma Kuralları.
- Reference taxonomy: Hukuk_Rehberi_TOC_4069.json on Desktop (4,069 titles, 34 chapters) for selecting petition type.

Workflow: pick template → collect facts WITH PII masking → fill [PLACEHOLDER] fields → verify statutes via tools → run the template's own checklist → produce .docx via makale/docx tooling into `Dilekce_Kutuphanesi\uretim\` (ask masked vs unmasked; default masked).

## Known pitfalls
- Deadline surfacing early: İTİRAZ 7 gün (İİK 62/2), cevap 2 hafta (HMK 127), istinaf 2 hafta (HMK 345; İYUK'da 30 gün), AYM 30 gün (6216/50), dilekçe hakkı cevabı 30/60 gün (3071/4).
- HMK 120 hard rejection trap: one missing element after the 1-week reminder kills the petition — run checklist before submit.
- Keep itirazlar (yetki/görev) separate from esas paragraphs (HMK 116-117).
- İcra: borca itiraz vs imzaya itiraz distinction (İİK 62) — pick the right lane.
- Anlaşmalı boşanma: marriage >1 yıl (TMK 166/3) + hâkim both parties' personal hearing + protocol's child-personal-relationship clause must be precise (TMK 184/2).
