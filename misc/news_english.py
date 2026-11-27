#!/usr/bin/env python3
"""Make the morning English card from last night's Hacker News cards, as a PNG for pixer_upload.py.

  news_english.py CARDS.json -o card.png

Ollama picks a story that leads to a real work situation and writes one sentence a colleague
would say in it. It exits 1 when the cards are not from last night or no story fits, so cron
can fall back to daily_english.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

from daily_english import CHROME, render
from github_trending import OLLAMA, get
from hacker_news import MODEL

MAX_WORDS = 16
LOOSE_ENDS = {"a", "an", "the", "our", "my", "your", "their", "to", "of", "for", "and", "we"}
PICK = {
    "type": "object",
    "properties": {
        "story": {"type": "integer"},
        "scene": {"type": "string"},
        "english": {"type": "string"},
        "cn": {"type": "string"},
        "phrase": {"type": "string"},
        "phrase_cn": {"type": "string"},
    },
    "required": ["story", "scene", "english", "cn", "phrase", "phrase_cn"],
}
SCHEMA = {"type": "object", "properties": {"picks": {"type": "array", "items": PICK, "maxItems": 3}},
          "required": ["picks"]}
SYSTEM = ("你是外企软件团队里的英语教练，学员是中国软件工程师。"
          "你从昨晚的技术新闻出发，教一句他今天在工作中真能说出口的英文。")
ASK = ("从上面的新闻中挑最多 3 条，按合适程度排序，各自引出一个真实的工作场景（站会、PR 评审、排障、事故复盘、技术选型、和经理同步）。"
       "story：新闻编号。scene：中文写场景，一句话，不超过 30 字，必须点出这条新闻，"
       "例如“看到 1Password 事件，你想在周会上提议排查 CI 权限”。"
       "english：一句地道、简短（不超过 14 个词）的英文，同事之间会直接这么说，不要书面腔，不要复述新闻。"
       "cn：这句英文的中文意思。phrase：句子里最值得学的一个地道搭配（如动词短语、习语），2–4 个词，"
       "原样出现在 english 中，能脱离这句话单独记住，不要以 our、the、a、to 等虚词结尾。"
       "phrase_cn：短语的中文释义。如果某条新闻引不出自然的工作场景，就不要选它，宁缺毋滥；都不合适就返回空列表。")


def load_stories(path: Path, today: dt.date) -> list[dict]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    if dt.date.fromisoformat(saved["day"]) != today - dt.timedelta(days=1):
        raise RuntimeError(f"the Hacker News cards are from {saved['day']}, not last night")
    return saved["stories"]


def news_prompt(stories: list[dict]) -> str:
    lines = []
    for n, story in enumerate(stories, 1):
        summary = story["summary"]
        lines.append(f"新闻 {n}：{story['title']}\n  中文：{summary['title']}。{summary['what']}\n"
                     f"  评论：{'；'.join(summary['views'])}")
    return "<news>\n" + "\n".join(lines) + "\n</news>\n\n" + ASK


def find_phrase(phrase: str, english: str) -> re.Match | None:
    """The phrase as whole words in the sentence, so "lock down" does not match inside "block downtime"."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", english, re.IGNORECASE)


def usable(pick: dict, total: int) -> bool:
    texts = [pick.get(key) for key in ("scene", "english", "cn", "phrase", "phrase_cn")]
    if not all(isinstance(t, str) and t.strip() for t in texts):
        return False
    english, phrase = pick["english"].strip(), pick["phrase"].strip()
    # A phrase like "audit our" is not something to remember on its own.
    return (isinstance(pick.get("story"), int) and 1 <= pick["story"] <= total
            and find_phrase(phrase, english) and len(english.split()) <= MAX_WORDS
            and phrase.split()[-1].lower() not in LOOSE_ENDS)


