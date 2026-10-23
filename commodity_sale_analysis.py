import numpy as np
import pandas as pd
import sqlite3
import re
import os
import json
from datetime import datetime
import argparse
from pathlib import Path


def get_data(db_path: str):
    try:
        with sqlite3.connect(db_path) as conn:
            goods_order = pd.read_sql_query(
                "SELECT * FROM commodity_order", conn)
    except Exception as e:
        print(f"ERROR: {e}")
        raise
    return goods_order


def analyze_orders(df: pd.DataFrame, time_period: str | None,) -> pd.DataFrame:

    df = df.copy()

    # ── 1. Parse order date from order ID ───────────────────────────────────
    df["order_date"] = pd.to_datetime(
        df["订单号"].str[:6], format="%y%m%d"
    )
    df.sort_values(by="order_date", inplace=True, ignore_index=True)

    TIME_PERIOD_DAYS = {
        "recent_30days": 30,
        "recent_90days": 90,
    }

    if time_period:
        max_date = df["order_date"].max()
        days = TIME_PERIOD_DAYS[time_period]
        df = df[
            (df["order_date"] >= max_date - pd.Timedelta(days=days)) &
            (df["order_date"] <= max_date)
        ]

    # ── 2. Label valid / invalid orders ─────────────────────────────────────
    valid_statuses = {"已收货", "待发货", "已发货，待收货"}
    df["order_status"] = df["订单状态"].apply(
        lambda x: "valid" if x in valid_statuses else "invalid"
    )

    # ── 3. Overall aggregation ───────────────────────────────────────────────
    total_statistic = df.groupby("商品", as_index=False).agg(
        total_order_num=("订单号", "nunique"),
        total_order_sales=("商品总价(元)", "sum"),
        total_order_actual_income=("商家实收金额(元)", "sum"),
        per_goods_sales_start_date=("order_date", "min"),
    )

    # ── 4. Valid-order aggregation ───────────────────────────────────────────
    valid_order = df[df["order_status"] == "valid"]
    valid_statistic = valid_order.groupby("商品", as_index=False).agg(
        valid_order_num=("订单号", "nunique"),
        valid_order_sales=("商品总价(元)", "sum"),
        valid_order_actual_income=("商家实收金额(元)", "sum"),
    )

    # ── 5. Recent 7 / 14-day order counts ───────────────────────────────────
    max_date = df["order_date"].max()
    recent_7d = df[df["order_date"] > max_date - pd.Timedelta(days=7)]
    recent_7d_map = recent_7d.groupby("商品")["订单号"].nunique().to_dict()
    recent_14d = df[df["order_date"] > max_date - pd.Timedelta(days=14)]
    recent_14d_map = recent_14d.groupby("商品")["订单号"].nunique().to_dict()

    # ── 6. Single-item vs multi-item order stats ─────────────────────────────
    one_item_map = (
        df[df["商品数量(件)"] == 1]
        .groupby("商品")["订单号"].nunique()
        .to_dict()
    )
    multi_item_desc = (
        df[df["商品数量(件)"] > 1]
        .groupby("商品", as_index=False)["商品数量(件)"].describe()
    )

    # ── 7. Derived ratio columns ─────────────────────────────────────────────
    statistic = pd.merge(
        left=total_statistic,
        right=valid_statistic,
        how="left",
        on="商品",
    )

    statistic["per_goods_total_order_num_percentage"] = statistic["total_order_num"] / \
        statistic["total_order_num"].sum()

    statistic["per_goods_sales_period_days"] = (
        df["order_date"].max()-statistic["per_goods_sales_start_date"]).dt.days

    statistic["valid_order_num_percentage"] = statistic["valid_order_num"] / \
        statistic["total_order_num"]
    statistic["valid_order_sales_percentage"] = statistic["valid_order_sales"] / \
        statistic["total_order_sales"]
    statistic["valid_order_actual_income_percentage"] = statistic["valid_order_actual_income"] / \
        statistic["total_order_actual_income"]

    statistic["recent_7days_order_num"] = statistic["商品"].map(recent_7d_map)
    statistic["recent_7days_order_num_percentage"] = statistic["recent_7days_order_num"] / \
        statistic["total_order_num"]

    statistic["recent_14days_order_num"] = statistic["商品"].map(
        recent_14d_map)
    statistic["recent_14days_order_num_percentage"] = statistic["recent_14days_order_num"] / \
        statistic["total_order_num"]

    statistic["one_order_one_item_percentage"] = (
        statistic["商品"].map(one_item_map) / statistic["total_order_num"]
    )

    statistic = statistic.merge(multi_item_desc, how='left', on="商品")

    statistic.rename(columns={
        "count": "one_order_multi_items_count",
        "mean":  "one_order_multi_items_mean",
        "std":   "one_order_multi_items_std",
        "min":   "one_order_multi_items_min",
        "25%":   "one_order_multi_items_25%",
        "50%":   "one_order_multi_items_50%",
        "75%":   "one_order_multi_items_75%",
        "max":   "one_order_multi_items_max",
    }, inplace=True)

    # ── 8. Week-level trend metrics ─────────────────────────────────────────
    min_date = df["order_date"].min()
    total_weeks = (max_date - min_date).days // 7 + 1
    df["week_num"] = "week_" + (
        total_weeks - (max_date - df["order_date"]).dt.days // 7
    ).astype(str)

    week_pivot = pd.pivot_table(
        data=df,
        values="订单号",
        index="商品",
        columns="week_num",
        aggfunc="nunique",
        sort=False,
    )
    statistic = statistic.merge(week_pivot, how="left", on="商品")

    statistic.fillna(0, inplace=True)

    week_cols = [
        c for c in statistic.columns if re.fullmatch(r"week_\d+", c)]

    statistic["week_std"] = statistic[week_cols].apply(np.std, axis=1)

    eps = 1e-6

    statistic["one_week_diff"] = (
        (statistic[week_cols[-1]] - statistic[week_cols[-2]])
        / (statistic[week_cols[-2]] + eps)
    )
    statistic["two_weeks_diff"] = (
        statistic[week_cols[-2:]].sum(axis=1)
        - statistic[week_cols[-4:-2]].sum(axis=1)
    ) / (
        statistic[week_cols[-4:-2]].sum(axis=1) + eps
    )

    statistic["four_weeks_diff"] = (
        statistic[week_cols[-4:]].sum(axis=1)
        - statistic[week_cols[-8:-4]].sum(axis=1)
    ) / (
        statistic[week_cols[-8:-4]].sum(axis=1) + eps
    )

    BASE_DIR = Path("/home/node/.openclaw/workspace")

    filename = f"goods_order_analysis_{datetime.now().isoformat(timespec='milliseconds')}.csv"

    full_dir = BASE_DIR / "output" / "csv"
    full_dir.mkdir(parents=True, exist_ok=True)

    full_path = full_dir / filename

    statistic.to_csv(full_path, index=False, encoding="utf-8")

    return statistic


