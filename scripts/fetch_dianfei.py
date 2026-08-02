"""
电费查询脚本 - 支持历史记录
流程:
  1. GET 页面获取 __VIEWSTATE
  2. 模拟 UpdatePanel 异步回发
  3. 解码 ViewState 提取电费信息
  4. 保存最新数据 + 追加历史记录
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
LATEST_FILE = os.path.join(OUTPUT_DIR, "dianfei.json")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "history.json")
MAX_HISTORY = 90  # 最多保留 90 条记录

PHONE = os.environ.get("PHONE_NUMBER", "").strip() or "19042120337"
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()

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


def xpath_attr(tree, xpath, attr, default=""):
    results = tree.xpath(xpath)
    return results[0].get(attr, default) if results else default


def parse_viewstate(viewstate_b64):
    decoded = base64.b64decode(viewstate_b64).decode("utf-8", errors="replace")
    info = {"tenant": "", "address": "", "room": "", "balance_kwh": None}

    text_blocks = re.findall(
        r"[\u4e00-\u9fff\uff00-\uffef][\u4e00-\u9fff\uff00-\uffef\w\d\s\.\,\;\:\/\(\)\-\<\>br\#\@\!\~\?\！\，\。\：\；\、\%\+\=]+",
        decoded,
    )
    full_text = "".join(text_blocks).strip()
    full_text = full_text.replace("<br/>", " / ").replace("<br>", " / ")
    info["full_text"] = full_text

    kwh_match = re.search(r"剩余电量[：:]\s*(\d+\.?\d*)", full_text)
    if kwh_match:
        info["balance_kwh"] = float(kwh_match.group(1))

    tenant_match = re.match(r"^([^：:地址栋单元号房\d]+)", full_text)
    if tenant_match:
        info["tenant"] = tenant_match.group(1).strip("：:")

    room_match = re.search(r"(\d+[号楼栋单元号房]+[\d\-\s]*)", full_text)
    if room_match:
        info["room"] = room_match.group(1).strip()

    addr_match = re.search(r"([\u4e00-\u9fff]+社区[\u4e00-\u9fff\s\d栋单元]+)", full_text)
    if addr_match:
        info["address"] = addr_match.group(1).strip()

    info["is_error"] = any(kw in full_text for kw in ["没有入住", "不存在", "错误", "失败"])
    return info


def load_history():
    """加载历史记录"""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history):
    """保存历史记录，限制条数"""
    history = history[-MAX_HISTORY:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    return history


def push_pushplus(info):
    kwh = info.get("balance_kwh")
    emoji = "🟢" if kwh and kwh >= 80 else ("🟡" if kwh and kwh >= 30 else "🔴")
    kwh_str = f"{kwh} 度" if kwh else "未知"
    title = f"{emoji} 电费查询: 剩余 {kwh_str}"
    content = f"""<h3>⚡ 电费查询结果</h3>
<p><b>住户:</b> {info.get('tenant', '?')}</p>
<p><b>剩余电量:</b> <font color="{'red' if kwh and kwh < 30 else 'green'}">{kwh_str}</font></p>
<p><b>地址:</b> {info.get('address', '?')}</p>
<p><b>时间:</b> {info.get('updated_at', '')}</p>
<hr><p style="color:#999;font-size:12px">{info.get('full_text', '')}</p>"""
    try:
        r = requests.post("https://www.pushplus.plus/send",
            json={"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"}, timeout=15)
        if r.json().get("code") == 200:
            print("[推送] PushPlus 成功")
    except Exception as e:
        print(f"[推送] PushPlus 异常: {e}")


def push_serverchan(info):
    kwh = info.get("balance_kwh")
    emoji = "🟢" if kwh and kwh >= 80 else ("🟡" if kwh and kwh >= 30 else "🔴")
    kwh_str = f"{kwh} 度" if kwh else "未知"
    title = f"{emoji} 电费查询: 剩余 {kwh_str}"
    desp = f"""## ⚡ 电费查询结果
