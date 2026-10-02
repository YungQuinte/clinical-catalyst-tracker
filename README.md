# Clinical Catalyst Tracker

A GitHub Pages dashboard for discovering upcoming **industry-sponsored Phase 2 and Phase 3 clinical-trial catalysts**, then automatically checking recent public company disclosures for likely readout guidance.

The project has two layers:

1. **ClinicalTrials.gov discovery** — finds trials with primary-completion dates near today.
2. **Company-guidance verification layer** — maps likely public sponsors to SEC issuers, scans recent SEC filings and optional company IR/news pages for phrases such as “topline data expected”, “results expected”, “readout expected”, and “expects to report data”, and flags candidate matches for human review.

The guidance layer is deliberately conservative: **automatic keyword hits are not treated as confirmed catalyst dates**. You open the source, verify that the text refers to the exact program/trial, and only then add the confirmed timing to `manual_catalysts.csv`.

---

## Project structure

```text
clinical-catalyst-tracker/
├── .github/
│   └── workflows/
│       └── update.yml
├── site/
│   ├── .nojekyll
│   ├── index.html
│   └── trials.json
├── company_map.csv
├── manual_catalysts.csv
├── requirements.txt
├── update_trials.py
├── verify_guidance.py
├── .gitignore
└── README.md
```

### `update_trials.py`
Downloads relevant ClinicalTrials.gov studies and writes `site/trials.json`.

Automatic screen:
- interventional studies;
- Phase 2 or Phase 3;
- industry lead sponsor;
- primary completion from 60 days ago through 240 days ahead;
- excludes clearly dead statuses such as withdrawn/terminated.

### `verify_guidance.py`
Runs after `update_trials.py` and adds automatic guidance-review information to each trial.

It can:
- use SEC's public ticker / CIK associations to map a ClinicalTrials.gov sponsor to a public company;
- use an explicit override in `company_map.csv` when automatic matching is unreliable;
- inspect recent SEC 8-K, 10-Q, 10-K, 6-K, 20-F, and 40-F filings;
- automatically use the company's `investorWebsite`/`website` metadata from the SEC submissions record when available, and crawl that IR/news site;
- use `company_map.csv` as an override when the automatic IR URL is missing or wrong;
- search for clinical readout language;
- extract surrounding snippets and timing language such as `Q1 2027`, `H2 2027`, or `December 2026`;
- score a hit `HIGH`, `MEDIUM`, or `LOW` depending on whether the snippet appears to mention this trial's drug/program or disease;
- flag matching trials as `Needs guidance review` on the dashboard.

It **does not automatically promote a keyword hit to a verified readout date**.

### `manual_catalysts.csv`
Human-confirmed readout timing.

Columns:

| Column | Meaning |
|---|---|
| `nct` | ClinicalTrials.gov NCT ID |
| `ticker` | Stock ticker |
| `company` | Public company name |
| `expected_readout` | Human-readable company guidance, e.g. `Q1 2027` |
| `expected_readout_date` | Optional approximate ISO sorting date, e.g. `2027-02-15` |
| `readout_confidence` | e.g. `HIGH`, `MEDIUM`, `LOW` |
| `source_url` | The source you personally verified |
| `notes` | Short notes |

Example:

```csv
nct,ticker,company,expected_readout,expected_readout_date,readout_confidence,source_url,notes
NCT12345678,ABCD,ABC Therapeutics,Q1 2027,2027-02-15,HIGH,https://example.com/investors,Phase 3 topline guided for Q1 2027
```

Do **not** use the fake NCT number above in the real file.

### `company_map.csv`
Optional overrides for sponsor → public-company mapping and optional IR-page scanning.

Columns:

| Column | Meaning |
|---|---|
| `sponsor` | Exact lead-sponsor name shown by ClinicalTrials.gov |
| `ticker` | Public ticker |
| `company` | Public company name |
| `cik` | SEC CIK; may be left blank if ticker can be resolved automatically |
| `ir_url` | Investor-relations/news landing page to scan |

