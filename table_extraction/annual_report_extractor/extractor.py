from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pdfplumber


TARGET_HEADING_PATTERNS = [
    "成本分析表",
    "成本分析",
    "成本构成",
    "成本结构",
    "分行业情况",
]

TABLE_HEADER_PATTERNS = [
    "成本构成项目",
    "本期金额",
    "本期占总成本比例",
    "上年同期金额",
    "上年同期占总成本比例",
    "本期金额较上年同期变动比例",
]

COST_ITEM_KEYWORDS = ["主营业务成本", "原材料", "人工", "折旧", "能源", "其他"]
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
DEFAULT_SEMANTIC_QUERY = (
    "成本分析表 成本结构 成本构成 分行业情况 主营业务成本 原材料 人工 折旧 能源 其他 "
    "cost analysis table cost structure cost composition operating cost breakdown"
)
DEFAULT_INPUT_PATH = "YOUR_PATH_HERE/table_extraction/reports"
DEFAULT_OUTPUT_DIR = "YOUR_PATH_HERE/table_extraction/outputs"
DEFAULT_SEMANTIC_MAX_CHARS = 3000
DEFAULT_MIN_OUTPUT_CONFIDENCE = 0.75


@dataclass
class ExtractionResult:
    report_year: str
    company: str
    source_file: str
    source_page: int
    section_heading: str
    industry: str
    cost_item: str
    current_amount_million_rmb: float | None
    current_ratio_pct: float | None
    prior_amount_million_rmb: float | None
    prior_ratio_pct: float | None
    yoy_change_pct: float | None
    raw_unit: str
    confidence: float


@dataclass
class PageCandidate:
    page_number: int
    keyword_score: float
    keyword_score_norm: float
    semantic_score: float | None
    semantic_score_norm: float | None
    combined_score: float
    text: str


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value))


def visible_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_number(value: object) -> float | None:
    text = visible_text(value).replace(",", "")
    match = NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_year(pdf_path: Path, metadata: dict[str, object], first_page_text: str) -> str:
    for source in [metadata.get("Title", ""), first_page_text, pdf_path.name]:
        match = re.search(r"(20\d{2})\s*年?\s*年度报告", str(source))
        if match:
            return match.group(1)
    match = re.search(r"(20\d{2})", pdf_path.name)
    return match.group(1) if match else ""


def parse_company(metadata: dict[str, object], first_page_text: str) -> str:
    title = str(metadata.get("Title", ""))
    for source in [title, first_page_text]:
        match = re.search(r"([\u4e00-\u9fffA-Za-z0-9（）()]+?)(?:股份有限公司|有限公司).{0,20}?年度报告", source)
        if match:
            return match.group(1).strip() + ("股份有限公司" if "股份有限公司" in source else "")
    first_line = next((line.strip() for line in first_page_text.splitlines() if line.strip()), "")
    return first_line


def pdf_header_warning(pdf_path: Path) -> str | None:
    try:
        header = pdf_path.read_bytes()[:8]
    except OSError as exc:
        return f"Cannot read PDF bytes: {exc}"
    if not header:
        return (
            "PDF file has no readable header bytes. The file may be corrupt, sparse, "
            "or a cloud placeholder that needs to be downloaded/restored."
        )
    if not header.startswith(b"%PDF-"):
        return f"File does not start with a PDF header; first bytes are {header!r}."
    return None