def choose(stories: list[dict], ollama: str, model: str) -> tuple[dict, dict]:
    """The model's first usable pick, stripped, and the story it came from."""
    body = {
        "model": model, "stream": False, "think": False, "format": SCHEMA, "options": {"temperature": 0.4},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": news_prompt(stories)}],
    }
    reply = get(f"{ollama}/api/chat", {"Content-Type": "application/json"}, json.dumps(body).encode(), timeout=300)
    picks = json.loads(json.loads(reply)["message"]["content"]).get("picks")
    for pick in picks if isinstance(picks, list) else []:
        if isinstance(pick, dict) and usable(pick, len(stories)):
            pick = {key: value.strip() if isinstance(value, str) else value for key, value in pick.items()}
            return pick, stories[pick["story"] - 1]
    raise RuntimeError("no story led to a usable workplace sentence")


def card_page(pick: dict, story: dict, day: dt.date) -> str:
    english = pick["english"].replace("'", "’")
    phrase = pick["phrase"].replace("'", "’")
    start, end = find_phrase(phrase, english).span()
    sentence = (html.escape(english[:start]) + f"<u>{html.escape(english[start:end])}</u>"
                + html.escape(english[end:]))
    size = 112 if len(english) <= 60 else 96
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><style>
  html, body {{ margin:0; padding:0; }}
  body {{ width:1872px; height:1404px; background:#fff; color:#111; box-sizing:border-box; padding:110px 140px;
         font-family:"Helvetica Neue",-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif;
         display:flex; flex-direction:column; }}
  .top {{ display:flex; justify-content:space-between; align-items:baseline; font-size:40px;
         letter-spacing:2px; color:#555; }}
  .top .rule {{ flex:1; border-bottom:3px solid #222; margin:0 40px 14px; }}
  .news {{ margin-top:70px; font-size:38px; color:#555; }}
  .scene {{ margin-top:18px; font-size:56px; line-height:1.4; font-weight:600; }}
  .mid {{ flex:1; display:flex; flex-direction:column; justify-content:center; }}
  .en {{ font-family:Georgia,"Times New Roman",serif; font-size:{size}px; line-height:1.28; text-wrap:balance; }}
  .en u {{ text-decoration-thickness:6px; text-underline-offset:14px; }}
  .cn {{ margin-top:40px; font-size:52px; color:#333; }}
  .bottom {{ font-size:42px; color:#555; border-top:2px solid #bbb; padding-top:26px; }}
  .bottom b {{ font-family:Georgia,serif; color:#111; }}
</style></head><body>
  <div class="top"><span>每日英语 · 昨晚的新闻</span><span class="rule"></span><span>{day.strftime("%b %d").upper()}</span></div>
  <div class="news">Hacker News · {html.escape(story["summary"]["title"])}</div>
  <div class="scene">{html.escape(pick["scene"])}</div>
  <div class="mid"><div class="en">“{sentence}”</div><div class="cn">{html.escape(pick["cn"])}</div></div>
  <div class="bottom"><b>{html.escape(phrase)}</b>　{html.escape(pick["phrase_cn"])}</div>
</body></html>"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cards", type=Path, help="cards.json written by hacker_news.py prepare")
    parser.add_argument("-o", "--output", type=Path, default=Path("news-english.png"))
    parser.add_argument("--ollama", default=OLLAMA, help=f"Ollama server (default: {OLLAMA})")
    parser.add_argument("--model", default=MODEL, help=f"Ollama model (default: {MODEL})")
    parser.add_argument("--chrome", default=CHROME, help="Chrome or Chromium binary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        day = dt.date.today()
        pick, story = choose(load_stories(args.cards, day), args.ollama, args.model)
        render(card_page(pick, story, day), args.output, args.chrome)
        print(f"{args.output}: {pick['english']}  ({story['title']})")
        return 0
    except Exception as exc:  # noqa: BLE001  (whatever fails, cron falls back to daily_english.py)
        print(f"error: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
