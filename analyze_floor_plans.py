"""
Use Claude Haiku vision to analyze floor plan images and cache structured
room data in listings.floor_plan_data (JSON).

Extracts: per-room type + area, living area m², kitchen open/closed,
toilet count, bathroom count.

Usage:
    python analyze_floor_plans.py [--limit N] [--reanalyze]
"""

import argparse, base64, json, logging, os, re, sqlite3, time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

PROMPT = """Analyze this Japanese apartment floor plan image.

IMPORTANT — how these floor plans work:
- Dimension lines in METERS are printed along the edges of each room (e.g. 2.775, 3.150, 4.700).
  These are NOT areas — they are wall lengths.
- Room areas are computed as: width_m × depth_m = area_m².
- Some rooms extend into a storage alcove (物入れ, 押入れ, WIC, SIC, クローゼット).
  Include the storage depth in the room's dimensions ONLY if it is labelled as part of that room
  (i.e. a dotted boundary shared with the room, not a solid wall separating them).
- Some rooms also have an 約X畳 annotation printed inside (e.g. 約5.1畳). Use this as a
  cross-check but do NOT use it as the primary area — read the dimension lines.
  Tatami conversion (fallback only): 1畳 ≈ 1.62 m².

Step-by-step:
1. Identify every distinct labeled space (洋室, 和室, LDK, LD, D, K, キッチン, UB, WC/トイレ,
   洗面所/脱衣室, 廊下, ホール, MB, 物入れ, 押入れ, WIC, SIC, バルコニー, etc.).
2. For each habitable room (not UB/WC/廊下/storage), find the two dimension numbers
   that bound its width and depth. If a room spans multiple segments, sum them
   (e.g. width = 1.500 + 1.575 = 3.075).
3. Compute area_m2 = round(width × depth, 2). Use null only if NO dimension lines
   are visible for that room.
4. has_window: true if the room touches an exterior wall (bold outer boundary), false otherwise.

Also determine:
- living_area_m2: the computed m² of the combined living/dining/kitchen space
  (LDK, LD+K, L+DK, or whichever is the main living area). Exclude separate kitchens
  that are enclosed by solid walls with a door.
- kitchen_open: true if the kitchen shares an open boundary (no solid wall + door)
  with the living/dining space.
- toilet_count: number of WC / トイレ rooms. Count carefully — do not double-count
  UB (bath unit) as a toilet.
- bathroom_count: number of UB / 浴室 (bathing) units.

Respond ONLY with valid JSON in exactly this format (no markdown, no explanation):
{
  "rooms": [
    {"label": "洋室(2)", "area_m2": 10.68, "has_window": true},
    {"label": "リビング・ダイニング", "area_m2": 14.45, "has_window": true},
    {"label": "UB", "area_m2": null, "has_window": false}
  ],
  "living_area_m2": 14.45,
  "kitchen_open": true,
  "toilet_count": 1,
  "bathroom_count": 1
}"""

MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _load_image_b64(path: str):
    ext = Path(path).suffix.lower()
    media_type = MEDIA_TYPES.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return data, media_type


def _extract_json(text: str) -> dict:
    # Claude should return pure JSON, but strip markdown fences if present
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try extracting the first {...} block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


def analyze_one(listing_id: str, image_path: str, client, model: str) -> dict:
    img_b64, media_type = _load_image_b64(image_path)
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    raw = msg.content[0].text
    data = _extract_json(raw)
    data["analyzed_at"] = __import__("datetime").datetime.now().isoformat()
    data["model"] = model
    return data


def run(limit: int, reanalyze: bool, model: str, delay: float):
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Try loading from .env in same dir
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    os.environ["ANTHROPIC_API_KEY"] = api_key
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return

    client = anthropic.Anthropic(api_key=api_key)

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
        log.info("Nothing to analyze.")
        return

    log.info(f"Analyzing {len(rows)} floor plan(s) with {model}…")
    ok = 0
    for i, row in enumerate(rows, 1):
        lid, path, name, size_m2, layout = row
        log.info(f"[{i}/{len(rows)}] {name} | {layout} | {size_m2}m²")
        if not os.path.exists(path):
            log.warning(f"  File not found: {path}")
            continue
        try:
            data = analyze_one(lid, path, client, model)
            living = data.get("living_area_m2")
            nrooms = len(data.get("rooms", []))
            log.info(f"  → {nrooms} rooms, living={living}m², kitchen_open={data.get('kitchen_open')}, "
                     f"WC={data.get('toilet_count')}, UB={data.get('bathroom_count')}")
            con.execute(
                "UPDATE listings SET floor_plan_data=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), lid),
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
    ap.add_argument("--reanalyze", action="store_true", help="Re-analyze listings that already have floor_plan_data")
    ap.add_argument("--model",     default="claude-haiku-4-5-20251001")
    ap.add_argument("--delay",     type=float, default=0.5)
    args = ap.parse_args()
    run(args.limit, args.reanalyze, args.model, args.delay)
