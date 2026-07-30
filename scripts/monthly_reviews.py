"""
每月自動撈 11 家店的 Google 評論、算漲跌、叫 Claude 寫回饋，寄成一封信。
由 .github/workflows/monthly-reviews.yml 每月觸發一次執行。
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date

SUPABASE_URL = "https://zsowgiobylacwabjjvub.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inpzb3dnaW9ieWxhY3dhYmpqdnViIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM5Mzg4NDMsImV4cCI6MjA5OTUxNDg0M30.SvAd1SgRl1oAt4wNTPuXHeiP4omRcFAb-S8DY5EJvEw"

GOOGLE_KEY = os.environ["GOOGLE_PLACES_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
RESEND_KEY = os.environ["RESEND_API_KEY"]
REPORT_TO = os.environ.get("REPORT_TO_EMAIL") or "ijs7594@gmail.com"


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


def get_last_snapshot(store_id):
    url = (
        f"{SUPABASE_URL}/rest/v1/store_review_snapshots"
        f"?store_id=eq.{store_id}&order=snapshot_date.desc&limit=1"
    )
    rows = http_json(url, headers=supa_headers())
    return rows[0] if rows else None


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
        last = get_last_snapshot(s["id"])
        delta_rating = round(rating - last["rating"], 2) if last and last.get("rating") is not None else None
        delta_count = count - last["user_rating_count"] if last and last.get("user_rating_count") is not None else None
        save_snapshot(s["id"], top["id"], rating, count)
        collected.append({
            "store": s,
            "matched_name": top["displayName"]["text"],
            "rating": rating,
            "count": count,
            "delta_rating": delta_rating,
            "delta_count": delta_count,
            "_has_history": last is not None,
            "reviews": [
                {
                    "rating": rv.get("rating"),
                    "text": (rv.get("text") or {}).get("text", ""),
                    "time": rv.get("relativePublishTimeDescription"),
                }
                for rv in details.get("reviews", [])
            ],
        })
    return collected


def build_prompt(collected):
    lines = ["以下是今年貴焿古早味麵線羹本月各分店的 Google 評論資料，請幫忙寫月報。\n"]
    for c in collected:
        name = c["store"]["name"]
        if c.get("error"):
            lines.append(f"【{name}】{c['error']}\n")
            continue
        no_new_reviews = c["delta_count"] == 0 and c["_has_history"]
        delta_r = f"{c['delta_rating']:+}" if c["delta_rating"] is not None else "無上月資料"
        delta_c = f"{c['delta_count']:+}" if c["delta_count"] is not None else ""
        lines.append(f"【{name}】目前 {c['rating']} 分（{delta_r}），共 {c['count']} 則（{delta_c}）")
        if no_new_reviews:
            lines.append("  本月則數沒有增加，代表這個月沒有新評論，不要引用下面列的舊評論內容當作本月回饋依據。")
        for rv in c["reviews"]:
            lines.append(f"  - [{rv['rating']}星 {rv['time']}] {rv['text'][:200]}")
        lines.append("")
    lines.append(
        "請用繁體中文輸出兩個部分，用 <manager> 跟 <exec> 這兩個 XML 標籤包起來：\n"
        "1. <manager> 標籤內：每家店各一句話，給該店店長看的具體本月回饋，"
        "根據評論內容指出這個月最該讚美或最該改善的一件事，語氣直接不要客套。"
        "如果該店本月沒有新評論（我有特別註明的），這句話就只講「本月無新評論」，"
        "不要翻舊帳去引用之前的評論內容或問題，除非我有另外要求做舊帳回顧。\n"
        "2. <exec> 標籤內：給執行長跟督導看的整體趨勢摘要，"
        "只根據本月真的有新增的評論來判斷退步/進步和共通問題模式，"
        "沒有新評論的店不用列入這段討論。"
        "內容要分成三段，各自用小標題開頭（純文字小標題，不用再包標籤）：\n"
        "「整體趨勢」：2-3 句，哪幾家分數在退步、哪幾家在進步。\n"
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
