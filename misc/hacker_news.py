#!/usr/bin/env python3
"""Show the top Hacker News stories on the frame, each summarised with its comments by Ollama.

  prepare DIR    fetch the front page, summarise the top stories, render DIR/01.png ...
  next DIR       print the next card's path, cycling through the cards
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import shutil
import sys
import urllib.parse
from pathlib import Path

from daily_english import CHROME, render
from github_trending import OLLAMA, get, next_card, swap_in, text

API = "https://hacker-news.firebaseio.com/v0"
MODEL = "gemma4:e4b"
COUNT = 8
CANDIDATES = 30
COMMENTS = 6
SCHEMA = {
    "type": "object",
    "properties": {
        "tech": {"type": "boolean"},
        "title": {"type": "string"},
        "what": {"type": "string"},
        "views": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
    },
    "required": ["tech", "title", "what", "views"],
}
SYSTEM = ("你为电子墨水屏写 Hacker News 讨论简报，读者是中国软件工程师。"
          "只依据给出的标题、正文和评论，不要编造事实。产品、公司、项目等专有名词保留英文原文，不要意译。")
ASK = ("tech：这条是否和软件、硬件、科学或工程实践相关（政治、社会新闻为 false）。"
       "title：中文标题，不超过 24 字。what：一句话直接陈述发生了什么，不超过 40 字，不要以“讨论了”“文章”开头。"
       "views：评论区 2–3 个有代表性的观点或分歧，每条不超过 30 字，用中文。")


def item(number: int) -> dict:
    return json.loads(get(f"{API}/item/{number}.json")) or {}  # a vanished item is null


def live(entry: dict) -> bool:
    return not entry.get("dead") and not entry.get("deleted")


def top_stories() -> list[dict]:
    """The front page's first CANDIDATES stories, highest score first, titles as plain text."""
    stories = []
    for number in json.loads(get(f"{API}/topstories.json"))[:CANDIDATES]:
        try:
            entry = item(number)
        except Exception as exc:  # noqa: BLE001  (a timeout, a cut-off read, bad JSON)
            print(f"story {number}: skipped ({exc!r})", file=sys.stderr)
            continue
        if entry.get("type") == "story" and live(entry):
            stories.append({**entry, "title": html.unescape(entry.get("title", ""))})  # the API sends HTML
    return sorted(stories, key=lambda s: s.get("score", 0), reverse=True)


def comments(story: dict) -> list[str]:
    """Up to COMMENTS top-level comments, as plain text."""
    found = []
    for number in story.get("kids", [])[:COMMENTS * 2]:  # some are dead or deleted
        entry = item(number)
        if entry.get("text") and live(entry):
            found.append(text(entry["text"]))
            if len(found) == COMMENTS:
                break
    return found


def story_prompt(story: dict, said: list[str]) -> str:
    parts = [f"标题：{story['title']}"]
    if story.get("url"):
        parts.append(f"链接：{story['url']}")
    if story.get("text"):
        parts.append(f"正文：{text(story['text'])[:1500]}")
    parts += [f"评论 {n}：{c[:600]}" for n, c in enumerate(said, 1)]
    return "<story>\n" + "\n".join(parts) + "\n</story>\n\n" + ASK


def summarise(story: dict, said: list[str], ollama: str, model: str) -> dict:
    body = {
        "model": model, "stream": False, "think": False, "format": SCHEMA, "options": {"temperature": 0.2},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": story_prompt(story, said)}],
    }
    reply = get(f"{ollama}/api/chat", {"Content-Type": "application/json"}, json.dumps(body).encode(), timeout=300)
    summary = json.loads(json.loads(reply)["message"]["content"])
    title, what, views = summary.get("title"), summary.get("what"), summary.get("views")
    if not (isinstance(summary.get("tech"), bool) and isinstance(title, str) and isinstance(what, str)
            and isinstance(views, list) and all(isinstance(v, str) for v in views)):
        raise ValueError("the model's reply does not match the schema")
    views = [v.strip() for v in views[:3] if v.strip()]
    if not title.strip() or not what.strip() or len(views) < 2:
        raise ValueError("the model returned an empty summary or fewer than two views")
    return {"tech": summary["tech"], "title": title.strip(), "what": what.strip(), "views": views}


