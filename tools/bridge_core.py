#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("opencart_stock_bridge")

ENCODINGS = (
    "utf-8-sig",
    "utf-8",
    "cp1253",
    "iso-8859-7",
    "latin-1",
    "cp1252",
)

MODEL_RE = re.compile(r"^[0-9]{6}$")
NBSP = "\u00A0"
NNBSP = "\u202F"


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class BridgeSummary:
    ok: bool
    stock_csv: str
    opencart_csv: str
    output_dir: str
    oc_stock_csv: str
    summary_csv: str
    bridge_log: str

    stock_rows: int
    opencart_rows: int
    output_rows: int

    quantity_changed_count: int
    status_would_change_count: int
    disabled_new_count: int
    enabled_new_count: int
    price_zero_forced_disabled_count: int
    price_zero_forced_disabled_products: list[dict[str, str]]

    ignored_stock_rows_count: int
    ignored_opencart_rows_count: int
    opencart_missing_in_stock_count: int
    stock_not_in_opencart_count: int


def is_atomic_model(value: str) -> bool:
    return bool(MODEL_RE.fullmatch(str(value or "").strip()))


def clean_cell(value: Any) -> str:
    return str(value if value is not None else "").replace(NBSP, " ").replace(NNBSP, " ").strip()


def clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", clean_cell(value))


def parse_number(value: Any) -> float:
    raw = clean_cell(value)

    if not raw:
        return 0.0

    raw = re.sub(r"[^0-9,\.\-+]", "", raw)

    if not raw or raw in {"-", "+", ".", ","}:
        return 0.0

    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    elif "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")

    try:
        return float(raw)
    except ValueError:
        return 0.0


def parse_quantity(value: Any) -> int:
    return max(0, int(round(parse_number(value))))


def parse_status(value: Any) -> int:
    number = int(round(parse_number(value)))
    return 1 if number > 0 else 0


def open_text_auto(path: Path) -> str:
    raw = path.read_bytes()

    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise BridgeError(f"Cannot decode file with supported encodings: {path}")


def sniff_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    sample = "\n".join(lines[:10]) if lines else ""

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t"])
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ","

    return list(csv.reader(io.StringIO(text), dialect=dialect))


def normalize_headers(row: list[str]) -> list[str]:
    return [clean_cell(header).lower() for header in row]


def read_stock_csv(path: Path) -> tuple[dict[str, int], list[list[str]]]:
    text = open_text_auto(path)
    rows = sniff_rows(text)

    if not rows:
        raise BridgeError(f"Stock CSV has no rows: {path}")

    headers = normalize_headers(rows[0])
    required = ["model", "quantity"]
    missing = [header for header in required if header not in headers]

    if missing:
        raise BridgeError(
            f"Stock CSV missing required headers: {missing}. Found: {rows[0]}"
        )

    idx_model = headers.index("model")
    idx_quantity = headers.index("quantity")

    stock_by_model: dict[str, int] = {}
    ignored_rows: list[list[str]] = []
    duplicate_models: list[str] = []

    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) <= max(idx_model, idx_quantity):
            continue

        model = clean_cell(row[idx_model])
        quantity_raw = row[idx_quantity]

        if not model:
            continue

        if not is_atomic_model(model):
            ignored_rows.append([str(row_number), model, clean_cell(quantity_raw), "invalid_or_composite_model"])
            continue

        if model in stock_by_model:
            duplicate_models.append(model)
            continue

        stock_by_model[model] = parse_quantity(quantity_raw)

    if duplicate_models:
        sample = ", ".join(sorted(set(duplicate_models))[:20])
        raise BridgeError(f"Stock CSV contains duplicate models. Sample: {sample}")

    return stock_by_model, ignored_rows


