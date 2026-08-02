"""
电费查询脚本
流程:
  1. GET 页面获取 __VIEWSTATE
  2. 模拟 UpdatePanel 异步回发 (tels TextChanged)
  3. 从响应中提取新 __VIEWSTATE 并 Base64 解码
  4. 解析剩余电量和住户信息, 保存 JSON
"""

import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from lxml import etree

import requests

# ---------- 配置 ----------
URL = "http://house.i8oa.com/cpaydianfei.aspx"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "dianfei.json")

# 手机号: 环境变量优先 (GitHub Secret), 本地测试可硬编码
PHONE = os.environ.get("PHONE_NUMBER", "").strip() or "19042120337"

# 微信推送配置 (可选, 不配置则不推送)
# PushPlus: 去 https://www.pushplus.plus/ 获取 Token
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
# Server酱 Turbo: 去 https://sct.ftqq.com/ 获取 SendKey
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()

# ---------- 请求头 ----------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
AJAX_HEADERS = {
    **HEADERS,
    "X-MicrosoftAjax": "Delta=true",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


def xpath_attr(tree: etree._Element, xpath: str, attr: str, default: str = "") -> str:
    results = tree.xpath(xpath)
    return results[0].get(attr, default) if results else default


def parse_viewstate(viewstate_b64: str) -> dict:
    """解码 ViewState 提取电费信息"""
    decoded = base64.b64decode(viewstate_b64).decode("utf-8", errors="replace")

    info = {
        "raw_decoded": decoded,
        "tenant": "",
        "address": "",
        "room": "",
        "balance_kwh": None,  # 剩余电量 (度)
    }

    # 提取中文文本（桥接 ViewState 中分割的文本段）
    # ViewState 里文本是被序列化对象分隔的，用正则提取连续的中文块
    text_blocks = re.findall(r"[\u4e00-\u9fff\uff00-\uffef][\u4e00-\u9fff\uff00-\uffef\w\d\s\.\,\;\:\/\(\)\-\<\>br\#\@\!\~\?\！\，\。\：\；\、\%\+\=]+", decoded)

    full_text = "".join(text_blocks).strip()
    # 处理 <br/> 标签
    full_text = full_text.replace("<br/>", " / ").replace("<br>", " / ")

    info["full_text"] = full_text

    # 解析剩余电量: "剩余电量:40.44度" 或 "剩余电量：40.44度"
    kwh_match = re.search(r"剩余电量[：:]\s*(\d+\.?\d*)", full_text)
    if kwh_match:
        info["balance_kwh"] = float(kwh_match.group(1))

    # 解析住户名 (冒号前的内容, 通常是 "姓名:xxx" 前缀)
    # 格式: "林竞庭:砂子塘社区新3栋1单元401 桔子运营-4号房270926"
    tenant_match = re.match(r"^([^：:地址栋单元号房\d]+)", full_text)
    if tenant_match:
        info["tenant"] = tenant_match.group(1).strip("：:")

    # 解析房间号
    room_match = re.search(r"(\d+[号楼栋单元号房]+[\d\-\s]*)", full_text)
    if room_match:
        info["room"] = room_match.group(1).strip()

    # 解析地址
    addr_match = re.search(r"([\u4e00-\u9fff]+社区[\u4e00-\u9fff\s\d栋单元]+)", full_text)
    if addr_match:
        info["address"] = addr_match.group(1).strip()

    # 错误检测
    info["is_error"] = any(kw in full_text for kw in ["没有入住", "不存在", "错误", "失败"])

    return info


# ---------- 微信推送 ----------
def push_pushplus(info: dict) -> bool:
    """通过 PushPlus 推送微信消息"""
    kwh = info.get("balance_kwh")
    emoji = "🟢" if kwh and kwh >= 80 else ("🟡" if kwh and kwh >= 30 else "🔴")
    kwh_str = f"{kwh} 度" if kwh else "未知"

    title = f"{emoji} 电费查询: 剩余 {kwh_str}"
    content = f"""<h3>⚡ 电费查询结果</h3>
<p><b>住户:</b> {info.get('tenant', '未知')}</p>
<p><b>地址:</b> {info.get('address', '未知')}</p>
<p><b>剩余电量:</b> <font color="{'red' if kwh and kwh < 30 else 'green'}">{kwh_str}</font></p>
<p><b>查询时间:</b> {info.get('updated_at', '')}</p>
<hr><p style="color:#999;font-size:12px">{info.get('full_text', '')}</p>"""

    try:
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"},
            timeout=15,
        )
        data = r.json()
        if data.get("code") == 200:
            print("[推送] PushPlus 发送成功")
            return True
        else:
            print(f"[推送] PushPlus 失败: {data}")
            return False
    except Exception as e:
        print(f"[推送] PushPlus 异常: {e}")
        return False


