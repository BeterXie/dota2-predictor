# Dota 2 Match Predictor

Predict Dota 2 match outcomes using historical match data from OpenDota. Data is stored in SQLite for both ML training and web display.

See [DESIGN.md](DESIGN.md) for full architecture and module specifications.

## Project Structure

```
dota2-predictor/
├── fetch/          # Module A: Data fetcher (API -> SQLite)
├── features/       # Module B: Feature engine (SQLite -> Parquet)
├── train/          # Module C: Model training (Parquet -> model.pkl)
├── predict/        # Module D: Prediction (model + DB -> prediction)
├── web/            # Module E: FastAPI server (DB -> REST API -> web page)
├── data/           # All data artifacts (gitignored)
│   ├── dota2.db            # Main SQLite database
│   ├── features/           # Parquet feature files
│   ├── models/             # Trained model files
│   └── predictions/        # Prediction JSON output
├── DESIGN.md       # Architecture & module specs
└── README.md
```

## Quick Start

```bash
cp .env.template .env
mkdir -p data/features data/models data/predictions

pip install httpx pandas numpy scikit-learn xgboost pyarrow fastapi uvicorn

# Step 1: Fetch match data into SQLite
python -m fetch.main

# Step 2: Build features
python -m features.main

# Step 3: Train model
python -m train.main

# Step 4: Predict
python -m predict.main --radiant 9247354 --dire 10150538

# Step 5: Start web server
python -m web.main
# Open http://localhost:8000 to browse matches
```

## Database

All match data lives in `data/dota2.db` (SQLite). The web API reads directly from it.
Browse it with any SQLite tool:

```bash
sqlite3 data/dota2.db "SELECT match_id, radiant_win, duration FROM matches LIMIT 5"
```
