Verizon capture-validated update (revision verizon-sales-cards-fix-6)

Replace api.py, database.py, inspect_verizon.py, and frontend/index.html in the existing project.
The API file is included for a complete patch; the new parser uses its existing lookup endpoints.

The new parser is validated against the three 5G Home cards in your supplied sales-page capture.
It preserves advertised prices, mobile bundle prices, Broadband Facts prices, and discount conditions.
It also shows the service address Verizon resolved, including any apartment/unit. A unit introduced
by the website remains unconfirmed until you confirm that it is the intended home.

To recover and store the existing capture (PowerShell from your project folder):
$latest = Get-ChildItem .\captures -Directory -Filter "verizon-*" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
.\.venv\Scripts\python.exe inspect_verizon.py --address "1393 Lincoln Pl, Brooklyn, NY 11213, USA" --capture-text "$($latest.FullName)\verizon_page_text.txt" --save-db

Use the same address as the original captured lookup. Keep verizon_result.json beside the text.
The recovery path uses no browser and preserves the original capture timestamp.

Start the app:
.\.venv\Scripts\python.exe -m uvicorn api:app --reload
Open http://127.0.0.1:8000/, enter the same full address, and click Load saved result.
For a fresh browser lookup, click Check address.

New and recovered checks are stored in plans.db, in address_checks and address_check_plans.
Additional pricing fields and the resolved address are retained in result_json and exposed by
GET /verizon/results?address=... . The frontend displays those fields.

Tests passed: captured-page extraction (3 cards and all 3 price categories), unit/address mismatch
handling, SQLite persistence and retrieval, Python syntax, and frontend JavaScript syntax.
Live network/browser behavior has not been verified in this environment.