def push_serverchan(info: dict) -> bool:
    """通过 Server酱 Turbo 推送微信消息"""
    kwh = info.get("balance_kwh")
    emoji = "🟢" if kwh and kwh >= 80 else ("🟡" if kwh and kwh >= 30 else "🔴")
    kwh_str = f"{kwh} 度" if kwh else "未知"

    title = f"{emoji} 电费查询: 剩余 {kwh_str}"
    desp = f"""## ⚡ 电费查询结果

| 项目 | 内容 |
|------|------|
| 住户 | {info.get('tenant', '未知')} |
| 地址 | {info.get('address', '未知')} |
| 剩余电量 | **{kwh_str}** |
| 查询时间 | {info.get('updated_at', '')} |

> {info.get('full_text', '')}
"""

    try:
        r = requests.post(
            f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
            data={"title": title, "desp": desp},
            timeout=15,
        )
        data = r.json()
        if data.get("code") == 0:
            print("[推送] Server酱 发送成功")
            return True
        else:
            print(f"[推送] Server酱 失败: {data}")
            return False
    except Exception as e:
        print(f"[推送] Server酱 异常: {e}")
        return False


def send_wechat_notify(result: dict):
    """尝试微信推送, 支持 PushPlus 和 Server酱"""
    if PUSHPLUS_TOKEN:
        push_pushplus(result)
    elif SERVERCHAN_KEY:
        push_serverchan(result)
    else:
        print("[推送] 未配置推送服务, 跳过")


def main():
    if not PHONE:
        print("[ERROR] 未设置 PHONE_NUMBER 环境变量")
        sys.exit(1)

    print(f"[{datetime.now(timezone.utc).isoformat()}] 查询电费中...")
    print(f"  手机号: {PHONE[:3]}****{PHONE[-3:]}")

    s = requests.Session()

    # Step 1: GET 获取初始 ViewState
    r1 = s.get(URL, headers=HEADERS, timeout=30)
    r1.raise_for_status()
    tree1 = etree.HTML(r1.text)
    vs = xpath_attr(tree1, '//input[@id="__VIEWSTATE"]', "value")
    vg = xpath_attr(tree1, '//input[@id="__VIEWSTATEGENERATOR"]', "value")
    if not vs or not vg:
        print("[ERROR] 无法获取 VIEWSTATE")
        sys.exit(1)

    # Step 2: 模拟 UpdatePanel 异步回发 — 触发 tels TextChanged
    data = {
        "ScriptManager": "UpdatePanel2|tels",
        "__EVENTTARGET": "tels",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": vs,
        "__VIEWSTATEGENERATOR": vg,
        "__ASYNCPOST": "true",
        "tels": PHONE,
        "dianfei": "50",
        "txtVCode": "",
    }
    r2 = s.post(URL, data=data, headers=AJAX_HEADERS, timeout=30)
    r2.raise_for_status()

    # Step 3: 解析 AJAX 响应, 提取新 ViewState
    # 格式: size|type|id|content|... (管道符分隔)
    # 新 ViewState 格式: NNN|hiddenField|__VIEWSTATE|BASE64_VALUE|
    body = r2.text
    new_vs_match = re.search(r"\|hiddenField\|__VIEWSTATE\|([^|]+)\|", body)
    if not new_vs_match:
        print(f"[ERROR] 无法从 AJAX 响应中提取 ViewState")
        print(f"  响应片段: {body[:300]}")
        sys.exit(1)

    new_vs = new_vs_match.group(1)

    # Step 4: 解码 ViewState 提取信息
    info = parse_viewstate(new_vs)

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "phone_masked": f"{PHONE[:3]}****{PHONE[-3:]}",
        **info,
    }

    # 移除 raw_decoded (太大, 不存档)
    del result["raw_decoded"]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[OK] 数据已保存至 {OUTPUT_FILE}")
    print(f"  住户: {info.get('tenant', '未知')}")
    print(f"  地址: {info.get('address', '未知')} {info.get('room', '')}")
    print(f"  剩余电量: {info.get('balance_kwh', '?')} 度")

    # 微信推送
    send_wechat_notify(result)

    if info["is_error"]:
        print(f"  [WARN] 可能存在错误: {info.get('full_text', '')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
