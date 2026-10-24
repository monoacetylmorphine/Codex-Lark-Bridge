import sqlite3
from pathlib import Path
import re
from unittest.mock import patch
import numpy as np
import pandas as pd
from mcp_service.tools.internal_tools.commodity_inventory_info import commodity_inventory_info, get_goods_inventory, to_markdown

def _orders() -> pd.DataFrame:
    return pd.DataFrame([{'商品': '商品A', '订单号': '260101-001', '商品数量(件)': 2}, {'商品': '商品A', '订单号': '260103-002', '商品数量(件)': 3}, {'商品': '商品B', '订单号': '260103-003', '商品数量(件)': 1}])

def _goods_info() -> pd.DataFrame:
    return pd.DataFrame([{'商品名称': '商品A', '商品状态': '现货', '参考零售价': 10.0, '单箱采购折扣': 0.5, '箱装商品数': 12, '备注': None}, {'商品名称': '商品B', '商品状态': '现货', '参考零售价': 20.0, '单箱采购折扣': 0.6, '箱装商品数': 6, '备注': '测试'}])

def test_inventory_keeps_reference_columns_and_writes_timestamped_csv(tmp_path: Path) -> None:
    csv_dir = tmp_path / 'csv'
    with patch('mcp_service.tools.internal_tools.commodity_inventory_info.np.random.randint', side_effect=[np.array([3, 3]), np.array([30, 30])]):
        inventory = get_goods_inventory(_orders(), csv_dir=csv_dir)
    assert inventory.columns.tolist() == ['商品', 'total_sales_num', 'per_goods_sales_start_date', 'per_goods_sales_period_days', 'daily_sales_average', 'current_inventory_num', 'inventory_available_days']
    assert inventory['total_sales_num'].tolist() == [5, 1]
    files = list(csv_dir.glob('goods_inventory_info_*.csv'))
    assert len(files) == 1
    assert re.fullmatch('goods_inventory_info_\\d{8}_\\d{6}_\\d{6}', files[0].stem)

def test_tool_reads_database_and_writes_markdown_and_csv(tmp_path: Path) -> None:
    root = tmp_path
    database = root / 'database.db'
    with sqlite3.connect(database) as connection:
        _orders().to_sql('commodity_order', connection, index=False)
        _goods_info().to_sql('commodity_info', connection, index=False)
    with patch('mcp_service.tools.internal_tools.commodity_inventory_info.DATABASE_PATH', database), patch('mcp_service.tools.internal_tools.commodity_inventory_info.WORKSPACE_ROOT', root), patch('mcp_service.tools.internal_tools.commodity_inventory_info.np.random.randint', side_effect=[np.array([3, 3]), np.array([30, 30])]):
        result = commodity_inventory_info()
    assert result.startswith('# 商品库存信息')
    assert '商品A' in result
    csv_files = list((root / 'output' / 'csv').glob('*.csv'))
    markdown_files = list((root / 'output' / 'md').glob('*.md'))
    assert len(csv_files) == 1
    assert len(markdown_files) == 1
    assert re.fullmatch('goods_inventory_info_\\d{8}_\\d{6}_\\d{6}', markdown_files[0].stem)
    assert markdown_files[0].read_text(encoding='utf-8') == result

def test_markdown_contains_purchase_information(tmp_path: Path) -> None:
    inventory = get_goods_inventory(_orders(), csv_dir=tmp_path)
    info = _goods_info().assign(商品=lambda frame: frame['商品名称'], single_box_purchase_price=lambda frame: frame['参考零售价'] * frame['箱装商品数'])
    markdown = to_markdown(info.merge(inventory, on='商品'))
    assert '单箱采购价' in markdown
    assert '商品A' in markdown
