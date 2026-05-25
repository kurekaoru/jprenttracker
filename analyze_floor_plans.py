"""
Batch runner: analyze all floor plan images using FloorPlanAgent and cache
results in listings.floor_plan_data (JSON).

Usage:
    python analyze_floor_plans.py [--limit N] [--reanalyze] [--model MODEL] [--delay F]
"""

import argparse, json, logging, os, sqlite3, time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run(limit: int, reanalyze: bool, model: str, delay: float):
    from floor_plan_agent import FloorPlanAgent

    # Load ANTHROPIC_API_KEY from .env if not already set
    if not os.environ.get("ANTHROPIC_API_KEY"):
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY not set"); return

    agent = FloorPlanAgent(model=model)

    db_path = Path(__file__).parent / "jkk_monitor.db"
    con = sqlite3.connect(str(db_path), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row

    # Ensure column exists
    try:
        con.execute("ALTER TABLE listings ADD COLUMN floor_plan_data TEXT")
        con.commit()
        log.info("Added floor_plan_data column")
    except Exception:
        pass

    skip_clause = "" if reanalyze else "AND l.floor_plan_data IS NULL"
    rows = con.execute(
        f"""SELECT li.listing_id, li.local_path, l.name, l.size_m2, l.layout
            FROM listing_images li
            JOIN listings l ON l.id = li.listing_id
            WHERE li.image_type = 'floor_plan'
              AND li.local_path IS NOT NULL
              {skip_clause}
            ORDER BY l.first_seen DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()

    if not rows:
        log.info("Nothing to analyze."); return

    log.info(f"Analyzing {len(rows)} floor plan(s) with {model}…")
    ok = 0
    for i, row in enumerate(rows, 1):
        lid, path, name, size_m2, layout = row
        log.info(f"[{i}/{len(rows)}] {name} | {layout} | {size_m2}m²")
        if not os.path.exists(path):
            log.warning(f"  File not found: {path}"); continue
        try:
            result = agent.analyze(path, total_area_m2=size_m2)
            living = result.living_area_m2
            nrooms = len(result.rooms)
            inferred = f"  inferred: {result.inferred_rooms}" if result.inferred_rooms else ""
            log.info(
                f"  → {nrooms} rooms, living={living}m², "
                f"kitchen_open={result.kitchen_open}, "
                f"WC={result.toilet_count}, UB={result.bathroom_count}{inferred}"
            )
            con.execute(
                "UPDATE listings SET floor_plan_data=? WHERE id=?",
                (json.dumps(result.to_dict(), ensure_ascii=False), lid),
            )
            con.commit()
            ok += 1
        except Exception as e:
            log.error(f"  Failed: {e}")
        if i < len(rows):
            time.sleep(delay)

    log.info(f"Done — {ok}/{len(rows)} analyzed.")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit",     type=int,   default=200)
    ap.add_argument("--reanalyze", action="store_true")
    ap.add_argument("--model",     default="claude-sonnet-4-6")
    ap.add_argument("--delay",     type=float, default=1.0)
    args = ap.parse_args()
    run(args.limit, args.reanalyze, args.model, args.delay)
