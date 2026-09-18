# Annual Report Cost Table Extractor

This project extracts cost-analysis tables from Chinese annual-report PDFs and writes normalized CSV files.

The extractor does not assume that the table is always on page 43. It scans every page, combines keyword similarity with optional semantic embedding similarity, selects the top-scoring page, scans forward for split-table continuations, normalizes extracted rows, converts money units to million RMB, and writes per-report plus combined CSV outputs.


## Install

From `annual_report/table_extraction`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Run the full default pipeline:

```bash
python -m annual_report_extractor.extractor
```

Run without embeddings:

```bash
python -m annual_report_extractor.extractor --disable-embeddings
```

Run with the cached embedding model and no Hugging Face network checks:

```bash
python -m annual_report_extractor.extractor --require-embeddings --offline-embeddings
```

## Useful Options

```bash
python -m annual_report_extractor.extractor --keyword-weight 0.4 --semantic-weight 0.6
python -m annual_report_extractor.extractor --semantic-max-chars 3000
python -m annual_report_extractor.extractor --max-continuation-pages 2
python -m annual_report_extractor.extractor --min-output-confidence 0.75
```

## Output Schema

```text
report_year
company
source_file
source_page
section_heading
industry
cost_item
current_amount_million_rmb
current_ratio_pct
prior_amount_million_rmb
prior_ratio_pct
yoy_change_pct
raw_unit
confidence
```

For tables whose unit is `万元`, amount conversion is:

```text
million RMB = 万元 / 100
```

## How It Works

1. Opens each PDF with `pdfplumber`.
2. Extracts text from every page.
3. Computes a keyword score from anchors such as `成本分析表`, `成本构成`, `分行业情况`, `成本构成项目`, `本期金额`, and `上年同期金额`.
4. Optionally embeds each page and a cost-analysis query, then computes semantic cosine similarity.
5. Normalizes keyword and semantic scores to the same `0..1` scale.
6. Selects the single top-scoring page by combined score.
7. Extracts tables from that page and scans forward for split-table continuation pages.
8. Inherits the money unit from nearby pages when the unit label appears on the page before the table body.
9. Removes repeated cost-item rows, keeping the first row in document order for each section/industry/item.
10. Drops rows with `confidence < 0.75` from CSV output by default.
11. Writes per-report CSV files, a combined CSV, and `diagnostics.json`.

## Pre-2012 Alternate Table

Older reports may not contain the modern `成本分析表`. For reports such as 2006-2011, the extractor also supports old business-segment tables such as `主营业务分行业、分产品情况表`, `主营业务分行业、产品情况`, and `主营业务分产品情况`.

For this older layout:

- `industry` is taken from `分行业或分产品` or `分行业或产品`.
- Once the old-format table is detected, every valid item row in the table is extracted.
- `空调` is normalized to `空调器`; `电冷柜` is normalized to `电冰柜`; `其他`, `其它`, and `其它产品` are normalized to `其他产品`.
- If the table does not provide `合计`, the extractor creates it by summing the kept `分行业或分产品` rows.
- `cost_item` is detected from the table header as `主营业务成本` or `营业成本`.
- `current_amount_million_rmb` uses only `主营业务成本` / `营业成本`; `主营业务收入` / `营业收入` is ignored.
- `current_ratio_pct` is each row's cost amount divided by the `合计` row's cost amount.
- `prior_amount_million_rmb` is calculated as `cost amount / (1 + cost yoy change / 100)`.
- `prior_ratio_pct` is each calculated prior amount divided by the `合计` calculated prior amount.
- `合计` has `current_ratio_pct = 100` and `prior_ratio_pct = 100`.
- `yoy_change_pct` is copied from `主营业务成本比上年增减（％）` / `营业成本比上年增减(%)`.
- Revenue, profit margin, revenue growth, and profit-margin growth columns are ignored.
- When `pdfplumber` separates product names from the numeric grid in pre-2012 reports, the extractor falls back to parsing the page text for the same old-layout fields.

## Diagnostics

`diagnostics.json` records:

- selected page and extracted pages
- keyword, semantic, and combined retrieval scores
- embedding status
- warnings
- raw extracted row count and written CSV row count
- number of low-confidence rows dropped
