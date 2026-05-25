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

Extract all labeled rooms and spaces. For each room:
1. Label (use text shown in image: LDK, DK, K, L, D, 洋室, 和室, WIC, SIC, UB, WC, MB, バルコニー, etc.)
2. Area in m² — look for numbers like "18.5m²" or tatami count like "6畳" (convert: 1畳 = 1.62m²). Use null if not shown.
3. has_window: true if the room has an exterior wall or window shown, false otherwise. Use null if unclear.

Also determine:
- living_area_m2: the m² area of the main living space (LDK, LD, or L room). Use the largest combined living area.
- kitchen_open: true if the kitchen is open/semi-open to the living/dining area, false if separated by a wall with a door.
- toilet_count: integer number of toilet rooms (WC, トイレ). Count 0 if none visible.
- bathroom_count: integer number of bathing units (UB, 浴室, バスルーム). Count 0 if none visible.

Respond ONLY with valid JSON in exactly this format (no markdown, no explanation):
{
  "rooms": [
    {"label": "LDK", "area_m2": 18.5, "has_window": true},
    {"label": "洋室", "area_m2": 6.5, "has_window": false},
    {"label": "UB", "area_m2": null, "has_window": false}
  ],
  "living_area_m2": 18.5,
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
