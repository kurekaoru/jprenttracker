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
• Each labeled dimension belongs to EXACTLY ONE space. Dimension labels on the
  same wall are stacked independently — never subtract one from another.
  Example: if the left wall shows 3.700 (和室) then 1.000 (押入れ) below/above it,
  the room is 2.775 × 3.700 and the closet is 2.775 × 1.000 — do NOT compute
  2.775 × (3.700 − 1.000).
• Storage (物入れ, 押入れ, WIC, SIC, クローゼット) with a dashed or open shared
  boundary is measured separately from the room it adjoins.
• ALWAYS prefer dimension-line calculations over 約X畳 tatami annotations.
  Use tatami only when NO dimension lines are visible for that room.
  UR/JKK public housing uses 団地間 (公団間): 1畳 = 1.44 m² — use 1.44, NOT 1.62.

=== BALCONY / VERANDA WIDTH RULE ===
バルコニー/ベランダ almost always spans the FULL building width at the bottom of
the floor plan. Use the TOTAL of all horizontal dimension segments at the bottom
of the floor plan as the balcony width — never just one segment.
Example: if the bottom shows 2.775 + 3.075 with a combined label of 5.850, the
balcony width is 5.850 m, not 2.775 m.

=== IRREGULAR SHAPES AND MISSING DIMENSIONS ===
Some rooms have irregular (L-shaped, T-shaped, or notched) outlines, or one
dimension of a sub-section is not labeled. In those cases:
1. Decompose the shape into simple rectangles.
2. For each rectangle where one dimension IS labeled and the other is NOT:
   estimate the missing dimension by pixel proportion relative to the nearest
   labeled dimension of the same orientation in the image.
   Write the estimate as "~X.XX (px)" in dim_calc, e.g. "(2.775+3.075) × 1.300 + 3.075 × ~0.45(px)".
3. Sum all sub-rectangles to get the total area.

For バルコニー depth when unlabeled: compare the balcony's visible pixel depth
to the nearest labeled vertical dimension (e.g. if envelope height = 7.600 m and
the balcony depth appears to be about 10% of the total plan height, estimate
depth = 7.600 × 0.10 = 0.76 m). Use pixel proportion — do not default to 0.35 m.

=== EXCLUSIONS ===
• The strip labelled 共用廊下 (shared corridor) at the top is NOT part of the
  apartment. Exclude it entirely — do not measure it or count it as a room.
• Do NOT include the external staircase (階段室) area as part of the apartment
  unless it is clearly inside the unit boundary.

=== CALIBRATION AND PIXEL FRACTION ===
Choose the best calibration reference for this specific floor plan:

  CASE A — individual room dimension lines exist for the main rooms:
    Use the LD/LDK area (computed from dimension lines) as the reference.
    "pixel_fraction_of_ld" = this room's visual area ÷ LD visual area.

  CASE B — only overall envelope dimensions are labeled (total width × total height):
    Use the TOTAL INTERIOR ENVELOPE as the reference.
    Compute: envelope_area = total_width × total_height.
    For every room, output "pixel_fraction_of_ld" = this room's visual area ÷
    TOTAL interior floor plan area. Then: room_area ≈ fraction × envelope_area.
    In this case LD's own pixel_fraction_of_ld will be less than 1.0.

For EVERY room output "pixel_fraction_of_ld" (always a number, never null).
This fraction is used as a fallback when area_m2 is null.

=== COMPLETENESS CHECK ===
After listing all rooms, verify:
  sum(all indoor room areas) + sum(outdoor areas) ≈ envelope_area (W × H).
If there is a gap larger than ~5% of the envelope, you have missed a space.
Common missed spaces: 廊下 (hallway connecting rooms), 玄関 (entrance recess),
corridor strips between rooms. Add them with area estimated from pixel fraction.

=== ROOMS TO EXTRACT ===
Extract EVERY distinct space inside the apartment boundary, including:
  洋室, 和室, LDK/LD/D/L, キッチン, 玄関/ホール, 廊下,
  WC/トイレ, UB/浴室, 洗面所/脱衣室, 物入れ/押入れ/WIC/SIC/クローゼット, MB,
  バルコニー/ベランダ (outdoor, note as is_outdoor=true)

