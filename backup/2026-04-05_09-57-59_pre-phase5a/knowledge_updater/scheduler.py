"""
AVA Knowledge Updater - Phase 2
scheduler.py — Main entry point. Run manually or via cron.

Usage:
    python3 -m knowledge_updater.scheduler
    python3 -m knowledge_updater.scheduler --dry-run
    python3 -m knowledge_updater.scheduler --source "AWS DevOps"
    python3 -m knowledge_updater.scheduler --install-cron
    python3 -m knowledge_updater.scheduler --status
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from knowledge_updater.scraper import BlogScraper
from knowledge_updater.ingestor import KnowledgeIngestor


def setup_logging(log_file: str, verbose: bool = False) -> None:
    log_dir = Path(log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(name)s] %(levelname)s — %(message)s"

    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )


logger = logging.getLogger("ava.scheduler")


def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def save_report(report: dict, report_path: str) -> None:
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if Path(report_path).exists():
        try:
            with open(report_path, "r") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = [existing]
        except Exception:
            existing = []

    existing.append(report)
    existing = existing[-10:]

    with open(report_path, "w") as f:
        json.dump(existing, f, indent=2, default=str)

    logger.info(f"Report saved -> {report_path}")


def install_cron(config: dict) -> None:
    import subprocess

    cron_expr = config["scheduler"]["cron"]
    venv_python = sys.executable
    log_file = config["scheduler"]["log_file"]
    project_root = str(PROJECT_ROOT)

    cron_line = (
        f"{cron_expr} cd {project_root} && "
        f"{venv_python} -m knowledge_updater.scheduler >> {log_file} 2>&1"
    )

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        current = result.stdout if result.returncode == 0 else ""

        if "knowledge_updater.scheduler" in current:
            print("\nCron job already installed:")
            for line in current.splitlines():
                if "knowledge_updater" in line:
                    print(f"  {line}")
            return

        new_crontab = current.rstrip() + f"\n# AVA Knowledge Updater - Phase 2\n{cron_line}\n"

        proc = subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            text=True,
            capture_output=True
        )

        if proc.returncode == 0:
            print(f"\n✓ Cron job installed successfully.")
            print(f"  Schedule: {cron_expr} (Sunday 2:00 AM)")
            print(f"  Log: {log_file}")
            print(f"\n  Verify with: crontab -l")
        else:
            print(f"Failed to install cron: {proc.stderr}")
            print(f"\nAdd manually with: crontab -e")
            print(f"  {cron_line}")

    except FileNotFoundError:
        print(f"\nAdd manually with: crontab -e")
        print(f"  {cron_line}")


def run_update(config: dict, dry_run: bool = False, source_filter: str = None) -> dict:
    logger.info("=" * 60)
    logger.info("AVA Knowledge Updater — Phase 2 Starting")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    if source_filter:
        logger.info(f"Source filter: {source_filter}")
    logger.info("=" * 60)

    if source_filter:
        filtered = [s for s in config["sources"] if s["name"].lower() == source_filter.lower()]
        if not filtered:
            available = [s["name"] for s in config["sources"]]
            logger.error(f"Source '{source_filter}' not found. Available: {available}")
            sys.exit(1)
        config = {**config, "sources": filtered}

    logger.info("STEP 1: Scraping blog sources...")
    scraper = BlogScraper(config)
    articles = scraper.fetch_all()

    if not articles:
        logger.warning("No articles fetched. Check RSS feeds or network.")
        return {"error": "no_articles_fetched", "run_at": datetime.now(timezone.utc).isoformat()}

    logger.info(f"Scraping complete: {len(articles)} articles to ingest")

    logger.info("STEP 2: Ingesting into ChromaDB...")
    ingestor = KnowledgeIngestor(config, dry_run=dry_run)
    report = ingestor.ingest_all(articles)

    logger.info("=" * 60)
    logger.info("KNOWLEDGE UPDATE COMPLETE")
    logger.info(f"  Articles processed:  {report['total_articles']}")
    logger.info(f"  Articles ingested:   {report['articles_ingested']}")
    logger.info(f"  Articles skipped:    {report['articles_skipped']}")
    logger.info(f"  New chunks added:    {report['total_chunks_added']}")
    logger.info(f"  Duplicate chunks:    {report['total_chunks_skipped']}")
    if not dry_run:
        logger.info(f"  Collection size now: {report.get('collection_total_chunks', 'N/A')}")
    logger.info("=" * 60)

    return report


def main():
    parser = argparse.ArgumentParser(
        description="AVA Knowledge Updater — Phase 2"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no ChromaDB writes")
    parser.add_argument("--source", type=str, help="Run single source by name")
    parser.add_argument("--install-cron", action="store_true", help="Install Sunday 2AM cron job")
    parser.add_argument("--status", action="store_true", help="Show last run report")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument(
        "--config",
        type=str,
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to config.yaml"
    )

    args = parser.parse_args()
    config = load_config(args.config)
    setup_logging(config["scheduler"]["log_file"], verbose=args.verbose)

    if args.status:
        report_path = config["scheduler"]["run_report"]
        if not Path(report_path).exists():
            print("No run reports found yet.")
            sys.exit(0)
        with open(report_path) as f:
            reports = json.load(f)
        last = reports[-1] if reports else {}
        print(json.dumps(last, indent=2, default=str))
        sys.exit(0)

    if args.install_cron:
        install_cron(config)
        sys.exit(0)

    dry_run = args.dry_run or config.get("dry_run", False)
    report = run_update(config, dry_run=dry_run, source_filter=args.source)

    if not dry_run:
        save_report(report, config["scheduler"]["run_report"])

    sys.exit(1 if report.get("error") else 0)


if __name__ == "__main__":
    main()