def read_opencart_export(path: Path) -> tuple[dict[str, dict[str, Any]], list[list[str]]]:
    text = open_text_auto(path)
    rows = sniff_rows(text)

    if not rows:
        raise BridgeError(f"OpenCart export has no rows: {path}")

    headers = normalize_headers(rows[0])
    required = ["model", "price", "quantity", "status"]
    missing = [header for header in required if header not in headers]

    if missing:
        raise BridgeError(
            f"OpenCart export missing required headers: {missing}. Found: {rows[0]}"
        )

    idx_model = headers.index("model")
    idx_price = headers.index("price")
    idx_quantity = headers.index("quantity")
    idx_status = headers.index("status")
    idx_name = headers.index("name") if "name" in headers else None

    opencart_by_model: dict[str, dict[str, Any]] = {}
    ignored_rows: list[list[str]] = []
    duplicate_models: list[str] = []

    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) <= max(idx_model, idx_price, idx_quantity, idx_status):
            continue

        model = clean_cell(row[idx_model])

        if not model:
            continue

        name = ""
        if idx_name is not None and idx_name < len(row):
            name = clean_name(row[idx_name])

        if not is_atomic_model(model):
            ignored_rows.append([str(row_number), model, name, "invalid_or_composite_model"])
            continue

        if model in opencart_by_model:
            duplicate_models.append(model)
            continue

        opencart_by_model[model] = {
            "model": model,
            "name": name,
            "price": parse_number(row[idx_price]),
            "quantity": parse_quantity(row[idx_quantity]),
            "status": parse_status(row[idx_status]),
        }

    if duplicate_models:
        sample = ", ".join(sorted(set(duplicate_models))[:20])
        raise BridgeError(f"OpenCart export contains duplicate models. Sample: {sample}")

    return opencart_by_model, ignored_rows


