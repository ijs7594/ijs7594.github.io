"""
每月自動撈 11 家店的 Google 評論、算漲跌、叫 Claude 寫回饋，寄成一封信。
由 .github/workflows/monthly-reviews.yml 每月觸發一次執行。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

SUPABASE_URL = "https://zsowgiobylacwabjjvub.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inpzb3dnaW9ieWxhY3dhYmpqdnViIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM5Mzg4NDMsImV4cCI6MjA5OTUxNDg0M30.SvAd1SgRl1oAt4wNTPuXHeiP4omRcFAb-S8DY5EJvEw"

GOOGLE_KEY = os.environ["GOOGLE_PLACES_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
RESEND_KEY = os.environ["RESEND_API_KEY"]
REPORT_TO = os.environ.get("REPORT_TO_EMAIL") or "ijs7594@gmail.com"


def load_known_constraints():
    path = os.path.join(os.path.dirname(__file__), "known_constraints.txt")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]


def http_json(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"User-Agent": "Mozilla/5.0 (compatible; guijiao-monthly-report/1.0)"}
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} calling {url}: {e.read().decode(errors='replace')}", file=sys.stderr)
        raise


def supa_headers():
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }


def get_stores():
    url = f"{SUPABASE_URL}/rest/v1/stores?select=id,name,address,status,opened_date"
    return http_json(url, headers=supa_headers())


def get_snapshots(store_id, limit=6):
    url = (
        f"{SUPABASE_URL}/rest/v1/store_review_snapshots"
        f"?store_id=eq.{store_id}&order=snapshot_date.desc&limit={limit}"
    )
    return http_json(url, headers=supa_headers())


def rating_tier(rating):
    if rating is None:
        return None
    if rating < 4.0:
        return "待加強"
    if rating < 4.5:
        return "穩定"
    return "標竿"


def save_snapshot(store_id, place_id, rating, count):
    url = f"{SUPABASE_URL}/rest/v1/store_review_snapshots"
    body = {
        "store_id": store_id,
        "place_id": place_id,
        "rating": rating,
        "user_rating_count": count,
        "snapshot_date": date.today().isoformat(),
    }
    http_json(url, method="POST", headers=supa_headers(), body=body)


def search_place(query):
    res = http_json(
        "https://places.googleapis.com/v1/places:searchText",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_KEY,
            "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
        },
        body={"textQuery": query, "languageCode": "zh-TW"},
    )
    places = res.get("places", [])
    return places[0] if places else None


def place_details(place_id):
    return http_json(
        f"https://places.googleapis.com/v1/places/{place_id}?languageCode=zh-TW",
        headers={
            "X-Goog-Api-Key": GOOGLE_KEY,
            "X-Goog-FieldMask": "id,displayName,rating,userRatingCount,reviews",
        },
    )


def parse_iso(ts):
    ts = ts.replace("Z", "+00:00")
    # Google 有時回傳奈秒級精度（小數點後 9 位），但 Python fromisoformat
    # 最多只吃微秒（6 位），超過的話直接截斷，不然會整支程式炸掉。
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    return datetime.fromisoformat(ts)


def previous_month_start(today, months_back=1):
    y, m = today.year, today.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    return datetime(y, m, 1, tzinfo=timezone.utc)


# 正常每月排程用 1（只回顧上個月）。手動觸發 workflow 時可以調大這個數字，
# 一次把還沒被報告過的月份（例如系統剛建置時錯過的六、七月）補進同一份報告。
LOOKBACK_MONTHS = int(os.environ.get("REVIEW_LOOKBACK_MONTHS", "1"))


def collect_store_data():
    collected = []
    for s in get_stores():
        if s["status"] == "籌備中":
            continue
        query = f"貴焿古早味麵線羹 {s['name']} {s['address']}"
        top = search_place(query)
        if not top:
            collected.append({"store": s, "error": "找不到 Google 商家頁面"})
            continue
        details = place_details(top["id"])
        rating = details.get("rating")
        count = details.get("userRatingCount")
        snapshots = get_snapshots(s["id"])
        last = snapshots[0] if snapshots else None
        delta_rating = round(rating - last["rating"], 2) if last and last.get("rating") is not None else None
        delta_count = count - last["user_rating_count"] if last and last.get("user_rating_count") is not None else None
        # 用「上個月月初」當作評論回顧的分界，而不是「上次程式跑到哪」——
        # 這樣就算系統才剛建置、快照歷史很短，第一次跑也能完整涵蓋上個月的評論，
        # 不會因為「上次執行時間」剛好是昨天，就把上個月的評論當成「不夠新」而濾掉。
        cutoff = previous_month_start(datetime.now(timezone.utc), LOOKBACK_MONTHS)

        # 拉長時間軸看趨勢：找最舊的一筆快照（最多回溯 6 次，約半年），
        # 這樣即使當月完全沒有新評論，也能看出評分是不是在慢慢下滑/上升。
        oldest = snapshots[-1] if snapshots else None
        trend_months = None
        delta_rating_trend = None
        delta_count_trend = None
        if oldest is not None and oldest is not last:
            span_days = (date.today() - date.fromisoformat(oldest["snapshot_date"])).days
            trend_months = max(1, round(span_days / 30))
            if oldest.get("rating") is not None:
                delta_rating_trend = round(rating - oldest["rating"], 2)
            if oldest.get("user_rating_count") is not None:
                delta_count_trend = count - oldest["user_rating_count"]
        all_reviews = [
            {
                "rating": rv.get("rating"),
                "text": (rv.get("text") or {}).get("text", ""),
                "time": rv.get("relativePublishTimeDescription"),
                "publish_time": rv.get("publishTime"),
            }
            for rv in details.get("reviews", [])
        ]
        new_reviews = [
            rv for rv in all_reviews
            if rv["publish_time"] and parse_iso(rv["publish_time"]) >= cutoff
        ]
        save_snapshot(s["id"], top["id"], rating, count)
        collected.append({
            "store": s,
            "matched_name": top["displayName"]["text"],
            "rating": rating,
            "count": count,
            "tier": rating_tier(rating),
            "delta_rating": delta_rating,
            "delta_count": delta_count,
            "trend_months": trend_months,
            "delta_rating_trend": delta_rating_trend,
            "delta_count_trend": delta_count_trend,
            "_has_history": last is not None,
            "reviews": new_reviews,
        })
    return collected


def build_prompt(collected):
    period_desc = "上個月" if LOOKBACK_MONTHS == 1 else f"最近 {LOOKBACK_MONTHS} 個月"
    lines = [f"以下是今年貴焿古早味麵線羹{period_desc}各分店的 Google 評論資料，請幫忙寫月報。\n"]
    constraints = load_known_constraints()
    if constraints:
        lines.append(
            "以下是總部已經明確決定、不會因為顧客評論而調整的既定政策。"
            "評論內容如果只是在抱怨這些，不要放進「可用SOP解決的系統性問題」或"
            "「需要店長／夥伴自我檢查的問題」這兩段去要求改善，也不要建議店長回應顧客時道歉或承諾會改，"
            "當作已有共識的既定政策看待就好：\n"
            + "\n".join(f"- {c}" for c in constraints)
            + "\n"
        )
    for c in collected:
        name = c["store"]["name"]
        if c.get("error"):
            lines.append(f"【{name}】{c['error']}\n")
            continue
        delta_r = f"{c['delta_rating']:+}" if c["delta_rating"] is not None else "無上月資料"
        delta_c = f"{c['delta_count']:+}" if c["delta_count"] is not None else ""
        lines.append(f"【{name}】目前 {c['rating']} 分（{delta_r}），共 {c['count']} 則（{delta_c}），評分等級：{c['tier']}")
        if c["trend_months"]:
            trend_r = f"{c['delta_rating_trend']:+}" if c["delta_rating_trend"] is not None else "無資料"
            trend_c = f"{c['delta_count_trend']:+}" if c["delta_count_trend"] is not None else "無資料"
            lines.append(f"  近 {c['trend_months']} 個月變化：評分 {trend_r}、則數 {trend_c}（這是真實數字，即使上個月沒有新評論也可以拿來討論長期趨勢）")
        if c["reviews"]:
            lines.append(
                f"  以下是 Google 目前顯示的代表性評論中，發布時間落在{period_desc}的 {len(c['reviews'])} 則"
                f"（Google 每家店只回傳最多 5 則代表性評論，不保證涵蓋{period_desc}全部評論，"
                "但這幾則的內容和發布時間都是真實的，可以直接引用討論）："
            )
        else:
            lines.append(
                f"  Google 目前顯示的代表性評論中，沒有一則是{period_desc}發布的"
                "（可能是真的沒有新評論，也可能是有新評論但沒被 Google 選進代表性名單裡，"
                "系統無法分辨這兩種情況）。不要杜撰或引用更早之前的評論內容當作上個月的回饋依據。"
            )
        for rv in c["reviews"]:
            lines.append(f"  - [{rv['rating']}星 {rv['time']}] {rv['text'][:200]}")
        lines.append("")
    lines.append(
        "請用繁體中文輸出兩個部分，用 <manager> 跟 <exec> 這兩個 XML 標籤包起來：\n"
        "1. <manager> 標籤內：每家店各一句話，給該店店長看的具體上月回饋，"
        "根據評論內容指出上個月最該讚美或最該改善的一件事，語氣直接不要客套。"
        "如果我有特別註明該店沒有任何一則代表性評論落在上個月，這句話就只講「上個月沒有可回顧的新評論」；"
        "不要翻舊帳去引用更早之前的評論內容或問題，除非我有另外要求做舊帳回顧。\n"
        "2. <exec> 標籤內：給執行長跟督導看的整體趨勢摘要。"
        "「系統性問題」和「自我檢查」這兩段只能根據上個月真的有拿到的評論內容來判斷，"
        "沒有拿到評論內容的店不用列入這兩段；"
        "但「整體趨勢」段不受此限——每家店我都有附上近幾個月的評分/則數變化（這是真實數字，"
        "不是評論內容），可以直接拿來討論長期趨勢和退步/進步，即使該店上個月沒有拿到評論內容也要看這個數字。"
        "內容要分成三段，各自用小標題開頭（純文字小標題，不用再包標籤）：\n"
        "「整體趨勢」：2-3 句，根據近幾個月的評分/則數變化，哪幾家在退步、哪幾家在進步、"
        "哪幾家評分明顯低於其他店（低於4.0分）需要留意。\n"
        "「可用 SOP 解決的系統性問題」：列出跨店重複出現、屬於制度或流程層級的問題"
        "（例如好幾家都有一樣的抱怨、加購定價不透明、包裝標準不一致這種可以寫成書面規範來源頭解決的），"
        "每項註明是哪些店，如果本月沒有這類問題就寫「本月無」。\n"
        "「需要店長／夥伴自我檢查的問題」：列出屬於個別人員態度、當下應對方式、"
        "個人責任心的問題（例如某店店員不耐煩、甩餐、算錯錢這種不是制度缺失、"
        "而是當下那個人沒做好的），每項註明是哪家店，如果本月沒有就寫「本月無」。\n"
        "不要輸出任何其他文字。"
    )
    return "\n".join(lines)


def ask_claude(prompt):
    res = http_json(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        body={
            "model": "claude-sonnet-5",
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    if res.get("stop_reason") == "max_tokens":
        print("警告：Claude 回覆被 max_tokens 截斷了", file=sys.stderr)
    return "".join(b["text"] for b in res["content"] if b.get("type") == "text")


def extract_tag(text, tag):
    start = text.find(f"<{tag}>")
    end = text.find(f"</{tag}>")
    if start == -1 or end == -1:
        return "（AI 回覆格式跑掉了，這段沒有抓到內容）"
    return text[start + len(tag) + 2:end].strip()


def build_email_html(collected, ai_text):
    manager_part = extract_tag(ai_text, "manager").replace("\n", "<br>")
    exec_part = extract_tag(ai_text, "exec").replace("\n", "<br>")

    rows = ""
    standing_rows = ""
    ranked = sorted(
        (c for c in collected if not c.get("error")),
        key=lambda x: x.get("rating") or 0,
    )
    for rank, c in enumerate(ranked, start=1):
        name = c["store"]["name"]
        below_line = c["rating"] is not None and c["rating"] < 4.0
        trend_note = "—"
        if c["trend_months"] and c["delta_rating_trend"] is not None:
            arrow = "↓" if c["delta_rating_trend"] < 0 else ("↑" if c["delta_rating_trend"] > 0 else "→")
            trend_note = f"{arrow} 近{c['trend_months']}個月 {c['delta_rating_trend']:+}"
        style = " style='color:#b3261e;font-weight:bold;'" if below_line else ""
        standing_rows += (
            f"<tr><td>{rank}</td><td{style}>{name}</td><td{style}>{c['rating']}（{c['tier']}）</td>"
            f"<td>{trend_note}</td></tr>"
        )

    for c in sorted(collected, key=lambda x: x.get("rating") or 0):
        name = c["store"]["name"]
        if c.get("error"):
            rows += f"<tr><td>{name}</td><td colspan='3'>{c['error']}</td></tr>"
            continue
        delta_r = f"{c['delta_rating']:+}" if c["delta_rating"] is not None else "—"
        rows += (
            f"<tr><td>{name}</td><td>{c['rating']}</td>"
            f"<td>{delta_r}</td><td>{c['count']}</td></tr>"
        )

    return f"""
    <div style="font-family:-apple-system,sans-serif;max-width:640px;margin:0 auto;color:#3a2a1c;">
      <h2 style="color:#7a3b1e;">今年貴焿 {date.today().strftime('%Y年%m月')} 評論月報</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        <tr style="background:#f4e9dc;text-align:left;">
          <th style="padding:6px;">店</th><th style="padding:6px;">評分</th>
          <th style="padding:6px;">較上月</th><th style="padding:6px;">則數</th>
        </tr>
        {rows}
      </table>
      <h3 style="color:#7a3b1e;">目前各店排名（不論本月有沒有新評論，每個月都看得到）</h3>
      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        <tr style="background:#f4e9dc;text-align:left;">
          <th style="padding:6px;">名次</th><th style="padding:6px;">店</th>
          <th style="padding:6px;">評分</th><th style="padding:6px;">長期趨勢</th>
        </tr>
        {standing_rows}
      </table>
      <p style="font-size:13px;color:#8a6d5a;">紅字＝評分低於 4.0，建議優先關注。長期趨勢是跟最早有紀錄的一次比較，會隨著月報累積愈來愈準。</p>
      <h3 style="color:#7a3b1e;">給執行長／督導的整體摘要</h3>
      <p>{exec_part}</p>
      <h3 style="color:#7a3b1e;">給各店店長的本月回饋</h3>
      <p>{manager_part}</p>
    </div>
    """


def send_email(html):
    body = {
        "from": "今年貴焿月報 <onboarding@resend.dev>",
        "to": [REPORT_TO],
        "subject": f"今年貴焿 {date.today().strftime('%Y年%m月')} 評論月報",
        "html": html,
    }
    http_json(
        "https://api.resend.com/emails",
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_KEY}",
            "Content-Type": "application/json",
        },
        body=body,
    )


def main():
    collected = collect_store_data()
    prompt = build_prompt(collected)
    ai_text = ask_claude(prompt)
    html = build_email_html(collected, ai_text)
    send_email(html)
    print("月報寄出完成")


if __name__ == "__main__":
    main()
