#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import sync_playwright

from tools.opencart_config import resolve_opencart_config


DEFAULT_HEADLESS = True
DEFAULT_IMPORT_ROUTE = "extension/ka_extensions/csv_product_import/ka_product_import"
MODEL_RE = re.compile(r"^[0-9]{6}$")
SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:user_token|token|password|pass|key|api[_-]?key)=)([^&\"'\s]+)"
)


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class StockImportError(RuntimeError):
    pass


def discover_repo_root(explicit_repo_root: str | None) -> Path:
    candidates: list[Path] = []

    if explicit_repo_root:
        candidates.append(Path(explicit_repo_root).expanduser().resolve())

    env_root = os.environ.get("OPENCART_PIPELINE_REPO_ROOT")
    if env_root:
        candidates.append(Path(env_root).expanduser().resolve())

    def walk_up(start: Path) -> list[Path]:
        return [start, *start.parents]

    candidates.extend(walk_up(Path.cwd().resolve()))
    candidates.extend(walk_up(Path(__file__).resolve().parent))

    seen: set[Path] = set()

    for candidate in candidates:
        if candidate in seen:
            continue

        seen.add(candidate)

        if (
            (candidate / "tools").is_dir()
            or (candidate / ".secrets").is_dir()
            or (candidate / "runs").is_dir()
            or (candidate / "logs").is_dir()
        ):
            return candidate

    raise StockImportError(
        "Could not auto-detect repo root. Pass --repo-root or set OPENCART_PIPELINE_REPO_ROOT."
    )


def normalize_admin_path(admin_path: str) -> str:
    value = (admin_path or "").strip().replace("\\", "/")

    if not value:
        return "/index.php"

    if re.match(r"^[A-Za-z]:/", value):
        parts = [part for part in value.split("/") if part]
        if len(parts) >= 2 and parts[-1].lower() == "index.php":
            return "/" + "/".join(parts[-2:])

    if "://" in value:
        return urlparse(value).path or "/index.php"

    return value if value.startswith("/") else f"/{value}"


def build_admin_index(store_base: str, admin_path: str) -> str:
    normalized_admin_path = normalize_admin_path(admin_path)
    return f"{store_base.rstrip('/')}/{normalized_admin_path.lstrip('/')}"


def append_session_token(target_url: str, current_url: str) -> str:
    user_token = parse_qs(urlparse(current_url).query).get("user_token", [None])[0]

    if not user_token:
        return target_url

    parsed = urlparse(target_url)
    query = parse_qs(parsed.query)
    query["user_token"] = [user_token]

    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def get_alert_text(page) -> str:
    alerts = page.locator(".alert, .alert-danger, .alert-warning, .alert-success")

    try:
        if alerts.count() == 0:
            return ""
        return "\n".join(text.strip() for text in alerts.all_inner_texts() if text.strip())
    except Exception:
        return ""


def log_line(log_file: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        print(line)
    except UnicodeEncodeError:
        safe_line = line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8",
            errors="replace",
        )
        print(safe_line)

    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def redact_report_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_report_value(item) for key, item in value.items()}

    if isinstance(value, list):
        return [redact_report_value(item) for item in value]

    if isinstance(value, tuple):
        return [redact_report_value(item) for item in value]

    if isinstance(value, str):
        return SENSITIVE_QUERY_RE.sub(r"\1***", value)

    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_report_value(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def resolve_stock_csv(repo_root: Path, explicit_csv_file: str | None, config: dict[str, str]) -> Path:
    candidates = [
        explicit_csv_file,
        os.environ.get("OPENCART_STOCK_IMPORT_FILE"),
        config.get("stock_import_file", ""),
        str(repo_root / "runs" / "latest" / "oc_stock.csv"),
    ]

    for candidate in candidates:
        if candidate and str(candidate).strip():
            path = Path(str(candidate)).expanduser().resolve()
            if path.exists() and path.is_file():
                return path

    raise StockImportError(
        "Could not find stock import CSV. Expected default: "
        f"{repo_root / 'runs' / 'latest' / 'oc_stock.csv'}"
    )


def validate_stock_csv(path: Path, *, min_rows: int, max_rows: int | None) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        rows = list(reader)

    if headers != ["model", "quantity", "status"]:
        raise StockImportError(
            f"Invalid oc_stock.csv headers. Expected ['model', 'quantity', 'status'], got {headers}"
        )

    if not rows:
        raise StockImportError(f"oc_stock.csv has no data rows: {path}")

    if len(rows) < min_rows:
        raise StockImportError(f"oc_stock.csv has too few rows: {len(rows)} < {min_rows}")

    if max_rows is not None and len(rows) > max_rows:
        raise StockImportError(f"oc_stock.csv has too many rows: {len(rows)} > {max_rows}")

    seen: set[str] = set()
    zero_count = 0
    quantity_sum = 0
    disabled_count = 0

    for row_index, row in enumerate(rows, start=2):
        model = str(row.get("model", "")).strip()
        quantity_raw = str(row.get("quantity", "")).strip()
        status_raw = str(row.get("status", "")).strip()

        if not MODEL_RE.fullmatch(model):
            raise StockImportError(
                f"Invalid model at row {row_index}: {model!r}. Expected exactly 6 digits."
            )

        if model in seen:
            raise StockImportError(f"Duplicate model in oc_stock.csv: {model}")

        seen.add(model)

        if not re.fullmatch(r"[0-9]+", quantity_raw):
            raise StockImportError(
                f"Invalid quantity at row {row_index}: {quantity_raw!r}. Expected integer >= 0."
            )

        if status_raw not in {"0", "1"}:
            raise StockImportError(
                f"Invalid status at row {row_index}: {status_raw!r}. Expected 0 or 1."
            )

        quantity = int(quantity_raw)

        if quantity == 0:
            zero_count += 1

        if status_raw == "0":
            disabled_count += 1

        quantity_sum += quantity

    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "headers": headers,
        "row_count": len(rows),
        "zero_quantity_count": zero_count,
        "zero_quantity_ratio_percent": round((zero_count / len(rows)) * 100, 2),
        "disabled_count": disabled_count,
        "quantity_sum": quantity_sum,
    }


