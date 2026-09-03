# GitHub automation

This folder contains the GitHub Actions workflows that keep the job data and
tenant lists up to date.

## Workflows

### Daily scraping

`workflows/scrape.yml` runs every day at **02:00 UTC**. It:

1. Installs the Python dependencies from `requirements.txt`.
2. Deletes the previous `output/` directory.
3. Runs `python scrap.py` for all configured ATS platforms.
4. Commits the regenerated compressed job chunks in `output/`.

### Monthly health checks

`workflows/health.yml` runs on the **first day of every month at 03:00 UTC**.
It runs the health checker for Ashby, BambooHR, Greenhouse, Lever, Personio,
Recruitee, Workable, and Workday. Confirmed-dead tenants are removed from the
corresponding files in `data/`, and those changes are committed automatically.

Temporary failures such as network errors, rate limits, and most HTTP errors
are kept in the tenant lists rather than removed.

## Running manually

Both workflows include `workflow_dispatch`. To start one manually:

1. Open the repository on GitHub.
2. Open the **Actions** tab.
3. Select **Daily job scraping** or **Monthly tenant health checks**.
4. Select **Run workflow**.

## Required permissions

The workflows use `contents: write` so the GitHub Actions bot can commit the
updated `output/` and `data/` files. The repository's Actions settings must
allow workflows to create and push commits.
