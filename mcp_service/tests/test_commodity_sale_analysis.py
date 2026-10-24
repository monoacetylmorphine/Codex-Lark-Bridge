import sqlite3
from pathlib import Path
import re
from unittest.mock import patch
import pandas as pd
import pytest
from mcp_service.tools.internal_tools.commodity_sale_analysis import analyze_orders, commodity_sale_analysis, to_markdown

def _orders() -> pd.DataFrame:
    return pd.DataFrame([{'商品': '商品A', '订单号': '260101-001', '订单状态': '已收货', '商品数量(件)': 1, '商品总价(元)': 10.0, '商家实收金额(元)': 9.0}, {'商品': '商品A', '订单号': '260102-002', '订单状态': '待发货', '商品数量(件)': 2, '商品总价(元)': 20.0, '商家实收金额(元)': 18.0}, {'商品': '商品B', '订单号': '260103-003', '订单状态': '退款成功', '商品数量(件)': 1, '商品总价(元)': 5.0, '商家实收金额(元)': 0.0}])

def test_analyze_orders_writes_csv_and_markdown_without_jsonl(tmp_path: Path) -> None:
    statistic = analyze_orders(_orders(), 'recent7days', csv_dir=tmp_path / 'csv')
    markdown = to_markdown(statistic, 'recent7days')
    assert statistic['商品'].tolist() == ['商品A', '商品B']
    assert '# 销售数据分析' in markdown
    assert '商品A' in markdown
    csv_files = list((tmp_path / 'csv').glob('*.csv'))
    assert len(csv_files) == 1
    assert re.fullmatch('goods_order_analysis_\\d{8}_\\d{6}_\\d{6}', csv_files[0].stem)
    assert not list(tmp_path.glob('**/*.jsonl'))

def test_period_must_be_one_of_three_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='recent7days'):
        analyze_orders(_orders(), 'recent14days', csv_dir=tmp_path)

def test_tool_reads_database_and_returns_markdown(tmp_path: Path) -> None:
    database = tmp_path / 'database.db'
    with sqlite3.connect(database) as connection:
        _orders().to_sql('commodity_order', connection, index=False)
    with patch('mcp_service.tools.internal_tools.commodity_sale_analysis.DATABASE_PATH', database), patch('mcp_service.tools.internal_tools.commodity_sale_analysis.WORKSPACE_ROOT', tmp_path):
        result = commodity_sale_analysis('recent30days')
    assert result.startswith('# 销售数据分析')
    csv_files = list((tmp_path / 'output' / 'csv').glob('*.csv'))
    markdown_files = list((tmp_path / 'output' / 'md').glob('*.md'))
    assert len(csv_files) == 1
    assert len(markdown_files) == 1
    assert re.fullmatch('goods_order_analysis_\\d{8}_\\d{6}_\\d{6}', csv_files[0].stem)
    assert re.fullmatch('goods_order_analysis_\\d{8}_\\d{6}_\\d{6}', markdown_files[0].stem)
    assert markdown_files[0].read_text(encoding='utf-8') == result
    assert not list((tmp_path / 'output' / 'csv').glob('*.md'))
