"""商品销售数据分析 MCP 工具。"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import numpy as np
import pandas as pd


DATABASE_PATH: Final[Path] = Path("~/.codex_bridge/database.db").expanduser()
WORKSPACE_ROOT: Final[Path] = Path("~/.codex/workspace").expanduser()
TIME_PERIOD_DAYS: Final[dict[str, int]] = {
    "recent7days": 7,
    "recent30days": 30,
    "recent90days": 90,
}
TimePeriod = Literal["recent7days", "recent30days", "recent90days"]
VALID_STATUSES: Final[set[str]] = {"已收货", "待发货", "已发货，待收货"}
REQUIRED_COLUMNS: Final[set[str]] = {
    "商品",
    "订单号",
    "订单状态",
    "商品数量(件)",
    "商品总价(元)",
    "商家实收金额(元)",
}


def _timestamp() -> str:
    """返回用于文件名的本地时间戳，精确到微秒。"""

    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def get_data(db_path: Path | None = None) -> pd.DataFrame:
    """读取商品订单数据。"""

    db_path = db_path or DATABASE_PATH
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    try:
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query("SELECT * FROM commodity_order", connection)
    except sqlite3.Error as exc:
        raise RuntimeError(f"读取 commodity_order 表失败: {exc}") from exc


def _safe_divide(
    numerator: pd.Series, denominator: pd.Series | float | int
) -> pd.Series:
    """按元素除法，分母为零时返回 0。"""

    if isinstance(denominator, pd.Series):
        denominator = denominator.replace(0, np.nan)
    elif denominator == 0:
        denominator = np.nan
    return numerator.div(denominator).fillna(0)


def _safe_change(current: pd.Series, previous: pd.Series) -> pd.Series:
    """计算变化率，历史值为零时返回 0。"""

    return _safe_divide(current - previous, previous)


def _multi_item_statistics(df: pd.DataFrame) -> pd.DataFrame:
    """生成多件商品订单的描述统计，保持参考脚本的列名。"""

    multi_item = df[df["商品数量(件)"] > 1]
    if multi_item.empty:
        return pd.DataFrame(
            columns=[
                "商品",
                "one_order_multi_items_count",
                "one_order_multi_items_mean",
                "one_order_multi_items_std",
                "one_order_multi_items_min",
                "one_order_multi_items_25%",
                "one_order_multi_items_50%",
                "one_order_multi_items_75%",
                "one_order_multi_items_max",
            ]
        )

    result = (
        multi_item.groupby("商品", as_index=False)["商品数量(件)"]
        .describe()
        .rename(
            columns={
                "count": "one_order_multi_items_count",
                "mean": "one_order_multi_items_mean",
                "std": "one_order_multi_items_std",
                "min": "one_order_multi_items_min",
                "25%": "one_order_multi_items_25%",
                "50%": "one_order_multi_items_50%",
                "75%": "one_order_multi_items_75%",
                "max": "one_order_multi_items_max",
            }
        )
    )
    return result


def analyze_orders(
    df: pd.DataFrame,
    time_period: str,
    *,
    csv_dir: Path | None = None,
) -> pd.DataFrame:
    """计算商品销售统计并保留中间 CSV 输出。"""

    if time_period not in TIME_PERIOD_DAYS:
        choices = ", ".join(TIME_PERIOD_DAYS)
        raise ValueError(f"time_period 必须是 {choices} 之一")

    missing_columns = REQUIRED_COLUMNS - set(df.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"commodity_order 缺少必要字段: {missing}")
    if df.empty:
        raise ValueError("commodity_order 表没有订单数据")

    data = df.copy()
    data["order_date"] = pd.to_datetime(
        data["订单号"].astype("string").str[:6],
        format="%y%m%d",
        errors="coerce",
    )
    data = data.dropna(subset=["order_date"]).sort_values(
        by="order_date", ignore_index=True
    )
    if data.empty:
        raise ValueError("订单号中没有可解析的日期")

    max_date = data["order_date"].max()
    days = TIME_PERIOD_DAYS[time_period]
    data = data[
        (data["order_date"] >= max_date - pd.Timedelta(days=days))
        & (data["order_date"] <= max_date)
    ].copy()
    if data.empty:
        raise ValueError(f"{time_period} 时间范围内没有订单数据")

    data["order_status"] = data["订单状态"].isin(VALID_STATUSES).map(
        {True: "valid", False: "invalid"}
    )
    total = data.groupby("商品", as_index=False).agg(
        total_order_num=("订单号", "nunique"),
        total_order_sales=("商品总价(元)", "sum"),
        total_order_actual_income=("商家实收金额(元)", "sum"),
        per_goods_sales_start_date=("order_date", "min"),
    )
    valid = (
        data[data["order_status"] == "valid"]
        .groupby("商品", as_index=False)
        .agg(
            valid_order_num=("订单号", "nunique"),
            valid_order_sales=("商品总价(元)", "sum"),
            valid_order_actual_income=("商家实收金额(元)", "sum"),
        )
    )
    statistic = total.merge(valid, how="left", on="商品")
    statistic[
        ["valid_order_num", "valid_order_sales", "valid_order_actual_income"]
    ] = statistic[
        ["valid_order_num", "valid_order_sales", "valid_order_actual_income"]
    ].fillna(0)

    recent_7d = data[data["order_date"] > max_date - pd.Timedelta(days=7)]
    recent_14d = data[data["order_date"] > max_date - pd.Timedelta(days=14)]
    one_item = (
        data[data["商品数量(件)"] == 1]
        .groupby("商品")["订单号"]
        .nunique()
    )
    statistic["per_goods_total_order_num_percentage"] = _safe_divide(
        statistic["total_order_num"], statistic["total_order_num"].sum()
    )
    statistic["per_goods_sales_period_days"] = (
        max_date - statistic["per_goods_sales_start_date"]
    ).dt.days
    statistic["valid_order_num_percentage"] = _safe_divide(
        statistic["valid_order_num"], statistic["total_order_num"]
    )
    statistic["valid_order_sales_percentage"] = _safe_divide(
        statistic["valid_order_sales"], statistic["total_order_sales"]
    )
    statistic["valid_order_actual_income_percentage"] = _safe_divide(
        statistic["valid_order_actual_income"],
        statistic["total_order_actual_income"],
    )
    statistic["recent_7days_order_num"] = statistic["商品"].map(
        recent_7d.groupby("商品")["订单号"].nunique()
    ).fillna(0)
    statistic["recent_7days_order_num_percentage"] = _safe_divide(
        statistic["recent_7days_order_num"], statistic["total_order_num"]
    )
    statistic["recent_14days_order_num"] = statistic["商品"].map(
        recent_14d.groupby("商品")["订单号"].nunique()
    ).fillna(0)
    statistic["recent_14days_order_num_percentage"] = _safe_divide(
        statistic["recent_14days_order_num"], statistic["total_order_num"]
    )
    statistic["one_order_one_item_percentage"] = _safe_divide(
        statistic["商品"].map(one_item).fillna(0), statistic["total_order_num"]
    )
    statistic = statistic.merge(
        _multi_item_statistics(data), how="left", on="商品"
    )

    # 按最近日期向前编号，兼容只有一到两周数据的短周期分析。
    total_weeks = max(1, (max_date - data["order_date"].min()).days // 7 + 1)
    data["week_num"] = "week_" + (
        total_weeks - (max_date - data["order_date"]).dt.days // 7
    ).astype(str)
    week_pivot = pd.pivot_table(
        data=data,
        values="订单号",
        index="商品",
        columns="week_num",
        aggfunc="nunique",
        sort=False,
    )
    statistic = statistic.merge(week_pivot, how="left", on="商品")
    week_cols = sorted(
        (column for column in statistic.columns if re.fullmatch(r"week_\d+", column)),
        key=lambda column: int(column.removeprefix("week_")),
    )
    statistic[week_cols] = statistic[week_cols].fillna(0)
    statistic["week_std"] = statistic[week_cols].apply(np.std, axis=1)
    latest = statistic[week_cols[-1]]
    previous = statistic[week_cols[-2]] if len(week_cols) >= 2 else latest
    statistic["one_week_diff"] = _safe_change(latest, previous)
    last_two = statistic[week_cols[-2:]].sum(axis=1)
    previous_two = (
        statistic[week_cols[-4:-2]].sum(axis=1)
        if len(week_cols) >= 4
        else pd.Series(0, index=statistic.index)
    )
    last_four = statistic[week_cols[-4:]].sum(axis=1)
    previous_four = (
        statistic[week_cols[-8:-4]].sum(axis=1)
        if len(week_cols) >= 8
        else pd.Series(0, index=statistic.index)
    )
    statistic["two_weeks_diff"] = _safe_change(last_two, previous_two)
    statistic["four_weeks_diff"] = _safe_change(last_four, previous_four)
    statistic.fillna(0, inplace=True)

    output_dir = (csv_dir or WORKSPACE_ROOT / "output" / "csv")
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"goods_order_analysis_{_timestamp()}.csv"
    statistic.to_csv(output_dir / filename, index=False, encoding="utf-8")
    return statistic


def _format_number(value: object, digits: int = 2) -> str:
    """格式化 Markdown 中的数值。"""

    number = float(value)
    if digits == 0:
        return str(int(round(number)))
    return f"{number:.{digits}f}"


def to_markdown(statistic: pd.DataFrame, time_period: str) -> str:
    """将统计结果转换为面向 MCP 调用方的 Markdown。"""

    lines = [
        "# 销售数据分析",
        "",
        f"- 分析周期：`{time_period}`",
        f"- 商品数：{len(statistic)}",
        f"- 订单数：{int(statistic['total_order_num'].sum())}",
        f"- 商品销售额：{_format_number(statistic['total_order_sales'].sum())} 元",
        f"- 商家实收金额：{_format_number(statistic['total_order_actual_income'].sum())} 元",
        "",
        "## 商品明细",
        "",
        "| 商品 | 订单数 | 销售额（元） | 实收金额（元） | 有效订单占比 | 近7天订单 | 近14天订单 | 单件订单占比 | 周环比 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in statistic.sort_values(
        ["total_order_sales", "商品"], ascending=[False, True]
    ).iterrows():
        lines.append(
            "| {goods} | {orders} | {sales} | {income} | {valid:.2%} | {recent7} | "
            "{recent14} | {single:.2%} | {wow:.2%} |".format(
                goods=str(row["商品"]).replace("|", "\\|"),
                orders=_format_number(row["total_order_num"], 0),
                sales=_format_number(row["total_order_sales"]),
                income=_format_number(row["total_order_actual_income"]),
                valid=float(row["valid_order_num_percentage"]),
                recent7=_format_number(row["recent_7days_order_num"], 0),
                recent14=_format_number(row["recent_14days_order_num"], 0),
                single=float(row["one_order_one_item_percentage"]),
                wow=float(row["one_week_diff"]),
            )
        )
    return "\n".join(lines)


def _write_markdown_output(markdown: str, output_dir: Path) -> Path:
    """将最终 Markdown 结果写入指定目录。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"goods_order_analysis_{_timestamp()}.md"
    output_path = output_dir / filename
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def commodity_sale_analysis(time_period: TimePeriod) -> str:
    """分析销售数据并返回 Markdown。

    参数 `time_period` 仅接受 `recent7days`、`recent30days`、`recent90days`。
    CSV 统计结果写入 `output/csv`，Markdown 结果写入 `output/md`。
    """

    statistic = analyze_orders(get_data(), time_period)
    markdown = to_markdown(statistic, time_period)
    _write_markdown_output(markdown, WORKSPACE_ROOT / "output" / "md")
    return markdown
