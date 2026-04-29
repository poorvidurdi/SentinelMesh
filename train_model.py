import pandas as pd
import pickle
import os
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score
import sys
sys.stdout.reconfigure(encoding='utf-8')

METRICS_FILE = "data/metrics.csv"
MODEL_FILE = "model.pkl"

def train():
    if not os.path.exists(METRICS_FILE):
        print("No metrics.csv found. Run monitor.py and nodes first to collect data.")
        return

    df = pd.read_csv(METRICS_FILE)
    print(f"[Trainer] Total samples loaded: {len(df)}")
    print(f"[Trainer] Label distribution:\n{df['label'].value_counts()}\n")

    if len(df) < 20:
        print("[Trainer] Not enough data yet. Collect more samples first.")
        return

    if df['label'].nunique() < 2:
        print("[Trainer] Need both healthy and pre_failure samples. Run a degrading node first.")
        return

    X = df[["battery", "packet_loss"]]
    y = df["label"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    model = RandomForestClassifier(n_estimators=100, random_state=42, class_weight="balanced")
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    print("[Trainer] ── Model Evaluation ──────────────────────")
    print(f"  Accuracy: {accuracy_score(y_test, y_pred):.2f}")
    print(classification_report(y_test, y_pred))

    with open(MODEL_FILE, "wb") as f:
        pickle.dump(model, f)

    print(f"[Trainer] Model saved to {MODEL_FILE}")

if __name__ == "__main__":
    train()