def login(page, admin_index: str, username: str, password: str, timeout_ms: int) -> None:
    login_url = f"{admin_index}?route=common/login"
    page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)

    user = page.locator('input[name="username"]')
    pwd = page.locator('input[name="password"]')

    user.wait_for(state="visible", timeout=timeout_ms)
    user.fill(username)
    pwd.fill(password)

    page.locator('button[type="submit"], input[type="submit"]').first.click()
    page.wait_for_load_state("networkidle", timeout=timeout_ms)

    if "route=common/login" in page.url:
        raise StockImportError("Admin login appears to have failed; still on login route.")


def open_import_page(page, admin_index: str, import_route: str, timeout_ms: int) -> None:
    route = import_route.strip().lstrip("?").lstrip("/")

    if route.startswith("route="):
        url = f"{admin_index}?{route}"
    else:
        url = f"{admin_index}?route={route}"

    url = append_session_token(url, page.url)

    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_load_state("networkidle", timeout=timeout_ms)

    alert_text = get_alert_text(page)
    lowered = alert_text.lower()

    if "permission" in lowered or "denied" in lowered:
        raise StockImportError(f"Permission error opening import page: {alert_text}")


def select_and_load_profile(page, profile: str, timeout_ms: int) -> dict[str, Any]:
    requested = profile.strip()
    requested_lower = requested.lower()

    page.wait_for_load_state("networkidle", timeout=timeout_ms)

    selects = page.locator("select")
    select_count = selects.count()

    if select_count == 0:
        raise StockImportError("No <select> elements found on import page.")

    inspected: list[dict[str, Any]] = []
    selected_index: int | None = None

    for i in range(select_count):
        select = selects.nth(i)

        try:
            options = select.locator("option").all_inner_texts()
            options_clean = [opt.strip() for opt in options]

            inspected.append(
                {
                    "index": i,
                    "name": select.get_attribute("name"),
                    "id": select.get_attribute("id"),
                    "options": options_clean,
                }
            )

            if any(opt.strip().lower() == requested_lower for opt in options_clean):
                selected_index = i
                break
        except Exception as exc:
            inspected.append({"index": i, "error": str(exc)})

    if selected_index is None:
        raise StockImportError(
            f"Could not find import profile {requested!r}. "
            f"Inspected selects: {json.dumps(inspected, ensure_ascii=False)}"
        )

    profile_select = selects.nth(selected_index)

    try:
        profile_select.select_option(label=requested)
    except Exception:
        options = profile_select.locator("option")
        option_count = options.count()
        matching_value = None

        for j in range(option_count):
            option = options.nth(j)
            text = option.inner_text().strip()

            if text.lower() == requested_lower:
                matching_value = option.get_attribute("value")
                break

        if matching_value is None:
            raise StockImportError(f"Could not resolve option value for import profile {requested!r}")

        profile_select.select_option(value=matching_value)

    page.wait_for_timeout(500)

    selected_text = profile_select.locator("option:checked").inner_text().strip()

    if selected_text.lower() != requested_lower:
        raise StockImportError(
            f"Import profile selection failed. Expected {requested!r}, got {selected_text!r}."
        )

    load_selectors = [
        'input[value="Load"]',
        'button:has-text("Load")',
        'a:has-text("Load")',
        'input[value="Load Profile"]',
        'button:has-text("Load Profile")',
        'a:has-text("Load Profile")',
    ]

    clicked_selector = None

    for selector in load_selectors:
        locator = page.locator(selector)

        if locator.count() > 0:
            locator.first.click()
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.wait_for_timeout(1000)
            clicked_selector = selector
            break

    if clicked_selector is None:
        raise StockImportError("Import profile was selected, but no Load button was found.")

    return {
        "profile_requested": requested,
        "profile_selected": selected_text,
        "selected_select_index": selected_index,
        "clicked_load_selector": clicked_selector,
        "alert_text": get_alert_text(page),
        "url_after_load": page.url,
        "inspected_selects": inspected,
    }


