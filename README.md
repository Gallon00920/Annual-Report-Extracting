# Annual Report Data Extraction

This project is designed to automate two stages of data collection from Chinese listed company annual reports:

1. Download annual report PDF files from the Shanghai Stock Exchange.
2. Parse the PDF documents and extract structured cost-analysis tables for downstream analysis.

The project is centered around two main scripts:

- scraping/annual_report_scraping.py
- table_extraction/annual_report_extractor/extractor.py

---

## 1. Report Download Script

File: scraping/annual_report_scraping.py

This script uses Selenium with Chrome to browse the Shanghai Stock Exchange disclosure page and automatically trigger PDF downloads for the annual reports of stock code 600690.

### What it does

- Opens the SSE annual report listing page.
- Enters the stock code 600690 into the search box.
- Selects the annual report category.
- Loops through years from 2008 to 2026.
- Sets a date range for each year and reloads the listing page.
- Finds annual report title links.
- Filters out unrelated entries such as summary versions or English-language reports.
- Clicks each relevant PDF report to trigger a browser download.

### Technical details

- Uses ChromeDriver through Selenium.
- Configures Chrome download settings with a custom local directory.
- Uses experimental browser preferences to automatically save PDFs instead of opening them in a browser tab.
- Includes waits and JavaScript clicks to handle dynamic page behavior and overlapping UI elements.
- Saves files to a directory configured via SAVE_DIR, which currently needs to be set manually.

### Important notes

- This script is a browser automation script for downloading reports, not a parser.
- It is tightly coupled to the SSE page structure and may require updates if the website changes.
- The target stock code is hardcoded as 600690, so it is not generalized for other companies.
- The script is effectively a pipeline for preparing PDF inputs for the extraction stage.

---

## 2. PDF Extraction and Table Parsing Script

File: table_extraction/annual_report_extractor/extractor.py

This script processes downloaded PDF annual reports and extracts rows from the cost-analysis section, turning unstructured report pages into CSV data.

### Main purpose

The script is designed to find pages in each annual report that contain Chinese cost analysis tables such as:

- 成本分析表
- 成本构成
- 成本结构
- 分行业情况

It then identifies rows that contain cost categories such as:

- 主营业务成本
- 原材料
- 人工
- 折旧
- 能源
- 其他

and extracts the numeric values associated with each category.

### Core workflow

1. Read the PDF and extract text from each page.
2. Score pages based on keywords and headings to find likely relevant pages.
3. Optionally use semantic matching with sentence-transformers embeddings to improve page selection.
4. Extract tables from the selected page and nearby pages.
5. Normalize numeric values and units (for example, yuan or ten thousand yuan).
6. Convert values to a consistent unit, such as million RMB.
7. Dedupe, validate, and filter rows by confidence score.
8. Write results to CSV and diagnostics JSON files.

### Key components

#### Page scoring and candidate selection

The script defines target headings and structural patterns. It computes a keyword score for each page and can combine that score with a semantic similarity score using embedding vectors.

This is implemented with:

- TARGET_HEADING_PATTERNS
- TABLE_HEADER_PATTERNS
- COST_ITEM_KEYWORDS
- page_score()
- find_candidates()

This allows it to rank pages likely to include the cost-analysis section before doing detailed extraction.

#### PDF handling and repair

The script opens PDFs with pdfplumber and includes a repair path using pypdf when a PDF cannot be opened normally. This makes the tool more resilient to corrupted or imperfect files.

Important helpers include:

- pdf_header_warning()
- open_pdf_with_repair()

#### Data normalization and parsing

The extraction logic converts text values into numbers with regex-based parsing and normalizes units:

- normalize_text()
- visible_text()
- parse_number()
- detect_unit()
- amount_to_million_rmb()

It also detects page metadata such as company name and report year from the document title and the first page text.

#### Table extraction logic

The script scans page tables and uses relevance checks to keep only likely cost-analysis sections. It then extracts rows from those tables and tries to map them into structured fields, including:

- report_year
- company
- source_file
- source_page
- section_heading
- industry
- cost_item
- current_amount_million_rmb
- current_ratio_pct
- prior_amount_million_rmb
- prior_ratio_pct
- yoy_change_pct
- raw_unit
- confidence

This is done through dataclasses such as:

- ExtractionResult
- PageCandidate

#### Validation and output

The script validates extracted rows to check whether expected items are present and whether the sum of cost components is reasonably close to the total operating cost. It also writes:

- a per-year CSV file under the outputs directory
- a combined CSV file for all years
- diagnostics.json with warnings and extraction metadata

The main CLI entry point is main(), which supports arguments such as:

- --input
- --output-dir
- --combined-name
- --embedding-model
- --disable-embeddings
- --require-embeddings
- --offline-embeddings
- --keyword-weight
- --semantic-weight
- --semantic-max-chars
- --max-continuation-pages
- --min-output-confidence

---

## End-to-End Workflow

The repository's data flow is:

1. Use the Selenium download script to save annual report PDFs.
2. Put downloaded PDFs in the reports folder.
3. Run the extractor script to analyze PDF pages and identify the cost-analysis tables.
4. Export the extracted data to CSV files in the outputs folder.
5. Review diagnostics.json for warnings or failed extractions.

---

## Practical Summary

This project is essentially an automated workflow for converting annual report PDFs into structured business cost data.

- The scraping script gathers the raw input files.
- The extractor script transforms those reports into tabular cost data suitable for analysis.

In short, it combines browser automation, PDF text extraction, rule-based table detection, and output validation to build a reusable annual-report data pipeline.