Example:

```csv
sponsor,ticker,company,cik,ir_url
ABC Therapeutics,ABCD,ABC Therapeutics Inc,1234567,https://investors.example.com/news-events
```

This file is most useful when:
- the ClinicalTrials.gov sponsor is a subsidiary;
- the sponsor's spelling differs from the SEC issuer name;
- a company is not reliably matched automatically;
- you want the scanner to crawl the company's IR/news site in addition to SEC filings.

### `site/index.html`
Dashboard frontend.

### `site/trials.json`
Generated data. Do not normally edit it by hand.

### `.github/workflows/update.yml`
Runs the tracker daily and deploys GitHub Pages.

---

# First-time GitHub setup

## 1. Upload the repository

Upload the project while preserving the exact paths. In particular:

```text
.github/workflows/update.yml
site/index.html
site/trials.json
```

must remain in those folders.

## 2. Give Actions write permission

Open:

**Settings → Actions → General → Workflow permissions**

Select:

**Read and write permissions**

and save.

The workflow needs this permission to commit refreshed `site/trials.json` back to the repository.

## 3. Enable GitHub Pages

Open:

**Settings → Pages**

Under **Build and deployment → Source**, select:

**GitHub Actions**

## 4. Add the SEC User-Agent variable

The SEC asks automated clients to identify themselves with a declared User-Agent. Do not put your email directly in a public workflow file.

Open:

**Settings → Secrets and variables → Actions → Variables**

Click:

**New repository variable**

Create:

```text
Name: SEC_USER_AGENT
```

For the value, use something like:

```text
ClinicalCatalystTracker your-real-email@example.com
```

This is passed to SEC requests by the workflow.

If this variable is missing, the tracker still updates ClinicalTrials.gov data, but the SEC guidance scan is disabled. IR scans still work for companies that have an explicit `ir_url` in `company_map.csv`.

## 5. Run the workflow manually once

Open:

**Actions → Update Clinical Catalyst Dashboard → Run workflow**

The workflow performs:

```text
ClinicalTrials.gov discovery
        ↓
site/trials.json
        ↓
SEC/public-company mapping
        ↓
recent SEC filing scan
        ↓
optional IR/news-page scan
        ↓
guidance phrase extraction
        ↓
HIGH / MEDIUM / LOW relevance scoring
        ↓
review flags added to site/trials.json
        ↓
GitHub Pages deployment
```

## 6. Open the site

The Pages URL is usually:

```text
https://YOUR-USERNAME.github.io/clinical-catalyst-tracker/
```

---

# How to use the new guidance-review system

The dashboard now has a **Guidance flags to review** counter and a filter:

**Needs guidance review**

When the scanner finds a likely disclosure, the row shows an orange badge such as:

```text
REVIEW 2
```

Expand **Review matches**.

Each match shows:
- source type (`SEC 8-K`, `SEC 10-Q`, `Company release / IR`, etc.);
- filing date where available;
- matched phrase;
- timing text it noticed;
- program/drug terms that matched the trial;
- surrounding source text;
- a direct link to the source.

### Relevance labels

**HIGH**
- the snippet contains a drug/program term from the trial, such as `ABC-123`;
- most useful for review.

**MEDIUM**
- the snippet matches the disease/indication but not a clear program identifier;
- verify carefully.

**LOW**
- company-level guidance phrase with no strong trial-specific term;
- may refer to another program.

These labels are **not investment confidence scores**. They only measure how strongly the text appears linked to that specific ClinicalTrials.gov row.

---

# Confirming a flagged match

Suppose the dashboard shows:

```text
Ticker: ABCD
Drug: ABC-123
Auto guidance scan: REVIEW 2
```

and a source snippet says:

```text
The company expects to report topline data from the Phase 3 ABC-123 study in Q1 2027.
```

Do the following:

