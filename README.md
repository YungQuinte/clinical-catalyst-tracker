# Clinical Catalyst Tracker

A small GitHub Pages dashboard that automatically discovers **industry-sponsored Phase 2 and Phase 3 interventional clinical trials** with primary-completion dates near today.

It uses the public **ClinicalTrials.gov API v2**, requires no API key, refreshes once per day using GitHub Actions, and lets you add human-verified company readout guidance through `manual_catalysts.csv`.

## What this tracker is — and is not

The automatic feed is a **candidate generator for investment research**.

A ClinicalTrials.gov **Primary Completion Date** means the date on which primary-outcome data collection is completed. It is **not** necessarily the date a company will announce topline results.

For investable catalysts, verify company guidance in press releases, earnings releases, SEC filings, presentations, or conference announcements and add that information to `manual_catalysts.csv`.

## Files

```text
clinical-catalyst-tracker/
├── .github/
│   └── workflows/
│       └── update.yml
├── site/
│   ├── index.html
│   └── trials.json
├── manual_catalysts.csv
├── requirements.txt
├── update_trials.py
├── .gitignore
└── README.md
```

### `update_trials.py`
Downloads and parses relevant ClinicalTrials.gov records.

Automatic screen:
- interventional studies;
- Phase 2 or Phase 3;
- industry lead sponsor;
- primary completion from 60 days ago through 240 days ahead;
- excludes clearly dead statuses such as withdrawn/terminated.

### `manual_catalysts.csv`
Optional human-verified catalyst information.

Columns:

| Column | Meaning |
|---|---|
| `nct` | ClinicalTrials.gov NCT ID |
| `ticker` | Stock ticker |
| `company` | Public company name |
| `expected_readout` | Human-readable company guidance, e.g. `Q1 2027` |
| `expected_readout_date` | Optional ISO date used as a rough sort date, e.g. `2027-02-15` |
| `readout_confidence` | e.g. `HIGH`, `MEDIUM`, `LOW` |
| `source_url` | Company / SEC / conference source |
| `notes` | Short notes |

Example:

```csv
nct,ticker,company,expected_readout,expected_readout_date,readout_confidence,source_url,notes
NCT12345678,ABCD,ABC Therapeutics,Q1 2027,2027-02-15,HIGH,https://example.com/investors,Phase 3 topline guided for Q1 2027
```

Do **not** use the fake NCT number above in the actual file.

### `site/index.html`
The dashboard.

### `site/trials.json`
Generated data. You normally do not edit this by hand.

### `.github/workflows/update.yml`
Runs the updater every day and deploys the dashboard to GitHub Pages.

---

# First-time setup

1. Create a new GitHub repository, for example `clinical-catalyst-tracker`.
2. Upload the contents of this project, preserving the folder structure.
3. Commit to the `main` branch.
4. Open **Settings → Pages**.
5. Under **Build and deployment → Source**, choose **GitHub Actions**.
6. Open the **Actions** tab.
7. Select **Update Clinical Catalyst Dashboard**.
8. Click **Run workflow → Run workflow**.
9. Wait for the workflow to finish successfully.
10. Return to **Settings → Pages** and use the displayed site URL.

The site URL will usually look like:

`https://YOUR-USERNAME.github.io/clinical-catalyst-tracker/`

# Adding a verified catalyst

1. Find an interesting NCT ID in the dashboard.
2. Verify the company's expected data timing from a primary source.
3. Open `manual_catalysts.csv` on GitHub.
4. Click the pencil/edit icon.
5. Add a new CSV row.
6. Commit the change to `main`.
7. The workflow runs again because `manual_catalysts.csv` changed.
8. The dashboard will show the ticker, company, expected readout, confidence and source.

# Manual refresh

Open:

**Actions → Update Clinical Catalyst Dashboard → Run workflow**

This is useful when you do not want to wait for the next scheduled run.

# Changing the date horizon

Open `update_trials.py` and edit:

```python
DAYS_BACK = 60
DAYS_FORWARD = 240
```

For example, for a full year ahead:

```python
DAYS_FORWARD = 365
```

# Important limitation

This first version does **not** automatically know which sponsor is publicly traded, nor does it automatically scrape every sponsor's investor relations site for guidance. That is deliberate: sponsor names and ticker/company relationships are messy, and actual readout guidance should be verified rather than guessed.

The next logical upgrade is an automatic **company-guidance verification layer** that searches SEC/company releases for phrases such as “topline data expected”, “results expected”, and “data readout”, then flags matches for human review.