def analysis_result(df: pd.DataFrame) -> pd.DataFrame:

    analysis_result = []

    for i in range(df.shape[0]):
        single_goods_describe = {

            "goods_name": df.loc[i, "商品"],

            "trade_overview": {
                "total_order_num": int(df.loc[i, "total_order_num"]),
                "total_order_sales": round(df.loc[i, "total_order_sales"], 4),
                "current_goods_total_order_num_pct": round(df.loc[i, "per_goods_total_order_num_percentage"], 4),
                "current_goods_sales_period_days": int(df.loc[i, "per_goods_sales_period_days"]),
                "valid_order_num": int(df.loc[i, "valid_order_num"]),
                "valid_order_pct": round(df.loc[i, "valid_order_num_percentage"], 4),
                "valid_order_actual_income": round(df.loc[i, "valid_order_actual_income"], 4),
                "order_num_7d": int(df.loc[i, "recent_7days_order_num"]),
                "order_num_7d_pct": round(df.loc[i, "recent_7days_order_num_percentage"], 4),
                "order_num_14d": int(df.loc[i, "recent_14days_order_num"]),
                "order_num_14d_pct": round(df.loc[i, "recent_14days_order_num_percentage"], 4),
            },

            "purchase_behavior": {
                "single_item_order_pct": df.loc[i, "one_order_one_item_percentage"],
                "multi_item_order_stats": {
                    "min": int(df.loc[i, "one_order_multi_items_min"]),
                    "25%": int(df.loc[i, "one_order_multi_items_25%"]),
                    "50%": int(df.loc[i, "one_order_multi_items_50%"]),
                    "75%": int(df.loc[i, "one_order_multi_items_75%"]),
                    "max": int(df.loc[i, "one_order_multi_items_max"]),
                    "mean": int(df.loc[i, "one_order_multi_items_mean"]),
                    "std": int(df.loc[i, "one_order_multi_items_std"]),
                }
            },

            "weekly_changes": {
                "wow_order_num_change_pct": round(df.loc[i, "one_week_diff"], 4),
                "last_2w_vs_prev_2w_order_num_change_pct": round(df.loc[i, "two_weeks_diff"], 4),
                "last_4w_vs_prev_4w_order_num_change_pct": round(df.loc[i, "four_weeks_diff"], 4),
            },

        },

        analysis_result.append(single_goods_describe)

    BASE_DIR = Path("/home/node/.openclaw/workspace")

    output_dir = BASE_DIR / "output" / "jsonl"

    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / \
        f"goods_order_analysis_{datetime.now().isoformat(timespec='milliseconds')}.jsonl"

    with open(json_path, "w", encoding="utf-8") as f:
        f.writelines(
            json.dumps(item, ensure_ascii=False) + "\n"
            for item in analysis_result
        )

    return analysis_result


def main():

    parser = argparse.ArgumentParser(
        description="This is a script to compute order statictic data")
    parser.add_argument(
        "--time_period",
        default=None,
        choices=["recent_30days", "recent_90days"],
        help="provide a time period",
    )
    args = parser.parse_args()

    # db_path = "/home/strawberrycharlotte/database/database.db"
    db_path = "../../../database/database.db"
    db_path = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), db_path))
    goods_order = get_data(db_path)

    statistic = analyze_orders(goods_order, time_period=args.time_period)
    analysis_result(statistic)


if __name__ == "__main__":
    main()
