#!/usr/bin/env python3
"""Example embedding script for local multilingual-e5-small."""

from pathlib import Path

from sentence_transformers import SentenceTransformer

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "multilingual-e5-small"


def main() -> None:
    if not MODEL_DIR.exists():
        raise FileNotFoundError(
            f"Model not found: {MODEL_DIR}\n"
            "Run: python scripts/download_model.py"
        )

    model = SentenceTransformer(str(MODEL_DIR))

    passages = [
        "passage: 東京は日本の首都です。",
        "passage: 富士山は日本で最も高い山です。",
        "passage: Python is a programming language.",
    ]
    query = "query: 日本の首都はどこですか？"

    passage_emb = model.encode(passages, normalize_embeddings=True)
    query_emb = model.encode([query], normalize_embeddings=True)

    scores = (query_emb @ passage_emb.T)[0]

    print("Query:", query)
    print("\nRanking:")
    for text, score in sorted(zip(passages, scores), key=lambda x: float(x[1]), reverse=True):
        print(f"{float(score):.4f}  {text}")


if __name__ == "__main__":
    main()