1. Click the source.
2. Confirm that the text is current and actually refers to the exact program/trial.
3. Open `manual_catalysts.csv`.
4. Add a row such as:

```csv
NCT12345678,ABCD,ABC Therapeutics,Q1 2027,2027-02-15,HIGH,https://SOURCE-URL,Company explicitly guides Phase 3 topline to Q1 2027
```

5. Commit the change.
6. The workflow reruns automatically.
7. The dashboard now shows the green verified readout instead of relying only on the orange review flag.

The approximate `expected_readout_date` is used only for sorting. If management says `Q1 2027`, a middle-of-quarter placeholder such as `2027-02-15` is fine; do not present that approximate date as company guidance.

---

# Improving company mapping

Automatic SEC name matching is deliberately conservative. When a company maps successfully, the scanner also tries to use SEC submissions metadata to discover its investor-relations website automatically. If company mapping or IR discovery fails, add an explicit override to `company_map.csv`.

For example, ClinicalTrials.gov might show a subsidiary:

```text
ABC Research LLC
```

while the public parent is:

```text
ABC Therapeutics Inc. (ABCD)
```

Add:

```csv
ABC Research LLC,ABCD,ABC Therapeutics Inc,1234567,https://investors.abctherapeutics.com/news-events
```

After the next workflow run, trials sponsored by `ABC Research LLC` can use both the parent company's SEC filings and the specified IR page.

---

# Guidance phrases searched

The scanner looks for patterns around language including:

```text
topline data expected
topline results expected
expects to report topline data
readout expected
data readout
clinical data expected
results expected
expects to report data/results
data expected
```

It also extracts nearby timing expressions such as:

```text
Q1 2027
first quarter 2027
H2 2027
second half 2027
December 2026
year-end 2027
```

Generic phrases such as `results expected` require surrounding clinical-trial vocabulary to reduce false positives from ordinary financial-results language.

---

# Scan limits

The default guidance scan is intentionally focused:

```python
SCAN_DAYS_BACK = 45
SCAN_DAYS_FORWARD = 180
SEC_LOOKBACK_DAYS = 210
MAX_SEC_COMPANIES_PER_RUN = 40
MAX_SEC_FILINGS_PER_COMPANY = 8
MAX_IR_LINKS_PER_COMPANY = 12
```

This keeps the daily job manageable and reduces unnecessary SEC requests.

The SEC scanner also rate-limits itself well below the SEC's published request ceiling.

---

# Important limitations

## Primary completion is not a result date

ClinicalTrials.gov discovery remains only the first screening layer.

## Keyword hits are not confirmations

A sentence can refer to:
- another study from the same company;
- an interim rather than topline analysis;
- an old timeline that has since changed;
- an unrelated program;
- a broad corporate target rather than a specific trial.

Always verify the source before promoting it to `manual_catalysts.csv`.

## SEC mapping is not perfect

The SEC itself notes that its ticker/CIK association files are periodically updated and are not guaranteed to be complete. Subsidiaries and non-US issuers are especially likely to need `company_map.csv` overrides.

## IR websites vary

Some IR sites are JavaScript-heavy, block automated requests, or publish PDFs. The lightweight IR crawler may miss those pages. SEC scanning is generally more structurally reliable for U.S. filers.

## This is a discovery system, not an investment recommendation engine

A catalyst appearing in the dashboard means it may deserve research. It does not mean the stock is attractive or that the trial will succeed.

---

# Daily workflow

The scheduled job runs at approximately **06:17 Europe/Brussels time** every day.

To force an immediate refresh:

**Actions → Update Clinical Catalyst Dashboard → Run workflow**

---

# Recommended research workflow

```text
Automatic trial discovery
        ↓
Automatic SEC/IR guidance flags
        ↓
Human source verification
        ↓
manual_catalysts.csv
        ↓
Verified upcoming catalyst
        ↓
Deep clinical + market-pricing analysis
        ↓
Trade / no-trade decision
        ↓
Risk-based position sizing
```