def card_page(story: dict, rank: int, total: int, day: dt.date, model: str) -> str:
    summary = story["summary"]
    host = (urllib.parse.urlparse(story.get("url", "")).hostname or "news.ycombinator.com").removeprefix("www.")
    views = "".join(f"<li>{html.escape(v)}</li>" for v in summary["views"])
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><style>
      body {{ margin:0; width:1872px; height:1404px; background:#fff; color:#111; box-sizing:border-box;
             padding:70px 100px 64px; display:flex; flex-direction:column;
             font-family:"Helvetica Neue","PingFang SC","Noto Sans CJK SC",sans-serif; }}
      header {{ display:flex; justify-content:space-between; font-size:34px; color:#555;
               border-bottom:4px solid #111; padding-bottom:20px; }}
      header b {{ color:#111; }}
      .host {{ font-size:40px; color:#555; margin-top:56px; }}
      .title {{ font-size:96px; font-weight:700; line-height:1.18; margin-top:10px; }}
      .what {{ font-size:54px; line-height:1.4; margin-top:40px; }}
      h2 {{ font-size:34px; color:#555; font-weight:600; margin:48px 0 0; letter-spacing:4px; }}
      ul {{ margin:18px 0 0; padding:0; list-style:none; }}
      li {{ font-size:46px; line-height:1.45; margin-bottom:16px; padding-left:64px; position:relative; }}
      li::before {{ content:""; position:absolute; left:6px; top:12px; width:10px; height:44px; background:#777; }}
      .english {{ font-size:30px; color:#555; line-height:1.4; margin-top:auto; white-space:nowrap;
                 overflow:hidden; text-overflow:ellipsis; }}
      footer {{ display:flex; justify-content:space-between; align-items:baseline; margin-top:24px;
               border-top:2px solid #bbb; padding-top:22px; font-size:34px; color:#555; }}
      footer b {{ font-size:46px; color:#111; }}
      .credit {{ font-size:26px; color:#777; }}
    </style></head><body>
      <header><span><b>Hacker News</b> · {day.month}月{day.day}日</span><span>第 {rank} / {total} 条</span></header>
      <div class="host">{html.escape(host)}</div>
      <div class="title">{html.escape(summary["title"])}</div>
      <div class="what">{html.escape(summary["what"])}</div>
      <h2>评论区怎么说</h2>
      <ul>{views}</ul>
      <div class="english">{html.escape(story["title"])}</div>
      <footer><span class="credit">摘要由 {html.escape(model)} 根据标题和评论生成</span>
              <span><b>▲ {int(story.get("score", 0))}</b>　💬 {int(story.get("descendants", 0))}</span></footer>
    </body></html>"""


def prepare(directory: Path, ollama: str, model: str, chrome: str, day: dt.date) -> list[dict]:
    chosen = []
    for story in top_stories():
        if len(chosen) == COUNT:
            break
        try:
            said = comments(story)
            if len(said) < 2:
                continue  # too little discussion to summarise
            story["summary"] = summarise(story, said, ollama, model)
        except Exception as exc:  # noqa: BLE001
            # A failed comment fetch or a malformed reply skips this story, not the evening's cards.
            print(f"{story.get('title')}: skipped ({exc})", file=sys.stderr)
            continue
        if story["summary"]["tech"]:
            chosen.append(story)
    if not chosen:
        raise RuntimeError("no story could be summarised")
    fresh = directory.with_name(directory.name + ".new")
    shutil.rmtree(fresh, ignore_errors=True)
    fresh.mkdir(parents=True)
    for rank, story in enumerate(chosen, 1):
        render(card_page(story, rank, len(chosen), day, model), fresh / f"{rank:02}.png", chrome)
    (fresh / "cards.json").write_text(
        json.dumps({"day": day.isoformat(), "stories": chosen}, ensure_ascii=False, indent=1), encoding="utf-8")
    swap_in(fresh, directory)
    return chosen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["prepare", "next"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--ollama", default=OLLAMA, help=f"Ollama server (default: {OLLAMA})")
    parser.add_argument("--model", default=MODEL, help=f"Ollama model (default: {MODEL})")
    parser.add_argument("--chrome", default=CHROME, help="Chrome or Chromium binary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "next":
            print(next_card(args.directory))
            return 0
        for rank, story in enumerate(prepare(args.directory, args.ollama, args.model, args.chrome, dt.date.today()), 1):
            print(f"{rank:2} {story['summary']['title']}  ({story['title']})")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
