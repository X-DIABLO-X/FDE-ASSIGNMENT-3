from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def _read_csv(name: str) -> pd.DataFrame:
    # Starter fix: the default read_csv turns empty cells into float NaN (and strings such as
    # "None"/"NA" into NaN as well). NaN is truthy, not JSON-serialisable and breaks `is None`
    # checks, so empty cells are kept as "" and everything is read as text.
    return pd.read_csv(DATA_DIR / name, dtype=str, keep_default_na=False)


def load_employees() -> pd.DataFrame:
    return _read_csv("employees.csv")


def load_budgets() -> pd.DataFrame:
    df = _read_csv("department_budgets.csv")
    for col in ("annual_software_budget_usd", "committed_usd", "available_usd"):
        df[col] = df[col].astype(int)
    return df


def load_software_catalog() -> pd.DataFrame:
    df = _read_csv("software_catalog.csv")
    df["annual_cost_usd"] = df["annual_cost_usd"].astype(int)
    df["licensed_seats"] = df["licensed_seats"].astype(int)
    return df


def load_vendors() -> pd.DataFrame:
    return _read_csv("vendors.csv")


def load_purchase_history() -> pd.DataFrame:
    df = _read_csv("purchase_history.csv")
    df["annual_amount_usd"] = df["annual_amount_usd"].astype(int)
    return df


def load_requests() -> list[dict]:
    return json.loads((DATA_DIR / "requests.json").read_text(encoding="utf-8"))


def get_request(request_id: str) -> dict:
    for request in load_requests():
        if request["request_id"] == request_id:
            return request
    raise KeyError(f"Unknown request_id: {request_id}")


def load_policy_text() -> str:
    return (DATA_DIR / "procurement_policy.md").read_text(encoding="utf-8")
