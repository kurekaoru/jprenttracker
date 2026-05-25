"""
FloorPlanAgent — reusable structured extractor for Japanese apartment floor plans.

Two-phase approach:
  Phase 1 (Claude vision): read wall-dimension lines, compute W×D for each room.
  Phase 2 (pixel ratio): for rooms without visible dimension lines Claude also
    outputs a pixel_fraction_of_ld (visual area ratio vs LD). Python multiplies
    that fraction by the calibrated LD area to fill null slots.

Usage:
    agent  = FloorPlanAgent()
    result = agent.analyze("images/abc/123.gif", total_area_m2=58.0)
    print(result)
"""

from __future__ import annotations
import base64, json, logging, os, re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── prompt ────────────────────────────────────────────────────────────────────

SYSTEM = """You are a precise Japanese apartment floor plan analyst.
Your job is to extract every room's area in m² from the image."""

PROMPT = """Analyze this Japanese apartment floor plan image.

=== LAYOUT CONVENTIONS ===
• Dimension lines (numbers in metres, e.g. 2.775, 4.700) are printed ALONG the
  edges of each room — they are wall lengths, NOT areas.
• Compute area_m2 = width_m × depth_m for each room.
  If a room spans multiple bays, sum the segments (e.g. 1.500+1.575 = 3.075).
• Some rooms extend into storage (物入れ, 押入れ, クローゼット). A dashed/dotted
  shared boundary means storage is counted as part of the room; a solid wall
  with a door means it is separate.
• 約X畳 annotations are secondary — use them only as a sanity-check, not as the
  primary area source.  (1畳 ≈ 1.62 m²)

=== EXCLUSIONS ===
• The strip labelled 共用廊下 (shared corridor) at the top is NOT part of the
  apartment. Exclude it entirely — do not measure it or count it as a room.
• Do NOT include the external staircase (階段室) area as part of the apartment
  unless it is clearly inside the unit boundary.

=== PIXEL FRACTION FALLBACK ===
For EVERY room — even those you calculate from dimension lines — output a field
"pixel_fraction_of_ld": your best visual estimate of how large this room is
relative to the リビング・ダイニング (LD/LDK). This fraction is used as a
fallback when area_m2 is null, and as a cross-check when it is known.
  Examples: LD itself → 1.0, WC (~1.2 m²) → ~0.08, UB (~2.5 m²) → ~0.17.

=== ROOMS TO EXTRACT ===
Extract EVERY distinct space inside the apartment boundary, including:
  洋室, 和室, LDK/LD/D/L, キッチン, 玄関/ホール, 廊下,
  WC/トイレ, UB/浴室, 洗面所/脱衣室, 物入れ/押入れ/WIC/SIC/クローゼット, MB,
  バルコニー/ベランダ (outdoor, note as is_outdoor=true)

For each room output:
• label        — the text label shown in the image
• area_m2      — calculated from dimension lines (null if no lines visible)
• dim_calc     — the calculation string, e.g. "2.775 × 3.150" (null if not computed)
• has_window   — true if the room touches the exterior boundary (bold outer wall)
• is_outdoor   — true only for バルコニー/ベランダ (default false)
• pixel_fraction_of_ld — visual area fraction vs LD (always a number, never null)

Also compute these summary fields:
• living_area_m2   — area_m2 of the LD/LDK (the main living space)
• kitchen_open     — true if kitchen shares an open boundary with the living/dining area
• toilet_count     — number of WC/トイレ rooms
• bathroom_count   — number of UB/浴室 units

Respond ONLY with valid JSON (no markdown fences, no explanation):
{
  "rooms": [
    {
      "label": "リビング・ダイニング",
      "area_m2": 14.45,
      "dim_calc": "(1.500+1.575) × 4.700",
      "has_window": true,
      "is_outdoor": false,
      "pixel_fraction_of_ld": 1.0
    },
    {
      "label": "WC",
      "area_m2": null,
      "dim_calc": null,
      "has_window": false,
      "is_outdoor": false,
      "pixel_fraction_of_ld": 0.08
    }
  ],
  "living_area_m2": 14.45,
  "kitchen_open": true,
  "toilet_count": 1,
  "bathroom_count": 1
}"""

MEDIA_TYPES = {
    ".gif": "image/gif", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class Room:
    label: str
    area_m2: Optional[float]          # from dimension lines (Phase 1)
    inferred_area_m2: Optional[float]  # from pixel fraction (Phase 2)
    dim_calc: Optional[str]
    has_window: bool
    is_outdoor: bool
    pixel_fraction_of_ld: float

    @property
    def best_area_m2(self) -> Optional[float]:
        return self.area_m2 if self.area_m2 is not None else self.inferred_area_m2


@dataclass
class FloorPlanResult:
    rooms: list[Room] = field(default_factory=list)
    living_area_m2: Optional[float] = None
    kitchen_open: Optional[bool] = None
    toilet_count: int = 0
    bathroom_count: int = 0
    analyzed_at: str = ""
    model: str = ""
    total_area_m2: Optional[float] = None   # listing's stated area (from DB)
    inferred_rooms: list[str] = field(default_factory=list)  # labels filled by Phase 2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rooms"] = [
            {**asdict(r), "best_area_m2": r.best_area_m2}
            for r in self.rooms
        ]
        return d

    def summary(self) -> str:
        lines = []
        for r in self.rooms:
            a = r.best_area_m2
            src = "" if r.area_m2 is not None else " (inferred)"
            a_str = f"{a:.2f} m²{src}" if a is not None else "—"
            w = "☀" if r.has_window else " "
            lines.append(f"  {w} {r.label:<20} {a_str}  [{r.dim_calc or 'pixel-ratio'}]")
        return "\n".join(lines)


