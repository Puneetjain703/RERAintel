import os
import re
import json
import time
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# -----------------------------
# SETTINGS
# -----------------------------

INPUT_CSV = "rajasthan_rera_projects.csv"
OUTPUT_DIR = "rera_project_detail_jsons"

# Paste the x-api-key from your Network tab here
API_KEY = "MySuperSecretApiKey_123"

# Put 3 for testing first. Use None for all projects.
MAX_PROJECTS = None

os.makedirs(OUTPUT_DIR, exist_ok=True)


def safe_filename(value):
    value = str(value or "").strip()
    value = re.sub(r'[\\/*?:"<>|]', "_", value)
    return value[:150]


def get_encrypted_id_column(df):
    """
    Prefer column named EncryptedProjectId.
    If not found, use the last column.
    """
    if "EncryptedProjectId" in df.columns:
        return "EncryptedProjectId"

    return df.columns[-1]


def main():
    df = pd.read_csv(INPUT_CSV)

    encrypted_col = get_encrypted_id_column(df)

    print("Input file:", INPUT_CSV)
    print("Encrypted ID column:", encrypted_col)
    print("Total rows in CSV:", len(df))

    if MAX_PROJECTS:
        df = df.head(MAX_PROJECTS)
        print("Testing only first rows:", MAX_PROJECTS)

    success_rows = []
    failed_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            extra_http_headers={
                "accept": "application/json, text/plain, */*",
                "origin": "https://rera.rajasthan.gov.in",
                "referer": "https://rera.rajasthan.gov.in/",
                "x-api-key": API_KEY,
            }
        )

        page = context.new_page()

        for index, row in df.iterrows():
            encrypted_id = str(row.get(encrypted_col, "")).strip()

            registration_no = row.get("REGISTRATIONNO", "")
            project_name = row.get("ProjectName", "")
            district = row.get("DistrictName", "")
            promoter = row.get("PromoterName", "")

            print(f"\n[{index + 1}/{len(df)}] {registration_no} | {project_name}")

            if not encrypted_id or encrypted_id.lower() == "nan":
                print("Skipped: missing EncryptedProjectId")
                failed_rows.append({
                    "RowNo": index + 1,
                    "REGISTRATIONNO": registration_no,
                    "ProjectName": project_name,
                    "EncryptedProjectId": encrypted_id,
                    "Reason": "Missing EncryptedProjectId",
                })
                continue

            project_page_url = f"https://rera.rajasthan.gov.in/ProjectDetail?id={encrypted_id}"

            try:
                with page.expect_response(
                    lambda response: (
                        "HomeWebsite/ViewProjectWebsite" in response.url
                        and response.status == 200
                    ),
                    timeout=90000
                ) as response_info:
                    page.goto(project_page_url, wait_until="domcontentloaded", timeout=90000)

                response = response_info.value
                detail_api_url = response.url

                print("Captured detail API:", detail_api_url)

                try:
                    detail_json = response.json()
                except Exception:
                    detail_json = json.loads(response.text())

                file_base = safe_filename(
                    f"{registration_no}_{project_name}_{encrypted_id}"
                )

                json_file = os.path.join(OUTPUT_DIR, f"{file_base}.json")

                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(detail_json, f, ensure_ascii=False, indent=2)

                success_rows.append({
                    "RowNo": index + 1,
                    "REGISTRATIONNO": registration_no,
                    "ProjectName": project_name,
                    "PromoterName": promoter,
                    "DistrictName": district,
                    "EncryptedProjectId": encrypted_id,
                    "ProjectDetailPage": project_page_url,
                    "DetailApiUrl": detail_api_url,
                    "JsonFile": json_file,
                    "Status": "Success",
                })

                time.sleep(0.5)

            except PlaywrightTimeoutError:
                print("Failed: detail API not captured")
                failed_rows.append({
                    "RowNo": index + 1,
                    "REGISTRATIONNO": registration_no,
                    "ProjectName": project_name,
                    "PromoterName": promoter,
                    "DistrictName": district,
                    "EncryptedProjectId": encrypted_id,
                    "ProjectDetailPage": project_page_url,
                    "Reason": "Timeout: ViewProjectWebsite API not captured",
                })

            except Exception as e:
                print("Failed:", str(e))
                failed_rows.append({
                    "RowNo": index + 1,
                    "REGISTRATIONNO": registration_no,
                    "ProjectName": project_name,
                    "PromoterName": promoter,
                    "DistrictName": district,
                    "EncryptedProjectId": encrypted_id,
                    "ProjectDetailPage": project_page_url,
                    "Reason": str(e),
                })

        browser.close()

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    success_file = f"rera_project_detail_success_{timestamp}.xlsx"
    failed_file = f"rera_project_detail_failed_{timestamp}.xlsx"

    pd.DataFrame(success_rows).to_excel(success_file, index=False)
    pd.DataFrame(failed_rows).to_excel(failed_file, index=False)

    print("\nDone.")
    print("Successful:", len(success_rows))
    print("Failed:", len(failed_rows))
    print("Success tracker:", success_file)
    print("Failed tracker:", failed_file)
    print("JSON folder:", OUTPUT_DIR)


if __name__ == "__main__":
    main()