def write_csv(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=",", lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def setup_logger(log_path: Path) -> logging.Handler:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOGGER.addHandler(handler)

    return handler


def run_bridge(
    *,
    stock_csv: Path,
    opencart_csv: Path,
    output_dir: Path,
) -> BridgeSummary:
    output_dir.mkdir(parents=True, exist_ok=True)

    oc_stock_path = output_dir / "oc_stock.csv"
    summary_path = output_dir / "summary.csv"
    bridge_log_path = output_dir / "bridge.log"

    handler = setup_logger(bridge_log_path)

    try:
        LOGGER.info("Starting OpenCart stock bridge")
        LOGGER.info("Stock CSV: %s", stock_csv)
        LOGGER.info("OpenCart export CSV: %s", opencart_csv)
        LOGGER.info("Output dir: %s", output_dir)

        if not stock_csv.exists():
            raise BridgeError(f"Stock CSV not found: {stock_csv}")

        if not opencart_csv.exists():
            raise BridgeError(f"OpenCart export CSV not found: {opencart_csv}")

        stock_by_model, ignored_stock_rows = read_stock_csv(stock_csv)
        opencart_by_model, ignored_opencart_rows = read_opencart_export(opencart_csv)

        LOGGER.info("Stock rows loaded: %d", len(stock_by_model))
        LOGGER.info("OpenCart rows loaded: %d", len(opencart_by_model))
        LOGGER.info("Ignored stock rows: %d", len(ignored_stock_rows))
        LOGGER.info("Ignored OpenCart rows: %d", len(ignored_opencart_rows))

        oc_stock_rows: list[list[Any]] = []
        summary_rows: list[list[Any]] = []

        quantity_changed_count = 0
        status_would_change_count = 0
        disabled_new_count = 0
        enabled_new_count = 0
        price_zero_forced_disabled_count = 0
        price_zero_forced_disabled_products: list[dict[str, str]] = []
        opencart_missing_in_stock_count = 0

        for model in sorted(opencart_by_model):
            oc = opencart_by_model[model]
            stock_qty = stock_by_model.get(model)

            if stock_qty is None:
                opencart_missing_in_stock_count += 1
                continue

            old_qty = int(oc["quantity"])
            old_status = int(oc["status"])
            old_price = float(oc.get("price", 0))
            new_qty = int(stock_qty)

            if old_price <= 0:
                new_status = 0
                price_zero_forced_disabled_count += 1
                price_zero_forced_disabled_products.append(
                    {
                        "model": model,
                        "name": str(oc.get("name", "")),
                    }
                )
            elif new_qty <= 0:
                new_status = 0
            else:
                new_status = 1

            quantity_changed = old_qty != new_qty
            status_would_change = old_status != new_status

            if not quantity_changed and not status_would_change:
                continue

            if quantity_changed:
                quantity_changed_count += 1

            if status_would_change:
                status_would_change_count += 1

            if new_status == 0:
                disabled_new_count += 1
            else:
                enabled_new_count += 1

            oc_stock_rows.append([model, new_qty, new_status])
            summary_rows.append(
                [
                    model,
                    oc.get("name", ""),
                    old_qty,
                    new_qty,
                    old_status,
                    new_status,
                ]
            )

        stock_not_in_opencart_count = len(set(stock_by_model) - set(opencart_by_model))

        write_csv(
            oc_stock_path,
            ["model", "quantity", "status"],
            oc_stock_rows,
        )

        write_csv(
            summary_path,
            ["model", "name", "old_qty", "new_qty", "old_status", "new_status"],
            summary_rows,
        )

        LOGGER.info("Output oc_stock rows: %d", len(oc_stock_rows))
        LOGGER.info("Quantity changed: %d", quantity_changed_count)
        LOGGER.info("Status would change: %d", status_would_change_count)
        LOGGER.info("New disabled count: %d", disabled_new_count)
        LOGGER.info("New enabled count: %d", enabled_new_count)
        LOGGER.info("Price zero forced disabled count: %d", price_zero_forced_disabled_count)
        LOGGER.info("OpenCart models missing in stock: %d", opencart_missing_in_stock_count)
        LOGGER.info("Stock models not in OpenCart: %d", stock_not_in_opencart_count)
        LOGGER.info("Bridge complete")

        return BridgeSummary(
            ok=True,
            stock_csv=str(stock_csv),
            opencart_csv=str(opencart_csv),
            output_dir=str(output_dir),
            oc_stock_csv=str(oc_stock_path),
            summary_csv=str(summary_path),
            bridge_log=str(bridge_log_path),
            stock_rows=len(stock_by_model),
            opencart_rows=len(opencart_by_model),
            output_rows=len(oc_stock_rows),
            quantity_changed_count=quantity_changed_count,
            status_would_change_count=status_would_change_count,
            disabled_new_count=disabled_new_count,
            enabled_new_count=enabled_new_count,
            price_zero_forced_disabled_count=price_zero_forced_disabled_count,
            price_zero_forced_disabled_products=price_zero_forced_disabled_products,
            ignored_stock_rows_count=len(ignored_stock_rows),
            ignored_opencart_rows_count=len(ignored_opencart_rows),
            opencart_missing_in_stock_count=opencart_missing_in_stock_count,
            stock_not_in_opencart_count=stock_not_in_opencart_count,
        )

    finally:
        LOGGER.removeHandler(handler)
        handler.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate OpenCart stock import CSV from Entersoft stock and OpenCart export."
    )

    parser.add_argument(
        "--stock-csv",
        required=True,
        help="Entersoft stock CSV. Required schema: model,quantity",
    )
    parser.add_argument(
        "--opencart-csv",
        required=True,
        help="OpenCart Bridge export CSV. Required columns: model,price,quantity,status. name is optional.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Run output directory.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        summary = run_bridge(
            stock_csv=Path(args.stock_csv).expanduser().resolve(),
            opencart_csv=Path(args.opencart_csv).expanduser().resolve(),
            output_dir=Path(args.output_dir).expanduser().resolve(),
        )

        print(json.dumps(asdict(summary), ensure_ascii=True, indent=2))
        return 0

    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "stock_csv": args.stock_csv,
            "opencart_csv": args.opencart_csv,
            "output_dir": args.output_dir,
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())