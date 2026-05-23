#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tools.opencart_config import resolve_opencart_config


DEFAULT_HEADLESS = True
DEFAULT_EXPORT_ROUTE = "extension/ka_extensions/csv_product_export/ka_product_export"


class BridgeExportError(RuntimeError):
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
            (candidate / ".git").exists()
            or (candidate / "tools").is_dir()
            or (candidate / ".secrets").is_dir()
            or (candidate / "input").is_dir()
            or (candidate / "exports").is_dir()
            or (candidate / "runs").is_dir()
        ):
            return candidate

    raise BridgeExportError(
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def log_line(log_file: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Windows consoles may use legacy encodings that cannot print symbols like ×.
    # Keep the UTF-8 log complete, but make console output safe.
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
        raise BridgeExportError("Admin login appears to have failed; still on login route.")


def open_export_page(
    page,
    admin_index: str,
    export_route: str,
    timeout_ms: int,
) -> None:
    route = export_route.strip().lstrip("?").lstrip("/")
    if route.startswith("route="):
        export_url = f"{admin_index}?{route}"
    else:
        export_url = f"{admin_index}?route={route}"

    export_url = append_session_token(export_url, page.url)

    page.goto(export_url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_load_state("networkidle", timeout=timeout_ms)

    alert_text = get_alert_text(page)
    if "permission" in alert_text.lower() or "denied" in alert_text.lower():
        raise BridgeExportError(f"Permission error opening export page: {alert_text}")


def select_and_load_profile(page, profile: str, timeout_ms: int) -> dict[str, Any]:
    """
    Select the export profile by scanning all visible <select> elements and choosing
    the one that contains the requested profile label.
    """
    requested = profile.strip()
    requested_lower = requested.lower()

    page.wait_for_load_state("networkidle", timeout=timeout_ms)

    selects = page.locator("select")
    select_count = selects.count()

    if select_count == 0:
        raise BridgeExportError("No <select> elements found on export page.")

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
            inspected.append(
                {
                    "index": i,
                    "error": str(exc),
                }
            )

    if selected_index is None:
        raise BridgeExportError(
            f"Could not find profile {requested!r} in any select. "
            f"Inspected selects: {json.dumps(inspected, ensure_ascii=False)}"
        )

    profile_select = selects.nth(selected_index)

    # Prefer exact label selection.
    try:
        profile_select.select_option(label=requested)
    except Exception:
        # Fallback: find the option value manually.
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
            raise BridgeExportError(f"Could not resolve option value for profile {requested!r}")

        profile_select.select_option(value=matching_value)

    page.wait_for_timeout(500)

    selected_text = profile_select.locator("option:checked").inner_text().strip()

    if selected_text.lower() != requested_lower:
        raise BridgeExportError(
            f"Profile selection failed. Expected {requested!r}, got {selected_text!r}."
        )

    load_selectors = [
        'input[value="Load"]',
        'button:has-text("Load")',
        'a:has-text("Load")',
        'input[value="Load Profile"]',
        'button:has-text("Load Profile")',
        'a:has-text("Load Profile")',
    ]

    loaded = False
    clicked_selector = None

    for selector in load_selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            locator.first.click()
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.wait_for_timeout(1000)
            loaded = True
            clicked_selector = selector
            break

    if not loaded:
        raise BridgeExportError("Profile was selected, but no Load button was found.")

    alert_text = get_alert_text(page)

    return {
        "profile_requested": requested,
        "profile_selected": selected_text,
        "selected_select_index": selected_index,
        "clicked_load_selector": clicked_selector,
        "loaded_by_button": loaded,
        "alert_text": alert_text,
        "url_after_load": page.url,
        "inspected_selects": inspected,
    }


def click_first_visible(page, selectors: list[str], timeout_ms: int) -> str | None:
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()

        if count == 0:
            continue

        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible(timeout=1000):
                    item.click(timeout=timeout_ms)
                    return selector
            except Exception:
                continue

    return None


def trigger_export_download(page, timeout_ms: int) -> tuple[Any, str]:
    """
    Walk through the Karapuz CSV Product Export wizard safely.

    Step 1:
        Click Next after the Bridge profile is loaded.

    Step 2:
        Click Next to start the export.

    Step 3:
        Do NOT click top-right buttons. Wait for export completion,
        then click the middle 'download' link.
    """

    def click_next_once(stage: str) -> str:
        next_selectors = [
            'button:has-text("Next")',
            'a:has-text("Next")',
            'input[value="Next"]',
            'button[type="submit"]:has-text("Next")',
        ]

        for selector in next_selectors:
            locator = page.locator(selector)

            if locator.count() == 0:
                continue

            for index in range(locator.count()):
                item = locator.nth(index)

                try:
                    if not item.is_visible(timeout=1000):
                        continue

                    label = f"{stage}: selector={selector} index={index}"
                    item.click(timeout=timeout_ms)

                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                    except Exception:
                        pass

                    try:
                        page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    except Exception:
                        pass

                    page.wait_for_timeout(1500)
                    return label

                except Exception:
                    continue

        raise BridgeExportError(
            f"Could not find visible Next button during {stage}. "
            f"Current URL: {page.url}. Alert text: {get_alert_text(page)}"
        )

    clicked_step1 = click_next_once("step1_to_step2")
    clicked_step2 = click_next_once("step2_to_step3")

    # We should now be on Step 3. From this point, do NOT click generic buttons.
    # The top-right button may be Stop/Done and must not be clicked.
    page.wait_for_timeout(2000)

    completion_selectors = [
        'text="Export is complete!"',
        'text=Export is complete',
        'text=100.00%',
        'text=100.000%',
    ]

    completed = False
    completion_seen_by = None

    deadline = time.time() + (timeout_ms / 1000)

    while time.time() < deadline:
        alert_text = get_alert_text(page)
        if alert_text:
            lowered = alert_text.lower()
            if "error" in lowered or "permission" in lowered or "not found" in lowered:
                raise BridgeExportError(f"Export wizard alert on Step 3: {alert_text}")

        for selector in completion_selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0 and locator.first.is_visible(timeout=1000):
                    completed = True
                    completion_seen_by = selector
                    break
            except Exception:
                pass

        if completed:
            break

        page.wait_for_timeout(5000)

    if not completed:
        raise BridgeExportError(
            "Timed out waiting for Step 3 export completion. "
            f"Current URL: {page.url}. "
            f"Clicked: {clicked_step1}, {clicked_step2}. "
            f"Alert text: {get_alert_text(page)}"
        )

    # Now wait for the actual download link in the middle of the Step 3 page.
    download_selectors = [
        'a:has-text("download")',
        'a:has-text("Download")',
        'text=download',
        'a[href*="download"]',
        'a[href*=".csv"]',
    ]

    download_locator = None
    download_selector_used = None

    deadline = time.time() + (timeout_ms / 1000)

    while time.time() < deadline:
        for selector in download_selectors:
            locator = page.locator(selector)

            if locator.count() == 0:
                continue

            for index in range(locator.count()):
                item = locator.nth(index)

                try:
                    if item.is_visible(timeout=1000):
                        download_locator = item
                        download_selector_used = f"{selector} index={index}"
                        break
                except Exception:
                    continue

            if download_locator is not None:
                break

        if download_locator is not None:
            break

        page.wait_for_timeout(3000)

    if download_locator is None:
        raise BridgeExportError(
            "Export completed, but no visible download link was found. "
            f"Current URL: {page.url}. Completion seen by: {completion_seen_by}"
        )

    with page.expect_download(timeout=timeout_ms) as download_info:
        download_locator.click(timeout=timeout_ms)

    clicked_label = (
        f"step1={clicked_step1}; "
        f"step2={clicked_step2}; "
        f"completion={completion_seen_by}; "
        f"download={download_selector_used}"
    )

    return download_info.value, clicked_label


def validate_downloaded_csv(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise BridgeExportError(f"Downloaded export file missing: {path}")

    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise BridgeExportError(f"Downloaded export file is empty: {path}")

    raw = path.read_bytes()
    sample = raw[:4096]

    # Basic sanity only. The bridge will perform stricter parsing later.
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1253", "iso-8859-7", "latin-1", "cp1252"):
        try:
            text = sample.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        raise BridgeExportError(f"Could not decode the beginning of the downloaded CSV: {path}")

    first_line = text.splitlines()[0] if text.splitlines() else ""
    lowered = first_line.lower()

    expected_any = ["model", "price", "quantity", "status"]
    found = [col for col in expected_any if col in lowered]

    return {
        "path": str(path),
        "size_bytes": size_bytes,
        "first_line": first_line,
        "found_expected_header_tokens": found,
    }


def resolve_output_file(repo_root: Path, explicit_output_file: str | None) -> Path:
    if explicit_output_file:
        return Path(explicit_output_file).expanduser().resolve()

    env_output = os.environ.get("OPENCART_BRIDGE_EXPORT_FILE")
    if env_output and env_output.strip():
        return Path(env_output).expanduser().resolve()

    return (repo_root / "input" / "opencart_export.csv").resolve()


def resolve_profile(explicit_profile: str | None) -> str:
    candidates = [
        explicit_profile,
        os.environ.get("OPENCART_BRIDGE_EXPORT_PROFILE"),
        os.environ.get("OPENCART_EXPORT_PROFILE"),
        "Bridge",
    ]

    for candidate in candidates:
        if candidate and str(candidate).strip():
            return str(candidate).strip()

    return "Bridge"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the OpenCart Bridge profile CSV through the OpenCart admin export page."
    )

    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--output-file", default=None, help="Default: input/opencart_export.csv")
    parser.add_argument("--profile", default=None, help='Export profile label. Default: "Bridge"')
    parser.add_argument("--export-route", default=DEFAULT_EXPORT_ROUTE)

    parser.add_argument("--store-base", default=None)
    parser.add_argument("--admin-path", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)

    parser.add_argument("--headless", dest="headless", action="store_true", default=DEFAULT_HEADLESS)
    parser.add_argument("--headed", dest="headless", action="store_false")
    parser.add_argument("--slow-mo-ms", type=int, default=0)

    parser.add_argument("--timeout-ms", type=int, default=300000)
    parser.add_argument("--report-file", default=None)
    parser.add_argument("--log-file", default=None)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    repo_root = discover_repo_root(args.repo_root)
    output_file = resolve_output_file(repo_root, args.output_file)
    profile = resolve_profile(args.profile)

    resolved_config = resolve_opencart_config(
        repo_root=repo_root,
        store_base=args.store_base,
        admin_path=args.admin_path,
        username=args.username,
        password=args.password,
        profile=None,
    )

    if not resolved_config["store_base"]:
        raise BridgeExportError(
            "Missing OPENCART_STORE_BASE. Pass --store-base or set it in .secrets/opencart.env."
        )

    if not resolved_config["username"] or not resolved_config["password"]:
        raise BridgeExportError(
            "Missing admin credentials. Pass --username/--password or set "
            "OPENCART_ADMIN_USER and OPENCART_ADMIN_PASS."
        )

    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_file = (
        Path(args.log_file).expanduser().resolve()
        if args.log_file
        else (repo_root / "logs" / f"opencart_bridge_export_{stamp}.log").resolve()
    )
    report_file = (
        Path(args.report_file).expanduser().resolve()
        if args.report_file
        else (repo_root / "logs" / f"opencart_bridge_export_{stamp}.json").resolve()
    )

    admin_index = build_admin_index(resolved_config["store_base"], resolved_config["admin_path"])

    result: dict[str, Any] = {
        "ok": False,
        "profile": profile,
        "admin_index": admin_index,
        "export_route": args.export_route,
        "output_file": str(output_file),
        "log_file": str(log_file),
        "report_file": str(report_file),
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)

    log_line(log_file, "Starting OpenCart Bridge profile export")
    log_line(log_file, f"repo_root={repo_root}")
    log_line(log_file, f"profile={profile}")
    log_line(log_file, f"output_file={output_file}")
    log_line(log_file, f"export_route={args.export_route}")
    log_line(log_file, f"admin_index={admin_index}")
    log_line(log_file, f"headless={args.headless}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slow_mo_ms)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            login(
                page,
                admin_index,
                resolved_config["username"],
                resolved_config["password"],
                args.timeout_ms,
            )
            result["login"] = {"ok": True, "url_after_login": page.url}
            log_line(log_file, "Login OK")

            open_export_page(page, admin_index, args.export_route, args.timeout_ms)
            result["export_page"] = {"ok": True, "url": page.url}
            log_line(log_file, f"Export page opened: {page.url}")

            profile_info = select_and_load_profile(page, profile, args.timeout_ms)
            result["profile_load"] = profile_info
            log_line(log_file, f"Profile loaded: {json.dumps(profile_info, ensure_ascii=False)}")

            download, clicked_selector = trigger_export_download(page, args.timeout_ms)
            suggested_name = download.suggested_filename

            temp_download_path = output_file.with_suffix(output_file.suffix + ".download")
            download.save_as(str(temp_download_path))

            if output_file.exists():
                backup_file = output_file.with_suffix(
                    output_file.suffix + f".previous_{stamp}.bak"
                )
                output_file.replace(backup_file)
                result["previous_output_backup"] = str(backup_file)
                log_line(log_file, f"Previous output backed up: {backup_file}")

            temp_download_path.replace(output_file)

            validation = validate_downloaded_csv(output_file)

            result["download"] = {
                "ok": True,
                "clicked_selector": clicked_selector,
                "suggested_filename": suggested_name,
                "saved_as": str(output_file),
                "validation": validation,
            }

            result["ok"] = True

            log_line(log_file, f"Download OK: {output_file}")
            log_line(log_file, f"Validation: {json.dumps(validation, ensure_ascii=False)}")

            write_json(report_file, result)
            log_line(log_file, f"Report written: {report_file}")

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
    except BridgeExportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)