# PriceGuard

PriceGuard is a desktop helper that keeps an eye on the price of a product and two additional text snippets on any web page. You select the elements once in a live browser (Shift+Click) and the application stores the CSS selectors together with the baseline price. From there it can periodically re-check the values from the GUI or in batch mode.

## Installation

1. **Install system prerequisites**
   * Python 3.9+.
   * A recent Chromium browser (downloaded automatically by Playwright).
2. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   playwright install
   ```
   The `requirements.txt` file contains the minimal set of packages needed to run PriceGuard.

The project keeps all state inside `targets.db` in the working directory, so no extra database setup is required.

## Running PriceGuard

* **GUI mode** (interactive monitoring):
  ```bash
  python priceguard.py
  ```
  Use the GUI to add URLs, select the price, and manage monitored entries.

* **Batch mode** (headless check with optional email summary):
  ```bash
  python priceguard.py --batch
  ```
  The command returns exit code `1` when a price drop, an error, or a newly found bonus text occurs; otherwise it exits with `0`.

## Configuration and environment variables

Email notifications are only available in batch mode. Configure them through environment variables or a `.env` file in the working directory:

| Variable | Description |
| --- | --- |
| `SMTP_HOST` | SMTP server hostname. |
| `SMTP_PORT` | SMTP port (default `587`). |
| `SMTP_USER` | SMTP username. |
| `SMTP_PASS` | SMTP password. |
| `SMTP_FROM` | Optional custom sender address (defaults to `SMTP_USER`). |
| `SMTP_TO` | Comma-separated list of recipients. |

If any of the required SMTP values are missing the email step is skipped automatically.

## GUI layout and field descriptions

* **URL** – address of the monitored page. Use Shift+Click inside the opened browser window to capture the price element.
* **Description** – optional label for your own reference; editable directly in the table.
* **Add and capture…** – opens a browser window, lets you select the price, stores the CSS selector, and sets the baseline price.
* **Check active** – runs a headless check for every target that has the Active flag enabled. Buttons show a spinner icon while a job is running.

### Table columns

| Column | Meaning |
| --- | --- |
| **ID** | Internal identifier of the monitored target (hidden by default). |
| **URL** | Stored address of the product page. |
| **Description** | Editable text label. |
| **Baseline** | Price recorded during the initial capture. |
| **Last** | Most recent observed price. |
| **Fetched** | Time of the last successful measurement. |
| **Change** | Difference between the last price and the baseline (green for drops, red for increases). Shows "Error" on failures. |
| **Bonus I / Bonus II** | Optional text snippets captured from other parts of the page. If their selector is defined but the text is missing, a flag icon indicates the missing value. |
| **Active** | Checkbox toggling whether the target should be included in automatic checks. |
| **Actions** | "Check" runs a one-off measurement, "Delete" removes the target including its history. |

### Bonus text fields

Clicking on **Bonus I** or **Bonus II** opens a browser window where you can Shift+Click to record another selector (for example "In stock" text or a coupon code). During each measurement the application fetches the text behind the selector and shows it in the table. When a previously empty bonus text becomes available the batch report highlights it as "new".

## How it works

1. **Capture** – The GUI launches Chromium via Playwright so you can Shift+Click the price element. PriceGuard stores the CSS path, the baseline price, and optional bonus selectors.
2. **Headless polling** – During checks a shared headless browser instance loads the page, waits for digits to appear, and uses multiple fallbacks to read text from the captured elements. If the primary price element is missing, the app scans nearby candidates (including JSON-LD price data) and picks the closest match.
3. **Logging** – Each measurement is stored in `checks` (individual observations) and `daily_stats` (one row per day) for plotting the price history chart.
4. **Alerts** – Batch mode gathers price drops, errors, and newly filled bonus fields into an HTML email report.

## Troubleshooting

* Use the GUI’s **Check** button next to a specific row to debug selectors without waiting for a full batch run.
* If a page changes structure, re-capture the price or bonus selector via Shift+Click. The application keeps previously stored history intact.
* When running on a server, make sure the environment has access to the display if you want to use the GUI. Batch mode works headlessly.

### Diagnosing "sources only" answers in web-enabled assistants

If your assistant prints only a **Sources** list and no final sentence, the most common causes are orchestration issues (stream cancellation, duplicate requests, or response assembly bugs), not failed crawling itself.

What to check in logs:

* **Web stage completed** – entries such as `staged_web_search status=finished` and `rerank status=finished returned=...` mean retrieval worked.
* **Firecrawl enrichment completed** – `staged_firecrawl_enrichment status=finished` means enrichment did not crash.
* **Stream interruption** – `stream_upstream status=cancelled` often indicates the first generation stream was interrupted and replaced. In that case, the final answer may contain only partially assembled output (for example, sources without synthesis text).

Practical mitigation:

1. Prevent duplicate in-flight requests for the same conversation/query.
2. Ensure source rendering happens **after** text synthesis is finalized, not before.
3. Harden query rewriting (strip wrappers such as `User message: ...`, preserve key entities, and fallback to original user query when overlap drops too low).
4. Treat empty body text as an upstream failure to fix, not as something to regenerate in the response layer.
