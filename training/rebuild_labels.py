"""
Run after manually moving images between labeled/ subdirectories.
Rewrites training_labels.csv to match the current folder state.

Supports two layouts:
  labeled/CLASS/file.jpg                          → label = CLASS
  labeled/CLASS/Misclassified/CORRECT_CLASS/file  → label = CORRECT_CLASS
"""
import csv
from pathlib import Path
from collections import Counter

base    = Path(__file__).parent
labeled = base / "labeled"
out     = base.parent / "training_labels.csv"

CLASSES = {"appliances", "exterior", "facilities", "floor_plan", "interior"}

def normalise(name: str) -> str | None:
    """Case-insensitive match against CLASSES."""
    low = name.lower()
    return low if low in CLASSES else None


rows = []
skipped = []

for class_dir in sorted(labeled.iterdir()):
    if not class_dir.is_dir():
        continue
    src_label = normalise(class_dir.name)
    if src_label is None:
        continue

    for f in sorted(class_dir.iterdir()):
        # Direct files in labeled/CLASS/ → label = CLASS
        if f.is_file() and not f.name.startswith("."):
            lid, fname = f.name.split("__", 1)
            rows.append({"path": f"images/{lid}/{fname}", "label": src_label})

        # labeled/CLASS/Misclassified/CORRECT_CLASS/ → label = CORRECT_CLASS
        elif f.is_dir() and f.name.lower() == "misclassified":
            for correct_dir in sorted(f.iterdir()):
                if not correct_dir.is_dir():
                    continue
                correct_label = normalise(correct_dir.name)
                if correct_label is None:
                    skipped.append(str(correct_dir))
                    continue
                for img in sorted(correct_dir.iterdir()):
                    if img.is_file() and not img.name.startswith("."):
                        lid, fname = img.name.split("__", 1)
                        rows.append({"path": f"images/{lid}/{fname}", "label": correct_label})

with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["path", "label"])
    w.writeheader()
    w.writerows(rows)

counts = Counter(r["label"] for r in rows)
print(f"Wrote {len(rows)} rows to {out}")
for cls in sorted(CLASSES):
    print(f"  {cls:12s}: {counts[cls]}")
if skipped:
    print(f"\nWARNING: unrecognised folders skipped: {skipped}")
