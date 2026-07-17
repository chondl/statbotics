# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Statbotics is an open-source data analytics platform for the FIRST Robotics Competition (FRC). It computes and serves **EPA (Expected Points Added)** ratings — a team performance metric in point units built on top of Elo — for all FRC teams, years, events, and matches from 2002 to present.

## Repository Structure

| Directory | Description | Details |
|-----------|-------------|---------|
| `backend/` | FastAPI server: fetches data from TBA, computes EPA, serves REST + site APIs | See `backend/CLAUDE.md` |
| `frontend/` | Next.js 13 frontend — the active, deployed website | See `frontend/CLAUDE.md` |

## Deploying & operating the staging mirror

This fork is deployed as a live mirror at **statbotics.iterativerefinement.com**.
Production runs the **`staging`** branch, not `master`. For how and when the mirror
recomputes EPA and re-ingests TBA data (the hourly cron + read-triggered ping),
see [`docs/superpowers/rig/DATA-REFRESH.md`](docs/superpowers/rig/DATA-REFRESH.md).

> **STOP — before ANY deploy or data rebuild, you MUST read
> [`docs/superpowers/rig/deploy/DEPLOY.md`](docs/superpowers/rig/deploy/DEPLOY.md)
> and use the [`Makefile`](docs/superpowers/rig/deploy/Makefile) beside it.** Do
> not hand-roll `gcloud`/Cloud Build commands — the Makefile targets (`make ship`,
> `make reprocess-curr-year`, `make reprocess-year YEAR=…`, `make smoke`, …) are
> the source of truth. Re-deriving these from scratch has burned time before;
> the docs exist so you don't.

- **Typical workflow is autonomous:** build the feature, deploy it, and verify it
  in production **without asking the user for permission**. Shipping to the mirror
  is expected, not an escalation.
- **You own correctness in production.** After deploying, adequately test the
  feature against the live mirror (API + a real browser load where relevant) —
  deploying the revision is not "done"; observing the feature work is.
- **Work a pre-deploy checklist every time.** Create the checklist from
  DEPLOY.md §2 as todos and complete each item, recording evidence, before
  calling a deploy finished.

## Working Style

- **Ask before implementing.** When diagnosing a bug or unexpected behavior, ask clarifying questions before proposing or making a fix. Don't assume the most obvious explanation is correct — there may be intentional design reasons or missing context. Investigate first, present findings, then ask what the right fix is.
- **Suggest CLAUDE.md updates proactively.** When exploring the codebase to fix a task, if important architectural patterns, non-obvious behaviors, or gotchas are discovered, suggest adding them to the relevant CLAUDE.md file so they're available in future sessions.

## Season Prep Checklist

When preparing for a new FRC season, update backend first, then frontend. Full file-by-file details are in each subdirectory's CLAUDE.md:

- **Backend** — see `backend/CLAUDE.md` → Season Prep
- **Frontend** — see `frontend/CLAUDE.md` → Season Prep
