import os
import re

import numpy as np
import pandas as pd

from datetime import datetime, timezone, timedelta
EASTERN_8 = timezone(timedelta(hours=8))
TIMESTAMP = datetime.now(EASTERN_8)
TODAY = TIMESTAMP.strftime("%Y%m%d")
YESTERDAY = (TIMESTAMP - timedelta(days=1)).strftime("%Y-%m-%d")






def insert(file_prefix:str, history:str, today:str)->pd.DataFrame:

    # 构建正则：前缀_当日日期_随机十六进制字符串.xlsx
    pattern = re.compile(
        rf"^{re.escape(file_prefix)}_{today}_[0-9a-zA-Z]+\.xlsx$"
    )

    # 在目录中查找所有匹配的文件
    matched_files = []
    for fname in os.listdir(downloads_dir):
        if pattern.match(fname):
            matched_files.append(fname)

    if not matched_files:
        print(f"在 {downloads_dir} 中未找到匹配的文件")
        return
    else:
        # 如果有多个匹配，按文件修改时间倒序，取最新的一个
        matched_files.sort(
            key=lambda f: os.path.getmtime(os.path.join(downloads_dir, f)),
            reverse=True
        )
        target_filename = matched_files[0]
        full_path = os.path.join(downloads_dir, target_filename)
        print("匹配成功:", target_filename)
        sycm = pd.read_excel(full_path)

        yesterday_idx = (history[history.loc[:,"Date"] == YESTERDAY]).index.start

        history.fillna(value=0, inplace=True)

        history.loc[yesterday_idx, "访客数\nUV"] = sycm.loc[0, "访客数"]
        history.loc[yesterday_idx, "客单价\nATV"] = sycm.loc[0, "客单价"]
        history.loc[yesterday_idx, "买家数\nBuyer"] = sycm.loc[0, "支付买家数"]
        history.loc[yesterday_idx, "成交金额\nSold Val"] = sycm.loc[0, "支付金额"]
        history.loc[yesterday_idx, "成交订单\nSold Order"] = sycm.loc[0, "支付子订单数"]
        history.loc[yesterday_idx, "退款金额\nReturn Val"] = sycm.loc[0, "成功退款金额"]
        history.loc[yesterday_idx, "站内推广花费\nPromotion Expenses"] = sycm.loc[0, "全站推广花费"]

        history["转化率\nCR%"] = history["买家数\nBuyer"] / history["访客数\nUV"]
        history["退款率\nRefund Rate"] = history["退款金额\nReturn Val"] / history["成交金额\nSold Val"]
        history["推广费比\nCost Ratio"] = history["站内推广花费\nPromotion Expenses"] / history["成交金额\nSold Val"]
        history["Completion Rate\n完成率(%)"] = history["成交金额\nSold Val"] / history["Turnover TARGET\n销售额目标"]
        history["净销金额\nNet Val"] = history["成交金额\nSold Val"] - history["退款金额\nReturn Val"] - history["刷单金额\nFake Val"]

        history["sortingDate"] = pd.to_datetime(history["Date"], format="%Y-%m-%d")
        history.sort_values(by="sortingDate", inplace=True)

        history.to_csv("/Users/fersk/Downloads/xbtc-daily-sales-data.csv", index=False, encoding="utf-8-sig")
        return history


def preview(data:pd.DataFrame, today:str)->pd.DataFrame:

    RATE_COLS = {
        "Completion Rate\n完成率(%)",
        "转化率\nCR%",
        "退款率\nRefund Rate",
        "推广费比\nCost Ratio",
    }

    SKIP_COLS = {"Date", "sortingDate"}

    data["Date"] = (
        data["sortingDate"].dt.strftime("%Y-%m-%d")
        + " "
        + data["sortingDate"].dt.day_name().str[:3].str.capitalize()
        )
    
    def fmt(val, col_name):
        # 1. 跳过列：日期类
        if col_name in SKIP_COLS:
            return val

        # 2. 跳过空值、日期、字符串
        if isinstance(val, (pd.Timestamp, datetime, str)) or pd.isna(val):
            return val

        # 3. 跳过 inf / -inf（否则 int(inf) 会 OverflowError）
        if isinstance(val, (int, float, np.number)) and not np.isfinite(val):
            return val

        # 4. 率值列 → xx%
        if col_name in RATE_COLS:
            return f"{int(round(float(val), 2) * 100)}%"

        # 5. 其余数值列 → int
        return int(val)

    data = data.replace([np.inf, -np.inf], np.nan)
    data = data.apply(lambda col: col.map(lambda v: fmt(v, col.name)))

    truncated_idx = (data[data["sortingDate"] == YESTERDAY]).index.start
    data.iloc[:truncated_idx+1, :-1].to_csv(f"/Users/fersk/Downloads/xbtc-daily-sales-preview-{today}.csv", index=False, encoding="utf-8-sig")



if __name__=="__main__":

    file_prefix = "xbtc-sycm"

    downloads_dir = "/Users/fersk/Downloads/"

    history = pd.read_csv("/Users/fersk/Downloads/xbtc-daily-sales-data.csv")

    period = [date.strftime("%Y%m%d") for date in pd.date_range(start=pd.Timestamp("20260922"), end=pd.Timestamp("20261008"), freq="D")]

    print(period)

    for date in period:

        data = insert(file_prefix=file_prefix, history=history, today=date)

        preview(data=data, today=date)



