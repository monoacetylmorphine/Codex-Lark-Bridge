# -*- coding: utf-8 -*-
import pandas as pd

source_order = pd.read_csv("/Users/renxiaoyao/Documents/ExportOrderList26954244167.csv")
goods = pd.read_csv("/Users/renxiaoyao/Documents/【鲜拔头筹】天猫货盘最新_260806.csv")

order = source_order.copy()

presale = order['分阶段信息'].str.rstrip(';').str.split(';', expand=True)
presale.columns = ['阶段1', '阶段2']
order = pd.concat([order, presale], axis=1)

mask = (
    (order["订单状态"]!="交易关闭") &
    (order["买家实付金额"]!=0 ) &
    (~order['阶段2'].str.contains('交易状态等待买家付款', na=False))
)
order = order[mask]


order = order[["子订单编号", "主订单编号", "商品标题", "商品属性", "订单状态", "购买数量", "买家实付金额", "商品ID", "分阶段信息"]]

goods = goods[["商品Id", "sku", "总成本"]]

goods.rename(columns={'商品Id': '商品ID', 'sku':'商品属性'}, inplace=True)
goods["总成本"] = pd.to_numeric(goods["总成本"], errors="coerce")

goods.dropna(subset=['总成本'], inplace=True)
goods.dropna(subset=['商品ID'], inplace=True)

order["子订单编号"] = order["子订单编号"].astype(int).astype(str)
order["主订单编号"] = order["主订单编号"].astype(int).astype(str)

order["商品ID"] = order["商品ID"].astype(int).astype(str)
goods["商品ID"] = goods["商品ID"].astype(int).astype(str)

goods["商品属性"] = goods["商品属性"].astype(str)
goods["总成本"] = goods["总成本"].astype(float)


prefixes = ["商品规格:", "莱瑞-145", "口味:", "净含量:", "具体规格:", ";", "（26年3-4月）", "，", "规格:"]

for df in [order, goods]:
    for prefix in prefixes:
        df["商品属性"] = df["商品属性"].str.replace(f"{prefix}", "", regex=True)


goods["商品属性"] = goods["商品属性"].str.replace("瘦腰段400g【切片2盒装】", "低脂中段400g【切片2盒装】", regex=False)
goods["商品属性"] = goods["商品属性"].str.replace("法罗皇冠中段600g【切片3盒装】", "法罗皇冠中段600g【切片3盒装】200g", regex=False)
goods["商品属性"] = goods["商品属性"].str.replace("挪威轮切三文鱼排1000g【2袋装】", "挪威轮切三文鱼1000g【2袋装】", regex=False)
goods["商品属性"] = goods["商品属性"].str.replace("挪威轮切三文鱼排2000g【4袋装】", "挪威轮切三文鱼2000g【4袋装】", regex=False)
goods["商品属性"] = goods["商品属性"].str.replace("去壳甜虾【100g大规格｜30尾×2板】送芥末酱油", "去壳甜虾【100g大规格｜30尾×2板】（200g）", regex=False)

order["商品属性"] = order["商品属性"].str.replace("净含量(不含冰):", "", regex=False)
order["商品属性"] = order["商品属性"].str.replace("尺寸:", "/", regex=False)
order["商品属性"] = order["商品属性"].str.replace("[", "", regex=False)
order["商品属性"] = order["商品属性"].str.replace("]", "", regex=False)

order["商品ID"] = order["商品ID"].str.replace("1020877136029", "1048728782787", regex=False)
order["商品属性"] = order["商品属性"].str.replace("挪威轮切三文鱼排2000g【4袋装】", "挪威轮切三文鱼2000g【4袋装】", regex=False)
order["商品属性"] = order["商品属性"].str.replace("轮切三文鱼4斤【4包装】", "挪威轮切三文鱼2000g【4袋装】", regex=False)

goods["商品属性"] = goods["商品属性"].str.replace('\u200b', '', regex=True)
goods["商品属性"] = goods["商品属性"].str.replace(r'\s+', '', regex=True)
order["商品属性"] = order["商品属性"].str.replace('\u200b', '', regex=True)
order["商品属性"] = order["商品属性"].str.replace(r'\s+', '', regex=True)

merged = pd.merge(left=order, right=goods, how="left", on=["商品ID","商品属性"])
merged["毛利"] = round(merged["买家实付金额"] - merged["总成本"] * merged["购买数量"], 2)
merged.to_csv("/Users/renxiaoyao/Documents/订单毛利-20260805.csv", index=False, encoding="utf-8-sig")

print(order.shape)
print(merged.shape)

source_order["子订单编号"] = source_order["子订单编号"].astype(int).astype(str)
source_order = pd.merge(left=source_order, right=merged[["子订单编号", "总成本", "毛利"]], how="left", on="子订单编号")
source_order.to_csv("/Users/renxiaoyao/Documents/订单毛利计算-20260805.csv", index=False, encoding="utf-8-sig")

print(source_order.shape)