| 项目 | 内容 |
|------|------|
| 住户 | {info.get('tenant', '?')} |
| 剩余电量 | **{kwh_str}** |
| 地址 | {info.get('address', '?')} |
| 时间 | {info.get('updated_at', '')} |
> {info.get('full_text', '')}"""
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
            data={"title": title, "desp": desp}, timeout=15)
        if r.json().get("code") == 0:
            print("[推送] Server酱 成功")
    except Exception as e:
        print(f"[推送] Server酱 异常: {e}")


def send_wechat_notify(result):
    if PUSHPLUS_TOKEN:
        push_pushplus(result)
    elif SERVERCHAN_KEY:
        push_serverchan(result)
    else:
        print("[推送] 未配置, 跳过")


def main():
    if not PHONE:
        print("[ERROR] 未设置 PHONE_NUMBER", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] 查询电费...")
    print(f"  手机号: {PHONE[:3]}****{PHONE[-3:]}")

    s = requests.Session()

    # Step 1: GET 获取 ViewState
    r1 = s.get(URL, headers=HEADERS, timeout=30)
    r1.raise_for_status()
    tree1 = etree.HTML(r1.text)
    vs = xpath_attr(tree1, '//input[@id="__VIEWSTATE"]', "value")
    vg = xpath_attr(tree1, '//input[@id="__VIEWSTATEGENERATOR"]', "value")
    if not vs:
        print("[ERROR] 无法获取 VIEWSTATE", file=sys.stderr)
        sys.exit(1)

    # Step 2: 异步回发
    r2 = s.post(URL, data={
        "ScriptManager": "UpdatePanel2|tels",
        "__EVENTTARGET": "tels", "__EVENTARGUMENT": "",
        "__VIEWSTATE": vs, "__VIEWSTATEGENERATOR": vg,
        "__ASYNCPOST": "true",
        "tels": PHONE, "dianfei": "50", "txtVCode": "",
    }, headers=AJAX_HEADERS, timeout=30)
    r2.raise_for_status()

    # Step 3: 提取新 ViewState
    new_vs_match = re.search(r"\|hiddenField\|__VIEWSTATE\|([^|]+)\|", r2.text)
    if not new_vs_match:
        print(f"[ERROR] 无法解析 ViewState: {r2.text[:200]}", file=sys.stderr)
        sys.exit(1)

    # Step 4: 解码
    info = parse_viewstate(new_vs_match.group(1))

    timestamp = now.isoformat().replace("+00:00", "Z")
    result = {
        "updated_at": timestamp,
        "phone_masked": f"{PHONE[:3]}****{PHONE[-3:]}",
        "tenant": info["tenant"],
        "address": info["address"],
        "room": info["room"],
        "balance_kwh": info["balance_kwh"],
        "full_text": info["full_text"],
        "is_error": info["is_error"],
    }

    # 保存最新数据
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 追加历史记录（去重: 同一小时内同数值不重复记录）
    history = load_history()
    should_append = True
    if history:
        last = history[-1]
        last_dt = datetime.fromisoformat(last["time"])
        if (now - last_dt).total_seconds() < 3600 and last.get("kwh") == info["balance_kwh"]:
            should_append = False

    if should_append:
        history.append({
            "time": timestamp,
            "kwh": info["balance_kwh"],
            "text": info.get("full_text", ""),
        })
        history = save_history(history)

    # 在 latest 中也附带历史摘要
    result["history"] = [{"time": h["time"], "kwh": h["kwh"]} for h in history]
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[OK] 剩余电量: {info.get('balance_kwh', '?')} 度")
    print(f"  历史记录: {len(history)} 条")
    print(f"  住户: {info.get('tenant', '?')}")
    print(f"  地址: {info.get('address', '?')}")

    send_wechat_notify(result)

    if info["is_error"]:
        print(f"[WARN] {info.get('full_text', '')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
