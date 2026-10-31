#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор PDF-счетов (инвойсов).

Считывает данные из data/*.csv и data/*.json, шаблоны из templates/*.html,
предлагает выбрать файл, шаблон и invoice id, затем генерирует PDF
в каталог output/ через WeasyPrint и открывает его в системной программе.

Подстановка данных в шаблон выполняется через string.Template:
плейсхолдеры вида $invoice_id, ${customer} и т.д. заменяются
значениями колонок/ключей из файла данных.

Зависимости:
    pip install weasyprint

Системная библиотека Pango (нужна WeasyPrint):
    Windows: GTK Runtime — https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer
    macOS:   brew install pango

Кириллица поддерживается шрифтами DejaVu Sans / Roboto (заданы в CSS шаблона,
с fallback на Arial/Helvetica).

pandas необязателен: при его наличии CSV читается быстрее, иначе — модуль csv.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys

from datetime import date
from html import escape
from pathlib import Path
from string import Template

try:
    from weasyprint import HTML
except (ImportError, OSError) as exc:
    sys.stderr.write(
        f"\n[ОШИБКА] Не удалось загрузить WeasyPrint: {exc}\n\n"
        "Установите библиотеку:\n"
        "    pip install weasyprint\n\n"
        "Для работы также нужна системная библиотека Pango:\n"
        "  - Windows: установите GTK Runtime\n"
        "  - macOS:   brew install pango\n"
    )
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"

LINE = "=" * 64

ID_KEY_CANDIDATES = (
    "invoice_id", "invoiceid", "invoice id", "invoice number", "invoice",
    "номер счёта", "номер счета", "номер", "id",
)
CUSTOMER_KEY_CANDIDATES = ("customer", "client", "клиент", "заказчик", "покупатель")


def collect_data_files() -> list[Path]:
    files: list[Path] = []
    if DATA_DIR.is_dir():
        for pattern in ("*.csv", "*.json"):
            files.extend(DATA_DIR.glob(pattern))
    return sorted(files, key=lambda p: p.name.lower())


def collect_templates() -> list[Path]:
    if not TEMPLATES_DIR.is_dir():
        return []
    return sorted(TEMPLATES_DIR.glob("*.html"), key=lambda p: p.name.lower())


FILE_ENCODINGS = ("utf-8-sig", "cp1251", "cp866")


def detect_encoding(path: Path) -> str:
    raw = path.read_bytes()
    try:
        raw.decode(FILE_ENCODINGS[0])
        return FILE_ENCODINGS[0]
    except UnicodeDecodeError:
        pass

    def cyrillic_ratio(enc: str) -> float:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            return -1.0
        non_ascii = [c for c in text if ord(c) > 127]
        if not non_ascii:
            return -1.0
        good = sum("\u0410" <= c <= "\u044F" or c in "Ёё" for c in non_ascii)
        return good / len(non_ascii)

    return max(FILE_ENCODINGS[1:], key=cyrillic_ratio)


def detect_dialect(sample: str):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def load_csv(path: Path) -> list[dict]:
    encoding = detect_encoding(path)
    try:
        import pandas as pd
    except ImportError:
        with path.open(encoding=encoding, newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            dialect = detect_dialect(sample)
            rows = [
                ["" if c is None else str(c).strip() for c in row]
                for row in csv.reader(fh, dialect=dialect)
            ]
    else:
        frame = pd.read_csv(
            path, dtype=str, keep_default_na=False, encoding=encoding,
            header=None, sep=None, engine="python",
        )
        rows = [
            ["" if c is None else str(c).strip() for c in row]
            for row in frame.values.tolist()
        ]

    if looks_like_sheet_invoice(rows):
        return records_from_sheet(rows)

    if not rows:
        return []
    headers = rows[0]
    return [
        {str(headers[i]).strip(): (row[i] if i < len(row) else "")
         for i in range(len(headers)) if str(headers[i]).strip()}
        for row in rows[1:]
    ]


SHEET_TITLE_RE = re.compile(r"^\s*сч[её]т\s*№?\s*:?\s*(\S*)\s*$", re.IGNORECASE)
SHEET_TOTALS_ROW_RE = re.compile(r"^\s*(?:итого|всего|total)\b", re.IGNORECASE)
SHEET_NAME_KEYS = ("товар", "наименование", "описание", "услуга", "работа", "description", "item")
SHEET_PRICE_KEYS = ("цена", "price")
SHEET_QTY_KEYS = ("кол-во", "количество", "qty", "quantity")
SHEET_SUM_KEYS = ("итого", "сумма", "total", "amount")
NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")


def _fmt_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(round(value, 2))


def _sheet_column(header_row: list[str], keys: tuple[str, ...]) -> int | None:
    for i, cell in enumerate(header_row):
        low = cell.strip().lower()
        if any(low.startswith(k) for k in keys):
            return i
    return None


def looks_like_sheet_invoice(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    non_empty = [str(c).strip() for c in rows[0] if str(c).strip()]
    if not non_empty or len(non_empty) > 2:
        return False
    return any(SHEET_TITLE_RE.match(c) for c in non_empty)


def records_from_sheet(rows: list[list[str]]) -> list[dict]:
    title_cells = [str(c).strip() for c in rows[0] if str(c).strip()]
    invoice_id = ""
    for cell in title_cells:
        match = SHEET_TITLE_RE.match(cell)
        if match:
            invoice_id = match.group(1)
            break
    if not invoice_id and len(title_cells) > 1:
        invoice_id = title_cells[1]

    header_row: list[str] | None = None
    for row in rows[1:]:
        cells = [str(c).strip() for c in row]
        if cells and cells[0].lower().startswith(SHEET_NAME_KEYS):
            header_row = cells
            break

    col_name = _sheet_column(header_row, SHEET_NAME_KEYS) if header_row else 0
    col_qty = _sheet_column(header_row, SHEET_QTY_KEYS) if header_row else 2
    col_price = _sheet_column(header_row, SHEET_PRICE_KEYS) if header_row else 1
    col_sum = _sheet_column(header_row, SHEET_SUM_KEYS) if header_row else 3
    start = rows.index(header_row) + 1 if header_row in rows else 1

    items: list[dict] = []
    total = ""
    for row in rows[start:]:
        cells = [str(c).strip() for c in row]
        non_empty = [c for c in cells if c]
        if not non_empty:
            continue
        if SHEET_TOTALS_ROW_RE.match(non_empty[0]):
            numbers = [c for c in non_empty[1:] if NUMBER_RE.match(c)]
            if numbers:
                total = numbers[-1]
            continue

        def cell(idx: int | None) -> str:
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx]

        name = cell(col_name) or non_empty[0]
        qty = cell(col_qty)
        price = cell(col_price)
        row_sum = cell(col_sum)
        if not NUMBER_RE.match(row_sum) and NUMBER_RE.match(qty) and NUMBER_RE.match(price):
            row_sum = _fmt_number(
                float(qty.replace(",", ".")) * float(price.replace(",", "."))
            )
        items.append({"name": name, "qty": qty, "price": price, "sum": row_sum})

    if not total:
        acc = 0.0
        for item in items:
            if NUMBER_RE.match(item["sum"]):
                acc += float(item["sum"].replace(",", "."))
            elif NUMBER_RE.match(item["qty"]) and NUMBER_RE.match(item["price"]):
                acc += float(item["qty"].replace(",", ".")) * float(item["price"].replace(",", "."))
        if items:
            total = _fmt_number(acc)

    return [{
        "invoice_id": invoice_id or "1",
        "date": date.today().strftime("%d.%m.%Y"),
        "customer": "",
        "customer_email": "",
        "description": items[0]["name"] if items else "",
        "quantity": items[0]["qty"] if items else "",
        "unit_price": items[0]["price"] if items else "",
        "total": total,
        "items": items,
    }]


def load_json(path: Path) -> list[dict]:
    with path.open(encoding=detect_encoding(path)) as fh:
        payload = json.load(fh)

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = None
        for value in payload.values():
            if isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
                records = value
                break
        if records is None:
            records = [payload]
    else:
        raise ValueError("ожидался список счетов или объект со списком внутри")

    cleaned = [
        {str(k): (str(v).strip() if v is not None else "")
         for k, v in item.items()}
        for item in records
        if isinstance(item, dict)
    ]
    if not cleaned:
        raise ValueError("в файле нет ни одной записи-словаря")
    return cleaned


def load_records(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv(path)
    if suffix == ".json":
        return load_json(path)
    raise ValueError(f"неподдерживаемый формат файла: {path.name}")


def _find_key(record: dict, candidates: tuple[str, ...]) -> str | None:
    lowered = {str(k).strip().lower(): str(k) for k in record}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def print_menu(title: str, items: list[str]) -> None:
    print()
    print(LINE)
    print(f"  {title}")
    print(LINE)
    for number, label in enumerate(items, start=1):
        print(f"   {number}. {label}")
    print(LINE)


def choose(title: str, items: list[str], prompt: str = "Выберите номер") -> int | None:
    print_menu(title, items)
    while True:
        raw = input(f"{prompt} (1-{len(items)}, q — выход): ").strip().lower()
        if raw in ("q", "й", "quit", "exit"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return int(raw) - 1
        print("  ! Неверный ввод, попробуйте ещё раз.")


def choose_invoice(records: list[dict]) -> tuple[str | None, dict | None]:
    ids: list[str] = []
    labels: list[str] = []
    by_id: dict[str, dict] = {}

    for record in records:
        id_key = _find_key(record, ID_KEY_CANDIDATES)
        if id_key is None:
            continue
        invoice_id = str(record.get(id_key, "")).strip()
        if not invoice_id or invoice_id in by_id:
            continue
        customer_key = _find_key(record, CUSTOMER_KEY_CANDIDATES)
        customer = str(record.get(customer_key, "")).strip() if customer_key else ""
        ids.append(invoice_id)
        labels.append(f"{invoice_id}  —  {customer}" if customer else invoice_id)
        by_id[invoice_id] = record

    if not ids:
        print("\n[ОШИБКА] В данных не найдено поле с номером счёта (invoice id).")
        print(f"  Ожидались колонки: {', '.join(ID_KEY_CANDIDATES)}")
        return None, None

    index = choose(
        f"ДОСТУПНЫЕ СЧЕТА — INVOICE ID ({len(ids)})", labels, "Выберите счёт"
    )
    if index is None:
        return None, None
    return ids[index], by_id[ids[index]]


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip().strip(".")
    return cleaned or "invoice"


def build_items_rows(record: dict) -> str:
    items = record.get("items")
    if not isinstance(items, list) or not items:
        items = [{
            "name": record.get("description", ""),
            "qty": record.get("quantity", ""),
            "price": record.get("unit_price", ""),
            "sum": record.get("total", ""),
        }]
    rows = []
    for item in items:
        cells = (
            str(item.get("name", "")),
            str(item.get("qty", "")),
            str(item.get("price", "")),
            str(item.get("sum", "")),
        )
        row_html = f"<td>{escape(cells[0])}</td>"
        row_html += "".join(f'<td class="num">{escape(c)}</td>' for c in cells[1:])
        rows.append(f"<tr>{row_html}</tr>")
    return "\n".join(rows)


def generate_pdf(template_path: Path, record: dict, invoice_id: str) -> Path:
    template_text = template_path.read_text(encoding=detect_encoding(template_path))
    placeholders = {
        match[0] or match[1]
        for match in re.findall(r"\$\{(\w+)\}|\$(\w+)", template_text)
    }
    data = {name: str(record.get(name) or "—") for name in placeholders}
    data["items_rows"] = build_items_rows(record)
    html_text = Template(template_text).safe_substitute(data)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{sanitize_filename(invoice_id)}.pdf"
    HTML(string=html_text, base_url=str(template_path.parent)).write_pdf(str(out_path))
    return out_path


def open_pdf(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        print(f"  ! Не удалось открыть PDF автоматически: {exc}")


def main() -> None:
    print(LINE)
    print("ГЕНЕРАТОР PDF-СЧЕТОВ".center(len(LINE)))
    print(LINE)
    print(f"Каталог данных:   {DATA_DIR}")
    print(f"Каталог шаблонов: {TEMPLATES_DIR}")
    print(f"Каталог вывода:   {OUTPUT_DIR}")

    data_files = collect_data_files()
    templates = collect_templates()

    if not data_files:
        print(f"\n[ОШИБКА] В каталоге {DATA_DIR} нет файлов *.csv или *.json.")
        return
    if not templates:
        print(f"\n[ОШИБКА] В каталоге {TEMPLATES_DIR} нет шаблонов *.html.")
        return

    csv_count = sum(1 for p in data_files if p.suffix.lower() == ".csv")
    print(f"\nНайдено файлов данных: {len(data_files)} "
          f"(CSV: {csv_count}, JSON: {len(data_files) - csv_count})")
    print(f"Найдено шаблонов:      {len(templates)}")

    file_labels = [
        f"{p.name}   [{p.suffix.lower().lstrip('.').upper()}]" for p in data_files
    ]
    file_index = choose(
        f"ДОСТУПНЫЕ ФАЙЛЫ ДАННЫХ ({len(data_files)})", file_labels, "Выберите файл данных"
    )
    if file_index is None:
        print("\nВыход.")
        return
    data_path = data_files[file_index]

    print(f"\nЧитаю файл: {data_path.name} ...")
    try:
        records = load_records(data_path)
    except Exception as exc:
        print(f"[ОШИБКА] Не удалось разобрать файл: {exc}")
        return
    print(f"Загружено записей: {len(records)}")

    invoice_id, record = choose_invoice(records)
    if invoice_id is None:
        print("\nВыход.")
        return

    template_labels = [p.name for p in templates]
    template_index = choose(
        f"ДОСТУПНЫЕ HTML-ШАБЛОНЫ ({len(templates)})",
        template_labels,
        "Выберите шаблон",
    )
    if template_index is None:
        print("\nВыход.")
        return
    template_path = templates[template_index]

    print(f"\nГенерирую PDF для счёта {invoice_id} ...")
    try:
        out_path = generate_pdf(template_path, record, invoice_id)
    except Exception as exc:
        print(f"[ОШИБКА] Не удалось создать PDF: {exc}")
        return

    print("\nГотово!")
    print(f"  PDF сохранён: {out_path}")
    print("Открываю PDF в системной программе...")
    open_pdf(out_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем.")
        sys.exit(130)
