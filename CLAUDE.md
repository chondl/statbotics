# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Statbotics is an open-source data analytics platform for the FIRST Robotics Competition (FRC). It computes and serves **EPA (Expected Points Added)** ratings — a team performance metric in point units built on top of Elo — for all FRC teams, years, events, and matches from 2002 to present.

## Repository Structure

| Directory | Description | Details |
|-----------|-------------|---------|
| `backend/` | FastAPI server: fetches data from TBA, computes EPA, serves REST + site APIs | See `backend/CLAUDE.md` |
| `frontend/` | Next.js 13 frontend — the active, deployed website | See `frontend/CLAUDE.md` |

## Production runs on the `staging` branch

This fork is deployed as a live mirror at **statbotics.iterativerefinement.com**,
and **what runs in production is the `staging` branch, not `master`.** `master`
tracks the upstream base codebase (minimal fixes); `staging` carries the live
architecture (Postgres/Cloud SQL + DuckDB-over-Parquet, offseason support, the
read-triggered freshness ping) and **all** operational documentation — the deploy
process and Makefile, mirror ops, and how EPA/data refresh.

**For any work touching production or the mirror — deploying, rebuilding data, or
diagnosing live behavior — switch to the `staging` branch and follow its docs
there. Do not deploy from `master`.**

## Working Style

- **Ask before implementing.** When diagnosing a bug or unexpected behavior, ask clarifying questions before proposing or making a fix. Don't assume the most obvious explanation is correct — there may be intentional design reasons or missing context. Investigate first, present findings, then ask what the right fix is.
- **Suggest CLAUDE.md updates proactively.** When exploring the codebase to fix a task, if important architectural patterns, non-obvious behaviors, or gotchas are discovered, suggest adding them to the relevant CLAUDE.md file so they're available in future sessions.

## Season Prep Checklist

When preparing for a new FRC season, update backend first, then frontend. Full file-by-file details are in each subdirectory's CLAUDE.md:

- **Backend** — see `backend/CLAUDE.md` → Season Prep
- **Frontend** — see `frontend/CLAUDE.md` → Season Prep