def step1_upload_and_next(page, csv_path: Path, timeout_ms: int) -> None:
    local_radios = [
        'input[type="radio"][value="local"]',
        'input[type="radio"][value="local computer"]',
        'input[type="radio"][value="0"]',
    ]

    for selector in local_radios:
        locator = page.locator(selector)
        if locator.count() > 0:
            try:
                locator.first.check(force=True)
                break
            except Exception:
                pass

    file_inputs = page.locator('input[type="file"]')

    if file_inputs.count() == 0:
        raise StockImportError("No file input found on CSV Product Import Step 1.")

    file_inputs.first.set_input_files(str(csv_path))

    next_button = page.locator(
        'button:has-text("Next"), a:has-text("Next"), input[value="Next"]'
    )

    if next_button.count() == 0:
        raise StockImportError("No Next button found after setting import file.")

    next_button.first.click()
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass

    page.wait_for_timeout(1500)

    alert_text = get_alert_text(page)
    lowered = alert_text.lower()

    if "file not found" in lowered or "maximum upload limit" in lowered:
        raise StockImportError(f"Upload failed: {alert_text}")

    if "step2" not in page.url.lower():
        raise StockImportError(
            f"Expected Step 2 after upload, but current URL is {page.url}. Alert: {alert_text}"
        )


def selected_text(select_locator) -> str:
    try:
        return select_locator.locator("option:checked").inner_text().strip()
    except Exception:
        return ""


