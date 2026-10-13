# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright

COOKIE_STRING = "_samesite_flag_=true; cookie2=1d7bc42b02ae9d94dbf38861c735cc69; t=dc179df465d034faa7b150ec4593bbf8; unb=2222826177323; cancelledSubSites=empty; thw=cn; _tb_token_=f5b6153e96bae; xlly_s=1; 3PcFlag=1786278800364; sgcookie=E1002aErBqX2dypPACkQpnt%2Frg6BJQV7rCFblBjC09KiGrFzzrAh65AO6UiNRQRbXu7110Q0Lfrl7bolEkayUwdW3MusVgUCMhegqOigg%2FwLdxzXd2AuM6SfbLDSK09bOgdk; sn=%E9%B2%9C%E6%8B%94%E5%A4%B4%E7%AD%B9%E6%97%97%E8%88%B0%3A%E9%AD%8F%E5%B7%8D; uc1=cookie14=UoYWO6okos3BsQ%3D%3D&cookie21=URm48syIZx9a; csg=ccf54997; _cc_=VT5L2FSpdA%3D%3D; skt=e638b51686d1413c; cna=cLXwIu9m8QsCAXTmYq4epUwH; v=0; mtop_partitioned_detect=1; _m_h5_tk=494da86e5122d8a165afc4f1ece1ec4b_1786515194566; _m_h5_tk_enc=b68ed778248be678e2be6ce9323a3361; tfstk=h4-IZWsJ_SVPZMjrSozCkXjHKgS89Q5PaLCRd1fdLLJRegO9avfeeXAWV1ddTwkHe86J_1qPU2cuVh9k6vqrEM7JV1pAa6hoKTIWTCHZgmoiXcG1q_nP2PqE7TS-gjoZ0pvhqBoF0r8LWOCGFkULJQI9B95Op_BJw1QOHtPd2gdJBA1GegCJwgH1X1XR2_dJwdQWQhh41TZebuXtU9_CGu5_2Ox1dw1xm1ZWKhTke72FGcNO_LpDyNt7kqskQEOWyCH82_TOhI62TcEC2Fd1YnEzJAM6rzc3oSvBomTM4wBGdNksCWLlJOX1pAM_rzbdI96tCANpr"


def parse_cookie(cookie_str):
    """将 'key=value; key2=value2' 解析为列表字典"""
    cookies = []
    for item in cookie_str.split(';'):
        item = item.strip()
        if '=' in item:
            key, value = item.split('=', 1)
            cookies.append({'name': key, 'value': value, 'domain': '.taobao.com', 'path': '/'})
    return cookies

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # 显示界面，方便调试
    context = browser.new_context()
    page = context.new_page()
    
    # 注入 Cookie
    cookies = parse_cookie(COOKIE_STRING)
    context.add_cookies(cookies)
    
    # 访问目标页面（订单详情页）
    # order_id = "5127181851305089633"  # 你要查询的订单号
    # url = f"https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId={order_id}"
    url = "https://sycm.taobao.com/portal/home.htm"
    page.goto(url)
    button_selector = "#SYCM_hBIfJ97P > div > div > div.pro-layout-card-header > div > div.pro-layout-card-header__ft > div > div.filter.sycm-filter-v2.filter-top-left > div > div > div > div > div.oui-date-picker-menu-v2.oui-date-picker-particle-button > button:nth-child(2)"
    button = page.locator(button_selector)
    option_selector = "text=今天"
    button.click()
    
    input("按任意键关闭浏览器...")
    browser.close()



# import requests
# from bs4 import BeautifulSoup, NavigableString, Tag
# import json

# def soup_to_dict(element):
#     """递归地将 BeautifulSoup 元素转换为字典/列表结构"""
#     if isinstance(element, NavigableString):
#         # 纯文本节点
#         return str(element).strip() or None  # 忽略空文本

#     if isinstance(element, Tag):
#         # 标签节点
#         result = {
#             "tag": element.name,
#             "attrs": element.attrs,          # 属性字典
#             "children": []
#         }
#         for child in element.children:
#             child_data = soup_to_dict(child)
#             if child_data is not None:       # 跳过空文本
#                 result["children"].append(child_data)
#         return result

# headers = {
#     'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
#     'Cookie': COOKIE_STRING
# }

# url = 'https://qn.taobao.com/home.htm/trade-platform/tp/sold'

# session = requests.Session()
# response = session.get(url, headers=headers)

# if response.status_code == 200:
#     soup = BeautifulSoup(response.text, 'html.parser')
#     tree_dict = soup_to_dict(soup)  # 根节点通常是整个文档
#     json_str = json.dumps(tree_dict, ensure_ascii=False, indent=2)
#     with open("/Users/renxiaoyao/Downloads/t.json", "w", encoding="utf-8") as f:
#         json.dump(tree_dict, f, ensure_ascii=False, indent=2)

    
# else:
#     print(f'请求失败，状态码：{response.status_code}')