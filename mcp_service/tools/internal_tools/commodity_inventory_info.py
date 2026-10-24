"""商品库存信息 MCP 工具。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd


DATABASE_PATH: Final[Path] = Path("~/.codex_bridge/database.db").expanduser()
WORKSPACE_ROOT: Final[Path] = Path("~/.codex/workspace").expanduser()
ORDER_REQUIRED_COLUMNS: Final[set[str]] = {"商品", "订单号", "商品数量(件)"}
INFO_REQUIRED_COLUMNS: Final[set[str]] = {
    "商品名称",
    "商品状态",
    "参考零售价",
    "单箱采购折扣",
    "箱装商品数",
    "备注",
}
INVENTORY_COLUMNS: Final[list[str]] = [
    "商品",
    "total_sales_num",
    "per_goods_sales_start_date",
    "per_goods_sales_period_days",
    "daily_sales_average",
    "current_inventory_num",
    "inventory_available_days",
]


def _timestamp() -> str:
    """返回用于文件名的本地时间戳，精确到微秒。"""

    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def get_data(
    db_path: Path | str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """读取商品订单和商品资料数据。"""

    database = Path(db_path).expanduser() if db_path else DATABASE_PATH
    if not database.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {database}")

    try:
        with sqlite3.connect(database) as connection:
            goods_order = pd.read_sql_query(
                "SELECT * FROM commodity_order", connection
            )
            goods_info = pd.read_sql_query(
                "SELECT * FROM commodity_info", connection
            )
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        raise RuntimeError(f"读取商品库存数据失败: {exc}") from exc

    missing_order = ORDER_REQUIRED_COLUMNS - set(goods_order.columns)
    if missing_order:
        raise ValueError(
            f"commodity_order 缺少必要字段: {', '.join(sorted(missing_order))}"
        )
    missing_info = INFO_REQUIRED_COLUMNS - set(goods_info.columns)
    if missing_info:
        raise ValueError(
            f"commodity_info 缺少必要字段: {', '.join(sorted(missing_info))}"
        )
    if goods_order.empty:
        raise ValueError("commodity_order 表没有订单数据")
    if goods_info.empty:
        raise ValueError("commodity_info 表没有商品数据")

    info = goods_info.copy()
    info["single_box_purchase_price"] = (
        pd.to_numeric(info["参考零售价"], errors="raise")
        * pd.to_numeric(info["箱装商品数"], errors="raise")
    )
    info = info.rename(columns={"商品名称": "商品"})
    return goods_order, info


def get_goods_inventory(
    orders: pd.DataFrame,
    *,
    csv_dir: Path | None = None,
) -> pd.DataFrame:
    """根据订单计算库存指标，并模拟当前库存数量。"""

    missing = ORDER_REQUIRED_COLUMNS - set(orders.columns)
    if missing:
        raise ValueError(
            f"订单数据缺少必要字段: {', '.join(sorted(missing))}"
        )

    data = orders.copy()
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

    inventory = data.groupby("商品", as_index=False).agg(
        total_sales_num=("商品数量(件)", "sum"),
        per_goods_sales_start_date=("order_date", "min"),
    )
    max_date = data["order_date"].max()
    inventory["per_goods_sales_period_days"] = (
        max_date - inventory["per_goods_sales_start_date"]
    ).dt.days
    # 单日数据的周期按一天计算，避免日均销量和可售天数出现无穷值。
    sales_days = inventory["per_goods_sales_period_days"].clip(lower=1)
    inventory["daily_sales_average"] = (
        inventory["total_sales_num"] / sales_days
    )

    # 库存仍按原脚本的随机模拟逻辑生成。
    inventory["current_inventory_num"] = (
        inventory["daily_sales_average"] * np.random.randint(3, 7)
        + np.random.randint(30, 60)
    ).astype(int)
    inventory["inventory_available_days"] = np.where(
        inventory["daily_sales_average"] > 0,
        inventory["current_inventory_num"] / inventory["daily_sales_average"],
        0,
    ).astype(int)
    inventory = inventory[INVENTORY_COLUMNS]

    output_dir = csv_dir or WORKSPACE_ROOT / "output" / "csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"goods_inventory_info_{_timestamp()}.csv"
    inventory.to_csv(output_path, index=False, encoding="utf-8")
    return inventory


def to_markdown(summary: pd.DataFrame) -> str:
    """将库存结果转换为面向 MCP 调用方的 Markdown。"""

    lines = [
        "# 商品库存信息",
        "",
        f"- 商品数：{len(summary)}",
        f"- 总销量：{int(summary['total_sales_num'].sum())} 件",
        "",
        "## 商品明细",
        "",
        "| 商品 | 总销量（件） | 销售起始日期 | 销售周期（天） | 日均销量 | 当前库存（件） | 可售天数 | 商品状态 | 单箱采购价 | 单箱折扣 | 单箱商品数 | 备注 |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for _, row in summary.sort_values("商品").iterrows():
        lines.append(
            "| {goods} | {sales} | {start} | {period} | {daily:.2f} | "
            "{current} | {available} | {status} | {price:.2f} | {discount:.2f} | "
            "{box} | {notes} |".format(
                goods=str(row["商品"]).replace("|", "\\|"),
                sales=int(row["total_sales_num"]),
                start=pd.Timestamp(row["per_goods_sales_start_date"]).date(),
                period=int(row["per_goods_sales_period_days"]),
                daily=float(row["daily_sales_average"]),
                current=int(row["current_inventory_num"]),
                available=int(row["inventory_available_days"]),
                status=str(row["商品状态"]).replace("|", "\\|"),
                price=float(row["single_box_purchase_price"]),
                discount=float(row["单箱采购折扣"]),
                box=int(row["箱装商品数"]),
                notes=str(row["备注"]).replace("|", "\\|").replace("\n", " "),
            )
        )
    return "\n".join(lines)


def _write_markdown_output(markdown: str, output_dir: Path) -> Path:
    """将最终 Markdown 结果写入指定目录。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"goods_inventory_info_{_timestamp()}.md"
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def commodity_inventory_info() -> str:
    """分析商品库存并返回 Markdown，同时保存 CSV 和 Markdown 结果。"""

    orders, goods_info = get_data()
    inventory = get_goods_inventory(orders)
    summary = goods_info.merge(inventory, how="left", on="商品")
    summary["备注"] = summary["备注"].fillna("null")
    markdown = to_markdown(summary)
    _write_markdown_output(markdown, WORKSPACE_ROOT / "output" / "md")
    return markdown