@contextlib.contextmanager
def open_pdf_with_repair(pdf_path: Path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            yield pdf, None
            return
    except Exception as original_exc:
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            raise original_exc

        with tempfile.NamedTemporaryFile(prefix="annual-report-repaired-", suffix=".pdf", delete=False) as handle:
            repaired_path = Path(handle.name)

        try:
            reader = PdfReader(str(pdf_path), strict=False)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            with repaired_path.open("wb") as stream:
                writer.write(stream)
            with pdfplumber.open(repaired_path) as pdf:
                yield pdf, f"pdfplumber opened a pypdf-repaired copy after initial failure: {type(original_exc).__name__}: {original_exc}"
        finally:
            repaired_path.unlink(missing_ok=True)


def page_score(text: str) -> float:
    compact = normalize_text(text)
    score = 0.0
    for pattern in TARGET_HEADING_PATTERNS:
        if pattern in compact:
            score += 6.0 if pattern == "成本分析表" else 2.5
    for pattern in TABLE_HEADER_PATTERNS:
        if pattern in compact:
            score += 2.0
    if "单位" in compact and ("万元" in compact or "人民币" in compact):
        score += 1.0
    if "产销量情况分析表" in compact:
        score += 0.3
    if "主营业务成本" in compact:
        score += 1.5
    return score


class EmbeddingModel:
    def __init__(self, model_name: str, *, local_files_only: bool = False):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed. Install optional embedding dependencies "
                "or run without --require-embeddings."
            ) from exc

        self.model_name = model_name
        self.model = SentenceTransformer(model_name, local_files_only=local_files_only)

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vectors, dtype=float)


def cosine_scores(query_vector: np.ndarray, page_vectors: np.ndarray) -> list[float]:
    if page_vectors.size == 0:
        return []
    return [float(np.dot(query_vector, vector)) for vector in page_vectors]


def minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return [1.0 if high > 0 else 0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def normalize_cosine(values: list[float]) -> list[float]:
    return [max(0.0, min(1.0, (value + 1.0) / 2.0)) for value in values]


def extract_page_texts(pdf: pdfplumber.PDF) -> list[str]:
    return [page.extract_text(x_tolerance=1, y_tolerance=3) or "" for page in pdf.pages]


def semantic_page_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_chars = int(max_chars * 0.75)
    tail_chars = max_chars - head_chars
    return f"{text[:head_chars]}\n{text[-tail_chars:]}"


def find_candidates(
    page_texts: list[str],
    *,
    embedding_model: EmbeddingModel | None,
    semantic_query: str,
    keyword_weight: float,
    semantic_weight: float,
    semantic_max_chars: int,
) -> list[PageCandidate]:
    keyword_scores = [page_score(text) for text in page_texts]
    keyword_norms = minmax(keyword_scores)

    semantic_scores: list[float | None] = [None] * len(page_texts)
    semantic_norms: list[float | None] = [None] * len(page_texts)
    if embedding_model is not None:
        embedding_texts = [semantic_page_text(text, semantic_max_chars) for text in page_texts]
        embeddings = embedding_model.encode([semantic_query, *embedding_texts])
        semantic_raw = cosine_scores(embeddings[0], embeddings[1:])
        semantic_scores = semantic_raw
        semantic_norms = normalize_cosine(semantic_raw)

    candidates: list[PageCandidate] = []
    for index, text in enumerate(page_texts, start=1):
        semantic_norm = semantic_norms[index - 1]
        if semantic_norm is None:
            combined = keyword_norms[index - 1]
        else:
            combined = keyword_weight * keyword_norms[index - 1] + semantic_weight * semantic_norm
        if combined > 0:
            candidates.append(
                PageCandidate(
                    page_number=index,
                    keyword_score=keyword_scores[index - 1],
                    keyword_score_norm=round(keyword_norms[index - 1], 6),
                    semantic_score=None if semantic_scores[index - 1] is None else round(float(semantic_scores[index - 1]), 6),
                    semantic_score_norm=None if semantic_norm is None else round(float(semantic_norm), 6),
                    combined_score=round(float(combined), 6),
                    text=text,
                )
            )
    return sorted(candidates, key=lambda item: item.combined_score, reverse=True)


def detect_unit(page_text: str) -> str:
    compact = normalize_text(page_text)
    if "单位：万元" in compact or "单位:万元" in compact:
        return "万元"
    if "单位：元" in compact or "单位:元" in compact:
        return "元"
    return ""


def find_nearby_table_unit(page_texts: list[str], page_number: int) -> str:
    page_index = page_number - 1
    nearby_indexes = [page_index - 1, page_index, page_index + 1]
    nearby_units = [
        detect_unit(page_texts[index])
        for index in nearby_indexes
        if 0 <= index < len(page_texts)
    ]
    if "万元" in nearby_units:
        return "万元"
    if "元" in nearby_units:
        return "元"
    return ""


def amount_to_million_rmb(amount: float | None, raw_unit: str) -> float | None:
    if amount is None:
        return None
    if raw_unit == "万元":
        return round(amount / 100.0, 6)
    if raw_unit == "元":
        return round(amount / 1_000_000.0, 6)
    return amount


def extract_section_heading(page_text: str) -> str:
    for line in page_text.splitlines():
        compact = normalize_text(line)
        if "成本分析表" in compact:
            return visible_text(line)
    for line in page_text.splitlines():
        compact = normalize_text(line)
        if "成本分析" in compact or "成本构成" in compact:
            return visible_text(line)
    return ""


def table_relevance(table: list[list[object]]) -> float:
    compact = normalize_text(" ".join(visible_text(cell) for row in table for cell in row))
    score = 0.0
    for pattern in TABLE_HEADER_PATTERNS:
        if pattern in compact:
            score += 2.0
    for item in COST_ITEM_KEYWORDS:
        if item in compact:
            score += 1.0
    if "分行业情况" in compact:
        score += 1.0
    return score


def clean_industry(value: str) -> str:
    text = value.replace("\n", "").strip()
    if text in {"分行业", "分行业情况"}:
        return ""
    return text


def find_cost_item(row: list[object]) -> str:
    for cell in row:
        text = normalize_text(cell)
        for keyword in COST_ITEM_KEYWORDS:
            if keyword in text:
                return keyword
    return ""


def row_numbers(row: list[object]) -> list[float]:
    numbers: list[float] = []
    for cell in row:
        text = visible_text(cell)
        if not text:
            continue
        if any(header in normalize_text(text) for header in ["本期金额", "上年同期", "比例", "变动"]):
            continue
        value = parse_number(text)
        if value is not None:
            numbers.append(value)
    return numbers


def extract_rows_from_table(
    table: list[list[object]],
    *,
    report_year: str,
    company: str,
    source_file: str,
    source_page: int,
    section_heading: str,
    raw_unit: str,
    table_confidence: float,
    initial_industry: str = "",
) -> list[ExtractionResult]:
    results: list[ExtractionResult] = []
    current_industry = initial_industry

    for row in table:
        cells = [visible_text(cell) for cell in row]
        compact_row = normalize_text(" ".join(cells))

        if not compact_row or any(header in compact_row for header in ["成本构成项目", "本期金额", "分行业情况"]):
            continue

        first_cell = clean_industry(cells[0]) if cells else ""
        if first_cell and "行业" in first_cell:
            current_industry = first_cell

        cost_item = find_cost_item(row)
        numbers = row_numbers(row)
        if not cost_item or len(numbers) < 5:
            continue

        current_amount, current_ratio, prior_amount, prior_ratio, yoy_change = numbers[:5]
        confidence = min(0.99, table_confidence + 0.08)
        results.append(
            ExtractionResult(
                report_year=report_year,
                company=company,
                source_file=source_file,
                source_page=source_page,
                section_heading=section_heading,
                industry=current_industry,
                cost_item=cost_item,
                current_amount_million_rmb=amount_to_million_rmb(current_amount, raw_unit),
                current_ratio_pct=current_ratio,
                prior_amount_million_rmb=amount_to_million_rmb(prior_amount, raw_unit),
                prior_ratio_pct=prior_ratio,
                yoy_change_pct=yoy_change,
                raw_unit=raw_unit,
                confidence=round(confidence, 3),
            )
        )

    return results


def dedupe_rows(rows: list[ExtractionResult]) -> list[ExtractionResult]:
    by_key: dict[tuple[str, str, float | None, float | None], ExtractionResult] = {}
    for row in rows:
        key = (row.source_file, row.cost_item, row.current_amount_million_rmb, row.prior_amount_million_rmb)
        previous = by_key.get(key)
        if previous is None or row.confidence > previous.confidence:
            by_key[key] = row
    return list(by_key.values())


def collapse_duplicate_cost_items(rows: list[ExtractionResult]) -> list[ExtractionResult]:
    seen: set[tuple[str, str, str, str]] = set()
    collapsed: list[ExtractionResult] = []
    for row in rows:
        key = (row.source_file, row.section_heading, row.industry, row.cost_item)
        if key in seen:
            continue
        seen.add(key)
        collapsed.append(row)
    return collapsed


def filter_output_rows(rows: Iterable[ExtractionResult], min_confidence: float) -> list[ExtractionResult]:
    return [row for row in rows if row.confidence >= min_confidence]


def has_next_major_section(page_text: str, section_heading: str) -> bool:
    seen_heading = False
    for line in page_text.splitlines():
        compact = normalize_text(line)
        if "成本分析表" in compact or (section_heading and normalize_text(section_heading) in compact):
            seen_heading = True
            continue
        if seen_heading and re.match(r"^\(?\d+\)?[).、．]", compact):
            return True
    return False


def extraction_window(selected_page: int, page_count: int, max_continuation_pages: int) -> list[int]:
    start = selected_page
    end = min(page_count, selected_page + max_continuation_pages)
    return list(range(start, end + 1))


def extract_cost_rows_from_pages(
    pdf: pdfplumber.PDF,
    candidates: list[PageCandidate],
    *,
    page_texts: list[str],
    report_year: str,
    company: str,
    source_file: str,
    max_continuation_pages: int,
) -> tuple[list[ExtractionResult], dict[str, object]]:
    selected = candidates[0]
    raw_unit = find_nearby_table_unit(page_texts, selected.page_number)
    section_heading = extract_section_heading(selected.text)
    rows: list[ExtractionResult] = []
    selected_pages: list[int] = []
    inherited_industry = ""

    candidate_by_page = {candidate.page_number: candidate for candidate in candidates}
    for page_number in extraction_window(selected.page_number, len(pdf.pages), max_continuation_pages):
        page = pdf.pages[page_number - 1]
        page_text = candidate_by_page.get(page_number, PageCandidate(page_number, 0, 0, None, None, 0, page_texts[page_number - 1])).text
        page_unit = raw_unit or detect_unit(page_text)
        page_heading = extract_section_heading(page_text) or section_heading
        page_combined_score = candidate_by_page.get(page_number, selected).combined_score

        page_rows: list[ExtractionResult] = []
        for table in page.extract_tables():
            relevance = table_relevance(table)
            if page_number == selected.page_number and relevance < 3.0:
                continue
            if page_number != selected.page_number and relevance < 1.0:
                continue
            page_rows.extend(
                extract_rows_from_table(
                    table,
                    report_year=report_year,
                    company=company,
                    source_file=source_file,
                    source_page=page_number,
                    section_heading=page_heading,
                    raw_unit=page_unit,
                    table_confidence=min(0.9, 0.5 + page_combined_score / 4 + relevance / 30),
                    initial_industry=inherited_industry,
                )
            )

        if page_rows:
            selected_pages.append(page_number)
            for row in page_rows:
                if row.industry:
                    inherited_industry = row.industry
            rows.extend(page_rows)
        elif page_number > selected.page_number and has_next_major_section(page_text, section_heading):
            break

    rows = dedupe_rows(rows)
    rows.sort(key=lambda row: (row.source_page, COST_ITEM_KEYWORDS.index(row.cost_item) if row.cost_item in COST_ITEM_KEYWORDS else 999))
    rows = collapse_duplicate_cost_items(rows)
    return rows, {
        "selected_page": selected.page_number,
        "extracted_pages": selected_pages,
        "top_page_score": {
            "keyword_score": selected.keyword_score,
            "keyword_score_norm": selected.keyword_score_norm,
            "semantic_score": selected.semantic_score,
            "semantic_score_norm": selected.semantic_score_norm,
            "combined_score": selected.combined_score,
        },
    }


def validate_results(rows: list[ExtractionResult]) -> list[str]:
    warnings: list[str] = []
    if not rows:
        return ["No cost-analysis rows were extracted."]

    items = {row.cost_item for row in rows}
    missing = [item for item in ["主营业务成本", "原材料", "人工", "折旧", "能源", "其他"] if item not in items]
    if missing:
        warnings.append(f"Missing expected cost items: {', '.join(missing)}")

    total = next((row for row in rows if row.cost_item == "主营业务成本"), None)
    components = [row for row in rows if row.cost_item != "主营业务成本"]
    if total and components and total.current_amount_million_rmb is not None:
        component_sum = sum(row.current_amount_million_rmb or 0 for row in components)
        tolerance = max(1.0, total.current_amount_million_rmb * 0.02)
        if abs(component_sum - total.current_amount_million_rmb) > tolerance:
            warnings.append(
                "Current component sum differs from 主营业务成本 by more than 2% "
                f"({component_sum:.2f} vs {total.current_amount_million_rmb:.2f} million RMB)."
            )

    for row in rows:
        if row.raw_unit != "万元":
            warnings.append(f"Unexpected or missing unit on page {row.source_page}: {row.raw_unit or 'unknown'}")
            break
    return warnings


def extract_from_pdf(
    pdf_path: Path,
    *,
    embedding_model: EmbeddingModel | None = None,
    semantic_query: str = DEFAULT_SEMANTIC_QUERY,
    keyword_weight: float = 0.5,
    semantic_weight: float = 0.5,
    max_continuation_pages: int = 2,
    semantic_max_chars: int = DEFAULT_SEMANTIC_MAX_CHARS,
) -> tuple[list[ExtractionResult], dict[str, object]]:
    header_warning = pdf_header_warning(pdf_path)
    if header_warning:
        return [], {"source_file": str(pdf_path), "warnings": [header_warning]}

    with open_pdf_with_repair(pdf_path) as (pdf, repair_warning):
        page_texts = extract_page_texts(pdf)
        first_page_text = page_texts[0] if page_texts else ""
        metadata = pdf.metadata or {}
        report_year = parse_year(pdf_path, metadata, first_page_text)
        company = parse_company(metadata, first_page_text)
        candidates = find_candidates(
            page_texts,
            embedding_model=embedding_model,
            semantic_query=semantic_query,
            keyword_weight=keyword_weight,
            semantic_weight=semantic_weight,
            semantic_max_chars=semantic_max_chars,
        )
        if not candidates:
            return [], {"source_file": str(pdf_path), "warnings": ["No candidate pages found."]}

        rows, extraction_info = extract_cost_rows_from_pages(
            pdf,
            candidates,
            page_texts=page_texts,
            report_year=report_year,
            company=company,
            source_file=str(pdf_path),
            max_continuation_pages=max_continuation_pages,
        )

        if not rows:
            return [], {
                "source_file": str(pdf_path),
                "candidate_pages": [asdict(candidate) | {"text": candidate.text[:500]} for candidate in candidates[:5]],
                "warnings": ["Candidate pages found, but no matching cost-analysis table could be parsed."],
            }

        warnings = validate_results(rows)
        if repair_warning:
            warnings.append(repair_warning)
        return rows, {
            "source_file": str(pdf_path),
            **extraction_info,
            "candidate_pages": [
                {
                    "page_number": item.page_number,
                    "keyword_score": item.keyword_score,
                    "keyword_score_norm": item.keyword_score_norm,
                    "semantic_score": item.semantic_score,
                    "semantic_score_norm": item.semantic_score_norm,
                    "combined_score": item.combined_score,
                }
                for item in candidates[:5]
            ],
            "warnings": warnings,
        }


def write_csv(rows: Iterable[ExtractionResult], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row_dicts = [asdict(row) for row in rows]
    fieldnames = [field.name for field in ExtractionResult.__dataclass_fields__.values()]
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(row_dicts)


def write_json(data: object, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_pdfs(input_path: Path) -> list[Path]:
    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        return [input_path]
    return sorted(input_path.glob("*.pdf"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract cost-analysis tables from Chinese annual reports.")
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH, help="PDF file or directory containing PDFs.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for CSV and diagnostics output.")
    parser.add_argument("--combined-name", default="cost_analysis_all_years.csv", help="Combined CSV filename.")
    parser.add_argument("--embedding-model", default="paraphrase-multilingual-MiniLM-L12-v2", help="Sentence-transformers model for semantic page retrieval.")
    parser.add_argument("--disable-embeddings", action="store_true", help="Use keyword-only page retrieval.")
    parser.add_argument("--require-embeddings", action="store_true", help="Fail instead of falling back when the embedding model is unavailable.")
    parser.add_argument("--offline-embeddings", action="store_true", help="Load embedding model from local cache only; do not contact Hugging Face.")
    parser.add_argument("--keyword-weight", type=float, default=0.5, help="Weight for normalized keyword score.")
    parser.add_argument("--semantic-weight", type=float, default=0.5, help="Weight for normalized semantic cosine score.")
    parser.add_argument("--semantic-max-chars", type=int, default=DEFAULT_SEMANTIC_MAX_CHARS, help="Maximum characters per page used for semantic embedding retrieval.")
    parser.add_argument("--max-continuation-pages", type=int, default=2, help="Number of pages after the top page to scan for split-table continuations.")
    parser.add_argument("--min-output-confidence", type=float, default=DEFAULT_MIN_OUTPUT_CONFIDENCE, help="Minimum confidence required for rows written to CSV files.")
    args = parser.parse_args()

    if args.keyword_weight < 0 or args.semantic_weight < 0:
        raise SystemExit("Weights must be non-negative.")
    weight_sum = args.keyword_weight + args.semantic_weight
    if weight_sum <= 0:
        raise SystemExit("At least one retrieval weight must be positive.")
    keyword_weight = args.keyword_weight / weight_sum
    semantic_weight = args.semantic_weight / weight_sum

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    pdfs = iter_pdfs(input_path)
    if not pdfs:
        raise SystemExit(f"No PDF files found at {input_path}")

    embedding_model: EmbeddingModel | None = None
    embedding_status = "disabled"
    if args.disable_embeddings:
        semantic_weight = 0.0
        keyword_weight = 1.0
    else:
        try:
            embedding_model = EmbeddingModel(args.embedding_model, local_files_only=args.offline_embeddings)
            embedding_status = f"enabled:{args.embedding_model}"
        except RuntimeError as exc:
            if args.require_embeddings:
                raise SystemExit(str(exc)) from exc
            embedding_status = f"unavailable:{exc}"
            semantic_weight = 0.0
            keyword_weight = 1.0

    all_rows: list[ExtractionResult] = []
    diagnostics: list[dict[str, object]] = []
    print(f"Processing {len(pdfs)} PDF file(s) from {input_path}", flush=True)
    for index, pdf_path in enumerate(pdfs, start=1):
        print(f"[{index}/{len(pdfs)}] Processing {pdf_path.name}...", flush=True)
        try:
            rows, diagnostic = extract_from_pdf(
                pdf_path,
                embedding_model=embedding_model,
                keyword_weight=keyword_weight,
                semantic_weight=semantic_weight,
                max_continuation_pages=args.max_continuation_pages,
                semantic_max_chars=args.semantic_max_chars,
            )
        except Exception as exc:
            rows = []
            diagnostic = {
                "source_file": str(pdf_path),
                "warnings": [f"Failed to process PDF: {type(exc).__name__}: {exc}"],
            }
        diagnostic["retrieval"] = {
            "embedding_status": embedding_status,
            "keyword_weight": keyword_weight,
            "semantic_weight": semantic_weight,
            "selection_policy": "top1 combined score, then adjacent-page continuation extraction",
        }
        output_rows = filter_output_rows(rows, args.min_output_confidence)
        diagnostic["output_filter"] = {
            "min_confidence": args.min_output_confidence,
            "raw_rows": len(rows),
            "written_rows": len(output_rows),
            "dropped_low_confidence_rows": len(rows) - len(output_rows),
        }
        diagnostics.append(diagnostic)
        all_rows.extend(output_rows)
        year = output_rows[0].report_year if output_rows else (rows[0].report_year if rows else pdf_path.stem)
        per_report_csv = output_dir / f"cost_analysis_{year}.csv"
        write_csv(output_rows, per_report_csv)
        print(f"{pdf_path.name}: extracted {len(output_rows)} rows -> {per_report_csv}", flush=True)
        for warning in diagnostic.get("warnings", []):
            print(f"  warning: {warning}", flush=True)

    combined_csv = output_dir / args.combined_name
    write_csv(all_rows, combined_csv)
    write_json(diagnostics, output_dir / "diagnostics.json")
    print(f"combined: extracted {len(all_rows)} rows -> {combined_csv}", flush=True)
    print(f"diagnostics -> {output_dir / 'diagnostics.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
