#!/usr/bin/env python3
"""
Mississauga Library – Swim/Skate Pass 可借监控 → Discord

用法:
  export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
  python monitor.py            # 检查一次（配合 cron / GitHub Actions 定时跑）
  python monitor.py --loop 300 # 本地常驻，每 300 秒检查一次
  python monitor.py --test     # 无论状态都推送一条当前状态，用来测试 webhook

可选环境变量:
  BIB_ID          默认 S220C769335（Swim/Skate Pass）
  BRANCH_FILTER   只关心某些分馆，逗号分隔的关键词，例如 "Central,Meadowvale"
  STATE_FILE      状态文件路径，默认 state.json
"""
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

LIBRARY = "mississauga"
BIB_ID = os.environ.get("BIB_ID", "S220C769335")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
BRANCH_FILTER = [s.strip().lower() for s in os.environ.get("BRANCH_FILTER", "").split(",") if s.strip()]

RECORD_URL = f"https://{LIBRARY}.bibliocommons.com/v2/record/{BIB_ID}"
API_URL = f"https://gateway.bibliocommons.com/v2/libraries/{LIBRARY}/bibs/{BIB_ID}/availability"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal availability monitor)",
    "Accept": "application/json, text/html;q=0.9",
}

AVAILABLE_WORDS = ("available", "on shelf", "in")  # 匹配 item 级别状态


def _item_is_available(item: dict) -> bool:
    av = item.get("availability") or {}
    status_type = str(av.get("statusType") or av.get("status") or item.get("status") or "").upper()
    if status_type in ("AVAILABLE", "ON_SHELF", "IN"):
        return True
    lib_status = str(av.get("libraryStatus") or "").lower()
    return lib_status in ("available", "in", "on shelf", "checked in")


def _branch_name(item: dict) -> str:
    br = item.get("branch") or {}
    return br.get("name") or item.get("branchName") or br.get("code") or "未知分馆"


def check_via_api():
    """调用 BiblioCommons 前端用的 gateway 接口。返回 dict 或抛异常。"""
    r = requests.get(API_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    ents = data.get("entities", {})

    summary = (ents.get("bibs", {}).get(BIB_ID, {}) or {}).get("availability", {}) or {}
    items = list((ents.get("bibItems") or {}).values())

    available_branches = sorted({_branch_name(i) for i in items if _item_is_available(i)})
    if BRANCH_FILTER:
        available_branches = [b for b in available_branches if any(k in b.lower() for k in BRANCH_FILTER)]

    if items:
        is_available = bool(available_branches)
    else:  # 没有 item 明细时退回到总览字段
        is_available = (summary.get("availableCopies") or 0) > 0 or str(summary.get("status", "")).upper() == "AVAILABLE"

    return {
        "available": is_available,
        "branches": available_branches,
        "available_copies": summary.get("availableCopies"),
        "total_copies": summary.get("totalCopies"),
        "holds": summary.get("heldCopies"),
        "source": "api",
    }


def check_via_html():
    """备用方案：抓记录页面，看是否出现 'All copies in use'。"""
    r = requests.get(RECORD_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    html = r.text
    in_use = re.search(r"All copies in use", html, re.I) is not None
    avail = re.search(r"\bAvailable\b", html) is not None and not in_use
    return {"available": avail, "branches": [], "available_copies": None,
            "total_copies": None, "holds": None, "source": "html"}


def check():
    try:
        return check_via_api()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] API 失败，改用页面检查: {e}", file=sys.stderr)
        return check_via_html()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def notify(result, title):
    if not WEBHOOK:
        print("[warn] 没有设置 DISCORD_WEBHOOK_URL，跳过推送", file=sys.stderr)
        return
    lines = []
    if result["branches"]:
        lines.append("**可借分馆：** " + "、".join(result["branches"]))
    if result["available_copies"] is not None:
        lines.append(f"可借 {result['available_copies']} / 共 {result['total_copies']} 份")
    lines.append(f"[打开借阅页面]({RECORD_URL})")
    payload = {
        "username": "Swim/Skate Pass 监控",
        "embeds": [{
            "title": title,
            "url": RECORD_URL,
            "description": "\n".join(lines),
            "color": 0x2ECC71 if result["available"] else 0x95A5A6,
            "footer": {"text": f"来源: {result['source']} · {datetime.now():%Y-%m-%d %H:%M}"},
        }],
    }
    r = requests.post(WEBHOOK, json=payload, timeout=20)
    r.raise_for_status()


def run_once(force=False):
    result = check()
    state = load_state()
    prev_available = state.get("available")
    prev_branches = state.get("branches", [])
    print(f"{datetime.now():%H:%M:%S} available={result['available']} branches={result['branches']} ({result['source']})")

    if force:
        notify(result, "🧪 测试：当前状态 " + ("可借 ✅" if result["available"] else "全部借出 ⛔"))
    elif result["available"] and (not prev_available or set(result["branches"]) - set(prev_branches)):
        notify(result, "🏊⛸️ Swim/Skate Pass 有了！赶紧去借")
    elif prev_available and not result["available"]:
        notify(result, "Swim/Skate Pass 又被借光了")

    save_state({"available": result["available"], "branches": result["branches"],
                "checked_at": datetime.now().isoformat(timespec="seconds")})


def main():
    args = sys.argv[1:]
    if "--test" in args:
        run_once(force=True)
    elif "--loop" in args:
        interval = int(args[args.index("--loop") + 1]) if len(args) > args.index("--loop") + 1 else 300
        while True:
            try:
                run_once()
            except Exception as e:  # noqa: BLE001
                print(f"[error] {e}", file=sys.stderr)
            time.sleep(interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
