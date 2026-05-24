#!/usr/bin/env python3
"""
CLIP image embedder + visual search CLI.

Runs on your Mac (Apple Silicon / CPU). The VM has too little RAM for CLIP.

Commands:
    python embedder.py embed              # embed pending images, push vectors to VM
    python embedder.py search "text"      # search against stored embeddings
    python embedder.py search "text" -n5  # top 5 results

Setup (once):
    pip install open-clip-torch Pillow numpy requests

The embedder:
  1. Pulls jkk_monitor.db from the VM
  2. Downloads thumbnail images from their stored URLs
  3. Generates CLIP ViT-B/32 embeddings (fast on MPS/M-series)
  4. Pipes the new embedding rows back into the VM's DB
"""

import argparse
import base64
import logging
import os
import struct
import subprocess
import sys
import tempfile
from io import BytesIO

import numpy as np
import requests
import sqlite3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

REMOTE_DB = "jkktrackr:/home/kaorukure/jprenttracker/jkk_monitor.db"
ZONE      = "us-central1-a"
MODEL     = "ViT-B-32"
PRETRAINED = "openai"
DIM       = 512

_IMG_HEADERS = {"User-Agent": "jkktrackr/1.0 (embedder)"}


# ── vector helpers ─────────────────────────────────────────────────────────────

def vec_to_blob(v: np.ndarray) -> bytes:
    return struct.pack(f"{len(v)}f", *v.tolist())

def blob_to_vec(b: bytes) -> np.ndarray:
    n = len(b) // 4
    return np.array(struct.unpack(f"{n}f", b), dtype=np.float32)


# ── CLIP ───────────────────────────────────────────────────────────────────────

_model = _preprocess = _tokenizer = _device = None

def _load_clip():
    global _model, _preprocess, _tokenizer, _device
    if _model is not None:
        return
    try:
        import torch
        import open_clip
    except ImportError:
        sys.exit("Run: pip install open-clip-torch Pillow numpy requests")

    import torch
    _device = "mps" if torch.backends.mps.is_available() else "cpu"
    log.info(f"Loading CLIP {MODEL} on {_device}...")
    _model, _, _preprocess = open_clip.create_model_and_transforms(
        MODEL, pretrained=PRETRAINED, device=_device
    )
    _model.eval()
    _tokenizer = open_clip.get_tokenizer(MODEL)


def embed_image_url(url: str) -> np.ndarray:
    import torch
    from PIL import Image
    _load_clip()
    r = requests.get(url, timeout=20, headers=_IMG_HEADERS)
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGB")
    tensor = _preprocess(img).unsqueeze(0).to(_device)
    with torch.no_grad():
        feat = _model.encode_image(tensor)
        feat = feat / feat.norm(dim=-1, keepdim=True)
    return feat[0].cpu().numpy().astype(np.float32)


def embed_image_path(path: str) -> np.ndarray:
    import torch
    from PIL import Image
    _load_clip()
    img = Image.open(path).convert("RGB")
    tensor = _preprocess(img).unsqueeze(0).to(_device)
    with torch.no_grad():
        feat = _model.encode_image(tensor)
        feat = feat / feat.norm(dim=-1, keepdim=True)
    return feat[0].cpu().numpy().astype(np.float32)


def embed_text(text: str) -> np.ndarray:
    import torch
    _load_clip()
    tokens = _tokenizer([text]).to(_device)
    with torch.no_grad():
        feat = _model.encode_text(tokens)
        feat = feat / feat.norm(dim=-1, keepdim=True)
    return feat[0].cpu().numpy().astype(np.float32)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def pull_db(local_path: str):
    log.info("Pulling DB from VM...")
    subprocess.run(
        ["gcloud", "compute", "scp", REMOTE_DB, local_path, f"--zone={ZONE}"],
        check=True
    )


