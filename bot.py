# Новые комментарии YouTube-канала → перевод на русский → Telegram.
# Запускается по расписанию в GitHub Actions. Ключи берутся из секретов репозитория:
#   YT_API_KEY  — ключ YouTube Data API v3
#   TG_TOKEN    — токен бота от @BotFather
#   TG_CHAT_ID  — ваш chat id в Telegram
#   CHANNEL_ID  — id канала (по умолчанию Las Cuentas de Paco)
# Что уже отправлено, хранится в state.json (его коммитит сам workflow).
import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from deep_translator import GoogleTranslator

YT = "https://www.googleapis.com/youtube/v3"
STATE = Path(__file__).with_name("state.json")
CHANNEL_ID = os.environ.get("CHANNEL_ID") or "UCqACQmQwpU8UWRsXrD4fh7g"
YT_KEY = os.environ.get("YT_API_KEY", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
PAGES = 2          # сколько страниц по 100 веток смотреть за запуск
MAX_SEEN = 5000    # сколько id помнить


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save_state(st: dict) -> None:
    st["seen"] = st["seen"][-MAX_SEEN:]
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def yt(path: str, **params) -> dict:
    r = requests.get(f"{YT}/{path}", params={**params, "key": YT_KEY}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"YouTube API {path}: {r.status_code} {r.text[:300]}")
    return r.json()


def fetch_threads() -> list[dict]:
    out, token = [], None
    for _ in range(PAGES):
        p = dict(part="snippet,replies", allThreadsRelatedToChannelId=CHANNEL_ID,
                 order="time", maxResults=100, textFormat="plainText")
        if token:
            p["pageToken"] = token
        data = yt("commentThreads", **p)
        out += data.get("items", [])
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def all_replies(thread_id: str) -> list[dict]:
    out, token = [], None
    while True:
        p = dict(part="snippet", parentId=thread_id, maxResults=100, textFormat="plainText")
        if token:
            p["pageToken"] = token
        data = yt("comments", **p)
        out += data.get("items", [])
        token = data.get("nextPageToken")
        if not token:
            return out


def titles_for(ids: set[str], cache: dict) -> dict:
    need = [i for i in ids if i not in cache]
    for k in range(0, len(need), 50):
        data = yt("videos", part="snippet", id=",".join(need[k:k + 50]))
        for v in data.get("items", []):
            cache[v["id"]] = v["snippet"]["title"]
    return cache


def _deepl(text: str) -> str:
    key = os.environ["DEEPL_KEY"]
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    r = requests.post(f"https://{host}/v2/translate", timeout=30, headers={"Authorization": f"DeepL-Auth-Key {key}"},
                      data={"text": text, "target_lang": "RU"})
    r.raise_for_status()
    return r.json()["translations"][0]["text"]


def _google(text: str) -> str:
    return GoogleTranslator(source="auto", target="ru").translate(text[:4500])


def _mymemory(text: str) -> str:
    p = {"q": text[:500], "langpair": "es|ru"}
    if os.environ.get("MYMEMORY_EMAIL"):
        p["de"] = os.environ["MYMEMORY_EMAIL"]  # с почтой лимит 50 000 знаков в день вместо 5 000
    d = requests.get("https://api.mymemory.translated.net/get", params=p, timeout=30).json()
    out = (d.get("responseData") or {}).get("translatedText") or ""
    if d.get("responseStatus") != 200 or "MYMEMORY WARNING" in out:
        raise RuntimeError(out or str(d)[:200])
    return out


def to_ru(text: str) -> str | None:
    # по очереди: DeepL (если есть ключ) → Google → MyMemory; если все отказали — шлём оригинал
    chain = ([_deepl] if os.environ.get("DEEPL_KEY") else []) + [_google, _mymemory]
    for f in chain:
        try:
            out = f(text)
            if out and out.strip():
                return html.unescape(out)
        except Exception as e:
            print(f"перевод {f.__name__} не удался: {str(e)[:120]}", file=sys.stderr)
    return None


def tg_send(text: str) -> None:
    for attempt in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", timeout=30, json={
            "chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        if r.status_code == 200:
            return
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        raise RuntimeError(f"Telegram: {r.status_code} {r.text[:300]}")


def message(c: dict, title: str, parent_author: str | None) -> str:
    s = c["snippet"]
    e = html.escape
    text = s.get("textOriginal") or s.get("textDisplay") or ""
    ru = to_ru(text)
    head = f"💬 <b>{e(s.get('authorDisplayName', '?'))}</b>"
    if parent_author:
        head += f" ↪️ в ветке <b>{e(parent_author)}</b>"
    likes = s.get("likeCount", 0)
    lines = [head, f"🎬 <i>{e(title[:90])}</i>", ""]
    if ru and ru.strip().lower() != text.strip().lower():
        lines += [f"🇷🇺 {e(ru)}", "", f"<blockquote expandable>🇪🇸 {e(text)}</blockquote>"]
    else:
        lines += [e(text)]
    url = f"https://www.youtube.com/watch?v={s.get('videoId', '')}&lc={c['id']}"
    lines += [f'<a href="{url}">Открыть на YouTube</a>' + (f" · 👍 {likes}" if likes else "")]
    msg = "\n".join(lines)
    return msg if len(msg) < 4000 else msg[:3990] + "…"


def main() -> None:
    if not (YT_KEY and TG_TOKEN and TG_CHAT):
        sys.exit("Нет секретов: нужны YT_API_KEY, TG_TOKEN, TG_CHAT_ID")
    st = load_state()
    first_run = "seen" not in st
    st.setdefault("seen", [])
    st.setdefault("replyCount", {})
    st.setdefault("titles", {})
    seen = set(st["seen"])

    threads = fetch_threads()
    new: list[tuple[dict, str | None]] = []  # (комментарий, автор ветки — если это ответ)
    for t in threads:
        top = t["snippet"]["topLevelComment"]
        top_author = top["snippet"].get("authorDisplayName")
        replies = t.get("replies", {}).get("comments", [])
        total = t["snippet"].get("totalReplyCount", 0)
        # в ленте приходит не больше 5 ответов — если ветка выросла, догружаем её целиком
        if total > len(replies) and st["replyCount"].get(t["id"]) != total:
            replies = all_replies(t["id"])
        st["replyCount"][t["id"]] = total
        for c, parent in [(top, None)] + [(r, top_author) for r in replies]:
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            st["seen"].append(c["id"])
            if c["snippet"].get("authorChannelId", {}).get("value") == CHANNEL_ID:
                continue  # свои ответы не присылаем
            new.append((c, parent))

    if first_run:
        tg_send(f"✅ Бот запущен. Слежу за комментариями канала, сейчас их {len(seen)} — "
                "присылать буду только новые.")
    else:
        new.sort(key=lambda x: x[0]["snippet"].get("publishedAt", ""))
        titles_for({c["snippet"].get("videoId", "") for c, _ in new} - {""}, st["titles"])
        for c, parent in new:
            tg_send(message(c, st["titles"].get(c["snippet"].get("videoId", ""), ""), parent))
            time.sleep(1.1)
    print(f"веток: {len(threads)}, новых комментариев: {0 if first_run else len(new)}")

    # раз в сутки меняем дату, чтобы GitHub не отключил расписание за «неактивность» репозитория
    st["day"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_state(st)


if __name__ == "__main__":
    main()
