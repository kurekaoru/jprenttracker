"""Standalone training script — equivalent to classify_train.ipynb without plots."""
import os, random, time
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import torchvision.transforms as T
import timm
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix

TRAINING_DIR = Path(__file__).parent
LABELS_CSV   = TRAINING_DIR.parent / "training_labels.csv"
MODEL_OUT    = TRAINING_DIR / "classifier.pt"
ONNX_OUT     = TRAINING_DIR / "classifier.onnx"

CLASSES     = ["appliances", "exterior", "facilities", "floor_plan", "interior"]
NUM_CLASSES = len(CLASSES)
C2I         = {c: i for i, c in enumerate(CLASSES)}

IMG_SIZE    = 224
BATCH_SIZE  = 32
EPOCHS      = 25
LR          = 3e-4
PATIENCE    = 5
SKIP_THRESH = 0.50

DEVICE = (
    "mps" if torch.backends.mps.is_available()
    else "cuda" if torch.cuda.is_available()
    else "cpu"
)
print(f"device: {DEVICE}")

torch.manual_seed(42); random.seed(42); np.random.seed(42)

# ── Data ──────────────────────────────────────────────────────────────────────

df = pd.read_csv(LABELS_CSV)
df["full_path"] = df["path"].apply(lambda p: TRAINING_DIR / p)
df = df[df["full_path"].apply(lambda p: p.exists())].reset_index(drop=True)
print(f"\n{len(df)} images on disk")
print(df.groupby("label").size().to_string())

train_df, val_df = train_test_split(df, test_size=0.15, stratify=df["label"], random_state=42)
val_df,  test_df = train_test_split(val_df, test_size=0.5,  stratify=val_df["label"], random_state=42)
print(f"\ntrain={len(train_df)}  val={len(val_df)}  test={len(test_df)}\n")

MEAN = [0.485, 0.456, 0.406]
STD  = [0.229, 0.224, 0.225]

train_tfm = T.Compose([
    T.Resize((IMG_SIZE + 32, IMG_SIZE + 32)),
    T.RandomCrop(IMG_SIZE),
    T.RandomHorizontalFlip(),
    T.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.2),
    T.ToTensor(),
    T.Normalize(MEAN, STD),
])
val_tfm = T.Compose([
    T.Resize((IMG_SIZE, IMG_SIZE)),
    T.ToTensor(),
    T.Normalize(MEAN, STD),
])


class ApartmentDataset(Dataset):
    def __init__(self, df, transform):
        self.paths  = df["full_path"].tolist()
        self.labels = [C2I[l] for l in df["label"]]
        self.tfm    = transform

    def __len__(self): return len(self.paths)

    def __getitem__(self, idx):
        try:
            img = Image.open(self.paths[idx]).convert("RGB")
        except Exception:
            img = Image.new("RGB", (IMG_SIZE, IMG_SIZE))
        return self.tfm(img), self.labels[idx]


train_loader = DataLoader(ApartmentDataset(train_df, train_tfm), batch_size=BATCH_SIZE,
                          shuffle=True, num_workers=0)
val_loader   = DataLoader(ApartmentDataset(val_df,   val_tfm),   batch_size=BATCH_SIZE,
                          shuffle=False, num_workers=0)
test_loader  = DataLoader(ApartmentDataset(test_df,  val_tfm),   batch_size=BATCH_SIZE,
                          shuffle=False, num_workers=0)

# ── Model ─────────────────────────────────────────────────────────────────────

model = timm.create_model("efficientnet_b0", pretrained=True, num_classes=NUM_CLASSES)
model = model.to(DEVICE)

label_counts = df["label"].value_counts()
weights = torch.tensor([1.0 / label_counts.get(c, 1) for c in CLASSES], dtype=torch.float32)
weights = (weights / weights.sum() * NUM_CLASSES).to(DEVICE)
print("class weights:", {c: f"{w:.3f}" for c, w in zip(CLASSES, weights.cpu().tolist())})

criterion = nn.CrossEntropyLoss(weight=weights)
optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=EPOCHS)

# ── Train ─────────────────────────────────────────────────────────────────────

def run_epoch(loader, train=True):
    model.train(train)
    total_loss = correct = n = 0
    with torch.set_grad_enabled(train):
        for imgs, labels in loader:
            imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
            logits = model(imgs)
            loss   = criterion(logits, labels)
            if train:
                optimizer.zero_grad(); loss.backward(); optimizer.step()
            total_loss += loss.item() * len(labels)
            correct    += (logits.argmax(1) == labels).sum().item()
            n          += len(labels)
    return total_loss / n, correct / n


best_val_loss = float("inf")
patience_left = PATIENCE

for epoch in range(1, EPOCHS + 1):
    t0 = time.time()
    tr_loss, tr_acc = run_epoch(train_loader, train=True)
    va_loss, va_acc = run_epoch(val_loader,   train=False)
    scheduler.step()

    improved = va_loss < best_val_loss
    if improved:
        best_val_loss = va_loss
        patience_left = PATIENCE
        torch.save(model.state_dict(), MODEL_OUT)
    else:
        patience_left -= 1

    mark = " *" if improved else ""
    print(f"Epoch {epoch:02d}/{EPOCHS}  "
          f"loss {tr_loss:.4f}/{va_loss:.4f}  "
          f"acc {tr_acc:.3f}/{va_acc:.3f}  "
          f"{time.time()-t0:.0f}s{mark}")

    if patience_left == 0:
        print(f"Early stop at epoch {epoch}")
        break

print(f"\nBest val loss: {best_val_loss:.4f}")

# ── Evaluate ──────────────────────────────────────────────────────────────────

model.load_state_dict(torch.load(MODEL_OUT, map_location=DEVICE))
model.eval()

all_preds, all_labels = [], []
with torch.no_grad():
    for imgs, labels in test_loader:
        preds = model(imgs.to(DEVICE)).argmax(1).cpu().tolist()
        all_preds.extend(preds)
        all_labels.extend(labels.tolist())

print("\n=== Test set results ===")
print(classification_report(all_labels, all_preds, target_names=CLASSES, digits=3))
print("Confusion matrix:")
print(confusion_matrix(all_labels, all_preds))

# ── Export ONNX ───────────────────────────────────────────────────────────────

model.load_state_dict(torch.load(MODEL_OUT, map_location="cpu"))
model.eval().cpu()
dummy = torch.randn(1, 3, IMG_SIZE, IMG_SIZE)
torch.onnx.export(
    model, dummy, str(ONNX_OUT),
    input_names=["image"], output_names=["logits"],
    dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}},
    opset_version=17,
)
print(f"\nONNX saved: {ONNX_OUT}  ({ONNX_OUT.stat().st_size / 1e6:.1f} MB)")
print("\nDone. Deploy with:")
print("  gcloud compute scp --zone us-central1-a training/classifier.onnx jkktrackr:/home/kaorukure/jprenttracker/")