For each room output:
• label        — the text label shown in the image
• area_m2      — calculated from dimension lines (null if no lines visible)
• dim_calc     — full calculation string including any pixel-estimated segments,
                 e.g. "(2.775+3.075) × 1.300 + 3.075 × ~0.45(px)"
• has_window   — true if the room touches the exterior boundary (bold outer wall)
• is_outdoor   — true only for バルコニー/ベランダ (default false)
• pixel_fraction_of_ld — visual area fraction vs LD (always a number, never null)

Also compute these summary fields:
• living_area_m2    — area_m2 of the LD/LDK (the main living space)
• kitchen_open      — true if kitchen shares an open boundary with the living/dining area
• toilet_count      — number of WC/トイレ rooms
• bathroom_count    — number of UB/浴室 units
• envelope_width_m  — the labeled total building width (e.g. 3.700); null if not labeled
• envelope_height_m — the labeled total building height (e.g. 7.600); null if not labeled

=== MANDATORY: ENVELOPE DIMENSIONS ===
You MUST output envelope_width_m and envelope_height_m as top-level JSON fields.
These are the overall dimension labels of the floor plan boundary — typically one
large number along the bottom edge (width) and one large number along the right or
left edge (height). Even if you only find one of them, output it.
DO NOT embed these values only inside dim_calc strings. They must appear as
"envelope_width_m": <number> and "envelope_height_m": <number> at the top level.

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
  "bathroom_count": 1,
  "envelope_width_m": 3.700,
  "envelope_height_m": 7.600
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
    envelope_width_m: Optional[float] = None
    envelope_height_m: Optional[float] = None
    analyzed_at: str = ""
    model: str = ""
    total_area_m2: Optional[float] = None   # listing's stated area (from DB)
    inferred_rooms: list[str] = field(default_factory=list)  # labels filled by Phase 2
    gap_m2: Optional[float] = None          # listed - extracted indoor sum (negative = overcount)
    quality_flags: list[str] = field(default_factory=list)  # warnings/errors from validation

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

    def __init__(self, model: str = "claude-haiku-4-5-20251001", api_key: str | None = None):
        self.model = model
        self._client = None
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def _client_lazy(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    # ── Phase 1: vision call ──────────────────────────────────────────────────

    @staticmethod
    def _detect_media_type(image_path: str) -> str:
        try:
            with open(image_path, "rb") as f:
                header = f.read(12)
            if header[:3] == b'GIF':
                return "image/gif"
            if header[:8] == b'\x89PNG\r\n\x1a\n':
                return "image/png"
            if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
                return "image/webp"
        except Exception:
            pass
        ext = Path(image_path).suffix.lower()
        return MEDIA_TYPES.get(ext, "image/jpeg")

    def _call_vision(self, image_path: str) -> dict:
        media_type = self._detect_media_type(image_path)
        with open(image_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode()

        client = self._client_lazy()
        msg = client.messages.create(
            model=self.model,
            max_tokens=1024,
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
        # Remove trailing commas before ] or } (common LLM JSON mistake)
        raw = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                cleaned = re.sub(r",\s*([}\]])", r"\1", m.group())
                return json.loads(cleaned)
            raise

    # ── Phase 2: pixel-ratio fill-in ─────────────────────────────────────────

    @staticmethod
    def _apply_pixel_ratio(rooms: list[Room], envelope_area: float | None = None) -> list[str]:
        """
        Fill null area_m2 values from pixel_fraction_of_ld.

        Calibration selection (Case A vs B):
          Case A — LD/LDK has known area AND its own pixel_fraction ≈ 1.0
            → scale = ld.area_m2 / ld.pixel_fraction_of_ld  (should ≈ ld.area_m2)
          Case B — LD pixel_fraction << 1.0 (envelope-relative mode) OR no LD with known area
            → use envelope_area as the scale reference (all fractions sum to ~1.0)
        """
        # Find LD/LDK/DK/kitchen with a known area — use as calibration reference
        _CALIB_KW = ("LDK", "LD", "リビング", "Living", "洋室", "和室",
                     "ダイニングキッチン", "ダイニング", "キッチン", "DK", "D")
        ld = next(
            (r for r in rooms if r.area_m2 is not None and any(
                kw in r.label for kw in _CALIB_KW
            )),
            None,
        )

        # Decide calibration mode
        if (ld is not None
                and ld.pixel_fraction_of_ld is not None
                and ld.pixel_fraction_of_ld >= 0.5):
            # Case A: LD fraction is close to 1.0 — LD-relative mode
            scale = ld.area_m2 / ld.pixel_fraction_of_ld
        elif envelope_area is not None:
            # Case B: fractions are envelope-relative
            scale = envelope_area
        elif ld is not None and ld.area_m2:
            # Fallback: use LD area as rough scale
            scale = ld.area_m2 / max(ld.pixel_fraction_of_ld or 1.0, 0.01)
        else:
            return []

        if scale <= 0:
            log.error(f"_apply_pixel_ratio: non-positive scale={scale:.3f} — skipping pixel fill")
            return []

        inferred = []
        for r in rooms:
            if r.area_m2 is None and r.pixel_fraction_of_ld and r.pixel_fraction_of_ld > 0:
                val = round(scale * r.pixel_fraction_of_ld, 2)
                if val < 0:
                    log.error(f"  Negative inferred area for '{r.label}': {val} (scale={scale:.3f}, frac={r.pixel_fraction_of_ld})")
                    continue
                r.inferred_area_m2 = val
                inferred.append(r.label)
        return inferred

    # ── public API ────────────────────────────────────────────────────────────

    def analyze(self, image_path: str, total_area_m2: float | None = None) -> FloorPlanResult:
        from datetime import datetime

        raw = self._call_vision(image_path)

        quality_flags: list[str] = []

        rooms: list[Room] = []
        for rd in raw.get("rooms", []):
            area = _float_or_none(rd.get("area_m2"))
            label = rd.get("label", "")
            if area is not None and area < 0:
                log.error(f"NEGATIVE AREA from model for '{label}': {area} — nulling and flagging")
                quality_flags.append(f"negative_area:{label}:{area}")
                area = None  # treat as missing; pixel ratio will fill it
            rooms.append(Room(
                label                = label,
                area_m2              = area,
                inferred_area_m2     = None,
                dim_calc             = rd.get("dim_calc"),
                has_window           = bool(rd.get("has_window", False)),
                is_outdoor           = bool(rd.get("is_outdoor", False)),
                pixel_fraction_of_ld = float(rd.get("pixel_fraction_of_ld") or 0),
            ))

        # Compute envelope from labeled dimensions if available
        envelope_w = _float_or_none(raw.get("envelope_width_m"))
        envelope_h = _float_or_none(raw.get("envelope_height_m"))

        # Fallback: extract envelope dims from dim_calc strings when model omits JSON fields
        if not (envelope_w and envelope_h):
            raw_str = json.dumps(raw)
            for pattern in [
                r"envelope\s+(\d+\.?\d*)\s*[×x\*]\s*(\d+\.?\d*)",
                r"(\d+\.?\d*)\s*[×x]\s*(\d+\.?\d*)\s*(?:envelope|全体|外形)",
            ]:
                m = re.search(pattern, raw_str, re.IGNORECASE)
                if m:
                    envelope_w = envelope_w or _float_or_none(m.group(1))
                    envelope_h = envelope_h or _float_or_none(m.group(2))
                    log.debug(f"Envelope extracted from dim_calc fallback: {envelope_w}×{envelope_h}")
                    break

        envelope_area = round(envelope_w * envelope_h, 2) if (envelope_w and envelope_h) else None

        # Sanity check: envelope area must be >= listed indoor area.
        # If it's smaller, the model read a partial width (e.g. balcony span only).
        # Correct by deriving width from total_area_m2 / envelope_height.
        if (total_area_m2 and envelope_area is not None
                and envelope_area > total_area_m2 * 1.5):
            log.warning(
                f"Envelope {envelope_area:.2f}m² is >50% larger than listed {total_area_m2:.2f}m² "
                f"— check dimension labels (shared corridor may have been included)."
            )

        if (total_area_m2 and envelope_area is not None
                and envelope_area < total_area_m2 * 0.97):
            log.warning(
                f"Envelope {envelope_area:.2f}m² < listed {total_area_m2:.2f}m² "
                f"— width label likely misread (partial span used). Correcting."
            )
            if envelope_h:
                envelope_w = round(total_area_m2 / envelope_h, 2)
                log.warning(f"  Corrected envelope_width_m → {envelope_w}")
            envelope_area = total_area_m2  # use listed area as calibration floor

        inferred = self._apply_pixel_ratio(rooms, envelope_area=envelope_area)

        # ── Gap / over-count validation ───────────────────────────────────────
        indoor_sum = round(sum((r.best_area_m2 or 0) for r in rooms if not r.is_outdoor), 2)
        gap_m2: float | None = None

        if total_area_m2 and total_area_m2 > 0:
            gap_m2 = round(total_area_m2 - indoor_sum, 2)
            tolerance = total_area_m2 * 0.05  # 5 %

            if gap_m2 < -tolerance:
                # Extracted indoor area EXCEEDS listed size → negative space
                excess = -gap_m2
                log.error(
                    f"NEGATIVE SPACE [{image_path}]: extracted {indoor_sum:.2f}m² "
                    f"> listed {total_area_m2:.2f}m² (excess={excess:.2f}m²). "
                    f"Normalising inferred rooms."
                )
                quality_flags.append(f"negative_space:excess={excess:.2f}m²")

                # Hard failure guard: if excess is >50% of listed area, something
                # is fundamentally wrong (e.g. shared corridor counted, doubled rooms).
                if excess > total_area_m2 * 0.50:
                    quality_flags.append("hard_fail:excess>50pct")
                    log.error(
                        f"  HARD FAIL: excess {excess:.2f}m² is >50% of listed "
                        f"{total_area_m2:.2f}m². Result unreliable."
                    )
                else:
                    # Normalise: scale inferred rooms down so indoor_sum ≈ total_area_m2.
                    # Only touch inferred (Phase 2) rooms — leave dimension-line rooms alone.
                    known_indoor = round(
                        sum((r.area_m2 or 0) for r in rooms
                            if not r.is_outdoor and r.area_m2 is not None),
                        2,
                    )
                    inferred_total = round(
                        sum((r.inferred_area_m2 or 0) for r in rooms
                            if not r.is_outdoor and r.inferred_area_m2 is not None
                            and r.area_m2 is None),
                        2,
                    )
                    target_inferred = total_area_m2 - known_indoor
                    if inferred_total > 0 and target_inferred > 0:
                        factor = target_inferred / inferred_total
                        for r in rooms:
                            if (not r.is_outdoor
                                    and r.inferred_area_m2 is not None
                                    and r.area_m2 is None):
                                r.inferred_area_m2 = round(r.inferred_area_m2 * factor, 2)
                        indoor_sum = round(
                            sum((r.best_area_m2 or 0) for r in rooms if not r.is_outdoor), 2
                        )
                        gap_m2 = round(total_area_m2 - indoor_sum, 2)
                        log.info(f"  After normalisation: indoor_sum={indoor_sum:.2f}m², gap={gap_m2:.2f}m²")

        result = FloorPlanResult(
            rooms            = rooms,
            living_area_m2   = _float_or_none(raw.get("living_area_m2")),
            kitchen_open     = raw.get("kitchen_open"),
            toilet_count     = int(raw.get("toilet_count") or 0),
            bathroom_count   = int(raw.get("bathroom_count") or 0),
            envelope_width_m = envelope_w,
            envelope_height_m= envelope_h,
            analyzed_at      = datetime.now().isoformat(),
            model            = self.model,
            total_area_m2    = total_area_m2,
            inferred_rooms   = inferred,
            gap_m2           = gap_m2,
            quality_flags    = quality_flags,
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
            indoor = sum((r.best_area_m2 or 0) for r in result.rooms if not r.is_outdoor)
            gap = args.total_area - indoor
            gap_str = f"{gap:+.1f} m²"
            if gap < -args.total_area * 0.05:
                gap_str += " ⚠ NEGATIVE SPACE (over-counted)"
            print(f"  Listed: {args.total_area} m²  |  Extracted: {indoor:.1f} m²  |  Gap: {gap_str}")
        print(f"  LD area: {result.living_area_m2} m²  |  Kitchen open: {result.kitchen_open}")
        print(f"  WC: {result.toilet_count}  |  Bath: {result.bathroom_count}")
        if result.inferred_rooms:
            print(f"  Pixel-inferred: {', '.join(result.inferred_rooms)}")
        if result.quality_flags:
            print(f"  ⚠ Quality flags: {', '.join(result.quality_flags)}")
        print(f"{'─'*55}")
        print(result.summary())
        print(f"{'─'*55}\n")