def push_embeddings(local_path: str):
    """Upsert just the listing_embeddings rows into the VM's DB via SSH."""
    con = sqlite3.connect(local_path)
    rows = con.execute(
        "SELECT listing_id, embedding, model, created_at FROM listing_embeddings"
    ).fetchall()
    con.close()
    if not rows:
        log.info("No embeddings to push.")
        return

    lines = [
        "BEGIN;",
        "CREATE TABLE IF NOT EXISTS listing_embeddings ("
        "listing_id TEXT PRIMARY KEY, embedding BLOB NOT NULL, "
        "model TEXT, created_at TEXT DEFAULT (datetime('now')));",
    ]
    for lid, emb, mdl, cat in rows:
        blob_hex = bytes(emb).hex()
        mdl_esc  = (mdl or "").replace("'", "''")
        cat_esc  = (cat or "").replace("'", "''")
        lines.append(
            f"INSERT OR REPLACE INTO listing_embeddings "
            f"(listing_id, embedding, model, created_at) "
            f"VALUES ('{lid}', x'{blob_hex}', '{mdl_esc}', '{cat_esc}');"
        )
    lines.append("COMMIT;")

    result = subprocess.run(
        ["gcloud", "compute", "ssh", "jkktrackr", f"--zone={ZONE}",
         "--", "sqlite3 /home/kaorukure/jprenttracker/jkk_monitor.db"],
        input="\n".join(lines).encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        log.error(f"Push failed: {result.stderr.decode()}")
        raise RuntimeError("Push failed")
    log.info(f"Pushed {len(rows)} embedding(s) to VM.")


def ensure_table(con: sqlite3.Connection):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS listing_embeddings (
            listing_id  TEXT PRIMARY KEY,
            embedding   BLOB NOT NULL,
            model       TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    con.commit()


# ── commands ───────────────────────────────────────────────────────────────────

def cmd_embed(args):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        local_db = f.name
    try:
        pull_db(local_db)
        con = sqlite3.connect(local_db)
        ensure_table(con)

        # listings with an image URL but no embedding yet
        rows = con.execute("""
            SELECT li.listing_id,
                   COALESCE(li.local_path, li.url) AS src,
                   li.url
            FROM   listing_images li
            LEFT JOIN listing_embeddings le ON le.listing_id = li.listing_id
            WHERE  le.listing_id IS NULL
              AND  (li.local_path IS NOT NULL OR li.url IS NOT NULL)
            GROUP  BY li.listing_id
        """).fetchall()

        if not rows:
            log.info("Nothing to embed — all images already have vectors.")
            return

        log.info(f"Embedding {len(rows)} listing(s)...")
        ok = 0
        for lid, src, url in rows:
            try:
                # prefer local file; fall back to URL
                if src and not src.startswith("http") and os.path.exists(src):
                    vec = embed_image_path(src)
                elif url:
                    vec = embed_image_url(url)
                else:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO listing_embeddings "
                    "(listing_id, embedding, model) VALUES (?,?,?)",
                    (lid, vec_to_blob(vec), f"{MODEL}/{PRETRAINED}")
                )
                con.commit()
                ok += 1
                log.info(f"  [{ok}/{len(rows)}] {lid}")
            except Exception as e:
                log.warning(f"  Skip {lid}: {e}")

        log.info(f"Embedded {ok}/{len(rows)}. Pushing to VM...")
        push_embeddings(local_db)
    finally:
        os.unlink(local_db)


def cmd_search(args):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        local_db = f.name
    try:
        pull_db(local_db)
        con = sqlite3.connect(local_db)
        con.row_factory = sqlite3.Row
        ensure_table(con)

        emb_rows = con.execute(
            "SELECT listing_id, embedding FROM listing_embeddings"
        ).fetchall()
        if not emb_rows:
            print("No embeddings stored yet — run 'embed' first.")
            return

        q_vec = embed_text(args.query)
        ids  = [r["listing_id"] for r in emb_rows]
        vecs = np.stack([blob_to_vec(bytes(r["embedding"])) for r in emb_rows])
        scores = (vecs @ q_vec).tolist()

        ranked = sorted(zip(ids, scores), key=lambda x: -x[1])[:args.n]
        print(f"\nTop {len(ranked)} matches for: \"{args.query}\"\n")
        for lid, score in ranked:
            row = con.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
            if row:
                print(
                    f"  {score:.3f}  {row['name']}  {row['ward']}  "
                    f"¥{row['rent']:,}  {row['layout'] or '—'}  {row['url'] or ''}"
                )
    finally:
        os.unlink(local_db)


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="CLIP embedder / search for JKKTrackr")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("embed", help="Embed pending images and push vectors to VM")

    sp = sub.add_parser("search", help="Search listings by text description")
    sp.add_argument("query", help='Search query, e.g. "bright open kitchen"')
    sp.add_argument("-n", type=int, default=10, help="Number of results (default 10)")

    args = p.parse_args()
    {"embed": cmd_embed, "search": cmd_search}[args.cmd](args)