# ── agent ─────────────────────────────────────────────────────────────────────

class FloorPlanAgent:
    """
    Reusable agent for extracting structured room data from Japanese floor plan images.
    """

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        self.model = model
        self._client = None
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def _client_lazy(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    # ── Phase 1: vision call ──────────────────────────────────────────────────

    def _call_vision(self, image_path: str) -> dict:
        ext = Path(image_path).suffix.lower()
        media_type = MEDIA_TYPES.get(ext, "image/jpeg")
        with open(image_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode()

        client = self._client_lazy()
        msg = client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": img_b64
                    }},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                return json.loads(m.group())
            raise

    # ── Phase 2: pixel-ratio fill-in ─────────────────────────────────────────

    @staticmethod
    def _apply_pixel_ratio(rooms: list[Room]) -> list[str]:
        """
        For rooms with area_m2=None, infer area from pixel_fraction_of_ld
        and the LD's known area.  Returns list of room labels that were inferred.
        """
        # Find the best calibration anchor: prefer the LD/LDK with known area
        ld = next(
            (r for r in rooms if r.area_m2 is not None and any(
                kw in r.label for kw in ("LDK", "LD", "リビング", "Living")
            )),
            None,
        )
        if ld is None:
            # Fall back to largest room with known area
            ld = max(
                (r for r in rooms if r.area_m2 is not None),
                key=lambda r: r.area_m2,
                default=None,
            )
        if ld is None or ld.pixel_fraction_of_ld == 0:
            return []

        ld_area = ld.area_m2
        inferred = []
        for r in rooms:
            if r.area_m2 is None and r.pixel_fraction_of_ld > 0:
                r.inferred_area_m2 = round(ld_area * r.pixel_fraction_of_ld, 2)
                inferred.append(r.label)
        return inferred

    # ── public API ────────────────────────────────────────────────────────────

    def analyze(self, image_path: str, total_area_m2: float | None = None) -> FloorPlanResult:
        from datetime import datetime

        raw = self._call_vision(image_path)

        rooms: list[Room] = []
        for rd in raw.get("rooms", []):
            rooms.append(Room(
                label                = rd.get("label", ""),
                area_m2              = _float_or_none(rd.get("area_m2")),
                inferred_area_m2     = None,
                dim_calc             = rd.get("dim_calc"),
                has_window           = bool(rd.get("has_window", False)),
                is_outdoor           = bool(rd.get("is_outdoor", False)),
                pixel_fraction_of_ld = float(rd.get("pixel_fraction_of_ld") or 0),
            ))

        inferred = self._apply_pixel_ratio(rooms)

        result = FloorPlanResult(
            rooms          = rooms,
            living_area_m2 = _float_or_none(raw.get("living_area_m2")),
            kitchen_open   = raw.get("kitchen_open"),
            toilet_count   = int(raw.get("toilet_count") or 0),
            bathroom_count = int(raw.get("bathroom_count") or 0),
            analyzed_at    = datetime.now().isoformat(),
            model          = self.model,
            total_area_m2  = total_area_m2,
            inferred_rooms = inferred,
        )
        return result


# ── helpers ───────────────────────────────────────────────────────────────────

def _float_or_none(v) -> Optional[float]:
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="Path to floor plan image")
    ap.add_argument("--total-area", type=float, default=None, help="Listed apartment area m²")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--json", dest="as_json", action="store_true", help="Output raw JSON")
    args = ap.parse_args()

    if not os.path.exists(args.image):
        print(f"File not found: {args.image}", file=sys.stderr); sys.exit(1)

    agent  = FloorPlanAgent(model=args.model)
    result = agent.analyze(args.image, total_area_m2=args.total_area)

    if args.as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"\n{'─'*55}")
        print(f"  Floor plan: {args.image}")
        if args.total_area:
            indoor = sum(
                (r.best_area_m2 or 0) for r in result.rooms if not r.is_outdoor
            )
            print(f"  Listed: {args.total_area} m²  |  Extracted: {indoor:.1f} m²  |  Gap: {args.total_area - indoor:.1f} m²")
        print(f"  LD area: {result.living_area_m2} m²  |  Kitchen open: {result.kitchen_open}")
        print(f"  WC: {result.toilet_count}  |  Bath: {result.bathroom_count}")
        if result.inferred_rooms:
            print(f"  Pixel-inferred: {', '.join(result.inferred_rooms)}")
        print(f"{'─'*55}")
        print(result.summary())
        print(f"{'─'*55}\n")