def selected_value(select_locator) -> str:
    try:
        return select_locator.input_value().strip()
    except Exception:
        return ""


def normalize_mapping(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def mapping_ok(select_locator, expected: str) -> bool:
    expected_norm = normalize_mapping(expected)
    text_norm = normalize_mapping(selected_text(select_locator))
    value_norm = normalize_mapping(selected_value(select_locator))

    return text_norm == expected_norm or value_norm == expected_norm


def find_mapping_select(page, field_name: str):
    direct_selectors = [
        f'select[name="fields[{field_name}]"]',
        f'select[name="field[{field_name}]"]',
        f'select[name="{field_name}"]',
    ]

    for selector in direct_selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator.first

    row_xpath = (
        "//tr[.//*[contains("
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'"
        f"), '{field_name.lower()}')]]//select"
    )

    fallback = page.locator(f"xpath={row_xpath}")

    if fallback.count() > 0:
        return fallback.first

    raise StockImportError(f"Could not find mapping select for field: {field_name}")


def assert_step2_mapping(page, profile: str, timeout_ms: int) -> dict[str, Any]:
    page.locator("#form-step2").wait_for(state="visible", timeout=timeout_ms)

    profile_name = ""

    profile_input = page.locator('input[name="profile_name"]')
    if profile_input.count() > 0:
        profile_name = profile_input.first.input_value().strip()

    model_select = find_mapping_select(page, "model")
    quantity_select = find_mapping_select(page, "quantity")
    status_select = find_mapping_select(page, "status")

    model_ok = mapping_ok(model_select, "model")
    quantity_ok = mapping_ok(quantity_select, "quantity")
    status_ok = mapping_ok(status_select, "status")

    result = {
        "profile_expected": profile,
        "profile_name": profile_name,
        "profile_ok": not profile_name or profile_name == profile,
        "model_mapping": {
            "selected_text": selected_text(model_select),
            "selected_value": selected_value(model_select),
            "ok": model_ok,
        },
        "quantity_mapping": {
            "selected_text": selected_text(quantity_select),
            "selected_value": selected_value(quantity_select),
            "ok": quantity_ok,
        },
        "status_mapping": {
            "selected_text": selected_text(status_select),
            "selected_value": selected_value(status_select),
            "ok": status_ok,
        },
    }

    if not result["profile_ok"] or not model_ok or not quantity_ok or not status_ok:
        raise StockImportError(
            f"Unexpected Step 2 mapping state: {json.dumps(result, ensure_ascii=False)}"
        )

    return result


def click_step2_next(page, timeout_ms: int) -> None:
    next_button = page.locator(
        'button:has-text("Next"), a:has-text("Next"), input[value="Next"]'
    )

    if next_button.count() == 0:
        raise StockImportError("No Next button found on Step 2.")

    next_button.first.click()
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass

    page.wait_for_timeout(1500)

    if "step3" not in page.url.lower():
        raise StockImportError(
            f"Expected Step 3 after Step 2 Next, but current URL is {page.url}. "
            f"Alert: {get_alert_text(page)}"
        )


def collect_counters(page) -> dict[str, str]:
    counters: dict[str, str] = {}

    labels = [
        "Completion at",
        "Time Passed",
        "Lines Processed",
        "Products Created",
        "Products Updated",
        "Products Deleted",
        "Products Disabled",
        "Categories Created",
    ]

    for label in labels:
        cell = page.locator(f'text="{label}"').first

        if cell.count() == 0:
            continue

        try:
            row = cell.locator("xpath=ancestor::tr[1]")
            tds = row.locator("td").all_inner_texts()

            if len(tds) >= 2:
                counters[label] = tds[1].strip()
        except Exception:
            pass

    return counters


def parse_counter_int(value: str | None) -> int | None:
    if value is None:
        return None

    match = re.search(r"-?\d+", str(value).replace(",", ""))

    if not match:
        return None

    return int(match.group(0))


def monitor_import(page, timeout_ms: int, poll_interval_sec: float, max_wait_sec: int) -> dict[str, Any]:
    page.locator("#import_status").wait_for(state="visible", timeout=timeout_ms)

    started_at = time.time()
    final_status = None
    status_text = ""
    messages_html = ""
    counters: dict[str, str] = {}

    while True:
        elapsed = time.time() - started_at

        if elapsed > max_wait_sec:
            raise StockImportError(f"Timed out waiting for import completion after {max_wait_sec}s")

        try:
            status_text = page.locator("#import_status").inner_text().strip()
        except Exception:
            status_text = ""

        try:
            messages_html = page.locator("#scroll").inner_html()
        except Exception:
            messages_html = ""

        counters = collect_counters(page)

        lowered_status = status_text.lower()
        lowered_messages = messages_html.lower()

        if page.locator("#buttons_completed:visible").count() > 0 or "complete" in lowered_status:
            final_status = "completed"
            break

        if page.locator("#buttons_stopped:visible").count() > 0 or "stopped" in lowered_status:
            final_status = "stopped"
            break

        if "server script error" in lowered_status or "server script error" in lowered_messages:
            final_status = "fatal_error"
            break

        if "fatal import error" in lowered_status or "fatal import error" in lowered_messages:
            final_status = "error"
            break

        page.wait_for_timeout(int(poll_interval_sec * 1000))

    return {
        "final_status": final_status,
        "status_text": status_text,
        "elapsed_sec": round(time.time() - started_at, 2),
        "messages_html": messages_html or "",
        "counters": counters,
    }


def resolve_profile(explicit_profile: str | None, config: dict[str, str]) -> str:
    candidates = [
        explicit_profile,
        os.environ.get("OPENCART_STOCK_IMPORT_PROFILE"),
        config.get("stock_import_profile", ""),
        "stock-only",
    ]

    for candidate in candidates:
        if candidate and str(candidate).strip():
            return str(candidate).strip()

    return "stock-only"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload oc_stock.csv to OpenCart through the stock-only import profile."
    )

    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--csv-file", default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--import-route", default=DEFAULT_IMPORT_ROUTE)

    parser.add_argument("--store-base", default=None)
    parser.add_argument("--admin-path", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)

    parser.add_argument("--headless", dest="headless", action="store_true", default=DEFAULT_HEADLESS)
    parser.add_argument("--headed", dest="headless", action="store_false")
    parser.add_argument("--slow-mo-ms", type=int, default=0)

    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=300000)
    parser.add_argument("--max-wait-sec", type=int, default=900)
    parser.add_argument("--poll-interval-sec", type=float, default=2.0)

    parser.add_argument("--min-rows", type=int, default=1)
    parser.add_argument("--max-rows", type=int, default=None)

    parser.add_argument("--allow-created-products", action="store_true")

    parser.add_argument("--report-file", default=None)
    parser.add_argument("--log-file", default=None)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    repo_root = discover_repo_root(args.repo_root)

    config = resolve_opencart_config(
        repo_root=repo_root,
        store_base=args.store_base,
        admin_path=args.admin_path,
        username=args.username,
        password=args.password,
        profile=None,
    )

    if not config["store_base"]:
        raise StockImportError("Missing OPENCART_STORE_BASE.")

    if not config["username"] or not config["password"]:
        raise StockImportError("Missing OPENCART_ADMIN_USER or OPENCART_ADMIN_PASS.")

    profile = resolve_profile(args.profile, config)
    csv_path = resolve_stock_csv(repo_root, args.csv_file, config)
    contract = validate_stock_csv(csv_path, min_rows=args.min_rows, max_rows=args.max_rows)

    stamp = time.strftime("%Y%m%d_%H%M%S")

    log_file = (
        Path(args.log_file).expanduser().resolve()
        if args.log_file
        else (repo_root / "logs" / f"opencart_stock_import_{stamp}.log").resolve()
    )

    report_file = (
        Path(args.report_file).expanduser().resolve()
        if args.report_file
        else (repo_root / "logs" / f"opencart_stock_import_{stamp}.json").resolve()
    )

    admin_index = build_admin_index(config["store_base"], config["admin_path"])

    result: dict[str, Any] = {
        "ok": False,
        "dry_run": bool(args.dry_run),
        "profile": profile,
        "admin_index": admin_index,
        "import_route": args.import_route,
        "csv_file": str(csv_path),
        "csv_contract": contract,
        "log_file": str(log_file),
        "report_file": str(report_file),
    }

    log_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)

    log_line(log_file, "Starting OpenCart stock import")
    log_line(log_file, f"repo_root={repo_root}")
    log_line(log_file, f"profile={profile}")
    log_line(log_file, f"csv_file={csv_path}")
    log_line(log_file, f"import_route={args.import_route}")
    log_line(log_file, f"admin_index={admin_index}")
    log_line(log_file, f"headless={args.headless}")
    log_line(log_file, f"dry_run={args.dry_run}")
    log_line(log_file, f"csv_contract={json.dumps(contract, ensure_ascii=False)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slow_mo_ms)
        context = browser.new_context()
        page = context.new_page()

        try:
            login(page, admin_index, config["username"], config["password"], args.timeout_ms)
            result["login"] = {"ok": True, "url_after_login": page.url}
            log_line(log_file, "Login OK")

            open_import_page(page, admin_index, args.import_route, args.timeout_ms)
            result["import_page"] = {"ok": True, "url": page.url}
            log_line(log_file, f"Import page opened: {page.url}")

            profile_info = select_and_load_profile(page, profile, args.timeout_ms)
            result["profile_load"] = profile_info
            log_line(log_file, f"Profile loaded: {json.dumps(profile_info, ensure_ascii=False)}")

            step1_upload_and_next(page, csv_path, args.timeout_ms)
            result["step1"] = {"ok": True, "url_after_upload": page.url}
            log_line(log_file, f"CSV uploaded; Step 2 URL: {page.url}")

            step2_info = assert_step2_mapping(page, profile, args.timeout_ms)
            result["step2"] = step2_info
            log_line(log_file, f"Step 2 mapping OK: {json.dumps(step2_info, ensure_ascii=False)}")

            if args.dry_run:
                result["ok"] = True
                result["message"] = "Dry run passed. Stopped on Step 2 before final import."
                write_json(report_file, result)
                log_line(log_file, f"Dry run OK. Report written: {report_file}")
                return 0

            click_step2_next(page, args.timeout_ms)
            result["step3"] = {"ok": True, "url": page.url}
            log_line(log_file, f"Step 3 opened: {page.url}")

            monitor = monitor_import(
                page,
                timeout_ms=args.timeout_ms,
                poll_interval_sec=args.poll_interval_sec,
                max_wait_sec=args.max_wait_sec,
            )

            result["import_result"] = monitor
            log_line(log_file, f"Import monitor: {json.dumps(monitor, ensure_ascii=False)}")

            if monitor["final_status"] != "completed":
                raise StockImportError(f"Import did not complete successfully: {monitor['final_status']}")

            products_created = parse_counter_int(monitor.get("counters", {}).get("Products Created"))

            if products_created is not None and products_created > 0 and not args.allow_created_products:
                raise StockImportError(
                    f"Safety stop: import created {products_created} products. "
                    "Stock-only import should update existing products only."
                )

            result["ok"] = True
            write_json(report_file, result)
            log_line(log_file, f"Import OK. Report written: {report_file}")

            return 0

        except Exception as exc:
            result["ok"] = False
            result["error"] = str(exc)
            write_json(report_file, result)
            log_line(log_file, f"ERROR: {exc}")
            raise

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StockImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
