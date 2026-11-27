#!/usr/bin/env python3
"""Show GitHub Trending on the frame, one project per card, summarised from its README by Ollama.

  prepare DIR    fetch today's trending list, summarise each README, render DIR/01.png ...
  next DIR       print the next card's path, cycling through the day's cards
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

from daily_english import CHROME, render

TRENDING = "https://github.com/trending?since=daily"
OLLAMA = "http://localhost:11434"
MODEL = "gemma4:12b"
COUNT = 10
README_CHARS = 6000
SCHEMA = {
    "type": "object",
    "properties": {
        "tagline": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
    },
    "required": ["tagline", "points"],
}
SYSTEM = "你为电子墨水屏写 GitHub 项目简介。只依据给出的 README，不要评价 README 本身，不要编造。"
ASK = ("用简体中文介绍这个项目：tagline 一句话说清它是什么（不超过 30 字）；"
       "points 2–3 条亮点或用途（每条不超过 28 字）。")


def get(url: str, headers: dict | None = None, data: bytes | None = None, timeout: float = 30) -> bytes:
    request = urllib.request.Request(url, data, {"User-Agent": "pixer-trending", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def text(fragment: str) -> str:
    """Tags stripped, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def parse_trending(page: str) -> list[dict]:
    repos = []
    for article in re.findall(r'<article class="Box-row">(.*?)</article>', page, re.S):
        name = re.search(r'<h2[^>]*>.*?href="/([^"/]+/[^"/]+)"', article, re.S)
        if not name:
            continue
        description = re.search(r"<p class=[^>]*>(.*?)</p>", article, re.S)  # not the icons' <path>
        language = re.search(r'itemprop="programmingLanguage">([^<]+)<', article)
        stars = re.search(r'/stargazers"[^>]*>.*?</svg>\s*([\d,]+)', article, re.S)
        today = re.search(r"([\d,]+) stars today", article)
        repos.append({
            "name": name.group(1),
            "description": text(description.group(1)) if description else "",
            "language": language.group(1).strip() if language else "",
            "stars": stars.group(1) if stars else "?",
            "today": today.group(1) if today else "?",
        })
    if not repos:
        raise RuntimeError("no repositories found; the trending page layout may have changed")
    return repos


def clean_readme(body: str) -> str:
    """The prose of a README: code, images, badges and HTML tags removed, cut to README_CHARS."""
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"<[^>]+>", "", body)
    return re.sub(r"\n\s*\n+", "\n\n", body)[:README_CHARS]


def readme(name: str) -> str:
    raw = get(f"https://api.github.com/repos/{name}/readme", {"Accept": "application/vnd.github.raw"})
    return clean_readme(raw.decode("utf-8", "replace"))


def summarise(readme_text: str, ollama: str, model: str) -> dict:
    # Without the schema the model tends to critique the README instead of describing the project.
    body = {
        "model": model, "stream": False, "think": False, "format": SCHEMA, "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"<readme>\n{readme_text}\n</readme>\n\n{ASK}"},
        ],
    }
    reply = get(f"{ollama}/api/chat", {"Content-Type": "application/json"}, json.dumps(body).encode(), timeout=300)
    summary = json.loads(json.loads(reply)["message"]["content"])
    if not isinstance(summary.get("tagline"), str) or not isinstance(summary.get("points"), list):
        raise ValueError("the model's reply does not match the schema")
    points = [p.strip() for p in summary["points"][:3] if p.strip()]
    if not summary["tagline"].strip() or not points:
        raise ValueError("the model returned an empty summary")
    return {"tagline": summary["tagline"].strip(), "points": points}


def card_page(repo: dict, rank: int, total: int, day: dt.date, model: str) -> str:
    owner, name = repo["name"].split("/", 1)
    size = min(120, int(1690 / (len(name) * 0.56)))  # a long name shrinks to stay on one line
    summary = repo.get("summary")
    if summary:
        tagline = html.escape(summary["tagline"])
        points = "".join(f"<li>{html.escape(p)}</li>" for p in summary["points"])
        english = html.escape(repo["description"])
        credit = f"摘要由 {html.escape(model)} 根据 README 生成"
    else:
        tagline, points, english = html.escape(repo["description"] or "（没有简介）"), "", ""
        credit = "未能生成摘要，显示项目原简介"
    meta = " · ".join(filter(None, [html.escape(repo["language"]), f"★ {html.escape(repo['stars'])}"]))
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><style>
      body {{ margin:0; width:1872px; height:1404px; background:#fff; color:#111; box-sizing:border-box;
             padding:70px 100px 64px; display:flex; flex-direction:column;
             font-family:"Helvetica Neue","PingFang SC","Noto Sans CJK SC",sans-serif; }}
      header {{ display:flex; justify-content:space-between; font-size:34px; color:#555;
               border-bottom:4px solid #111; padding-bottom:20px; }}
      header b {{ color:#111; }}
      .owner {{ font-size:50px; color:#555; margin-top:60px; }}
      .name {{ font-size:{size}px; font-weight:700; line-height:1.1; white-space:nowrap; margin-top:6px; }}
      .tagline {{ font-size:62px; font-weight:600; line-height:1.35; margin-top:56px; }}
      ul {{ margin:36px 0 0; padding:0; list-style:none; }}
      li {{ font-size:44px; line-height:1.45; margin-bottom:18px; padding-left:44px; position:relative; }}
      li::before {{ content:""; position:absolute; left:4px; top:24px; width:16px; height:16px; background:#111; }}
      .english {{ font-size:30px; color:#555; line-height:1.4; margin-top:auto; display:-webkit-box;
                 -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }}
      footer {{ display:flex; justify-content:space-between; align-items:baseline; margin-top:24px;
               border-top:2px solid #bbb; padding-top:22px; font-size:34px; color:#555; }}
      .today {{ font-size:46px; font-weight:700; color:#111; }}
      .credit {{ font-size:26px; color:#777; }}
    </style></head><body>
      <header><span><b>GitHub Trending</b> · {day.month}月{day.day}日</span><span>第 {rank} / {total} 名</span></header>
      <div class="owner">{html.escape(owner)} /</div>
      <div class="name">{html.escape(name)}</div>
      <div class="tagline">{tagline}</div>
      <ul>{points}</ul>
      <div class="english">{english}</div>
      <footer><span>{meta}　<span class="credit">{credit}</span></span>
              <span class="today">+{html.escape(repo["today"])} 今日</span></footer>
    </body></html>"""


def prepare(directory: Path, ollama: str, model: str, chrome: str, day: dt.date) -> list[dict]:
    repos = parse_trending(get(TRENDING).decode("utf-8"))[:COUNT]
    for repo in repos:
        try:
            repo["summary"] = summarise(readme(repo["name"]), ollama, model)
        except Exception as exc:  # noqa: BLE001
            # Whatever goes wrong for one project (no README, a sleeping Ollama, a malformed
            # reply) costs that project its summary, not the day its cards.
            print(f"{repo['name']}: no summary ({exc})", file=sys.stderr)
    # Render into a fresh folder, then swap it in, so a failed run keeps the last good cards.
    fresh = directory.with_name(directory.name + ".new")
    shutil.rmtree(fresh, ignore_errors=True)
    fresh.mkdir(parents=True)
    for rank, repo in enumerate(repos, 1):
        render(card_page(repo, rank, len(repos), day, model), fresh / f"{rank:02}.png", chrome)
    (fresh / "cards.json").write_text(
        json.dumps({"day": day.isoformat(), "repos": repos}, ensure_ascii=False, indent=1), encoding="utf-8")
    swap_in(fresh, directory)
    return repos


def swap_in(fresh: Path, directory: Path) -> None:
    """Replace directory with fresh; if the swap fails, put the old directory back."""
    old = directory.with_name(directory.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if directory.exists():
        directory.rename(old)
    try:
        fresh.rename(directory)
    except OSError:
        if old.exists():
            old.rename(directory)
        raise
    shutil.rmtree(old, ignore_errors=True)


def next_card(directory: Path) -> Path:
    cards = sorted(directory.glob("[0-9][0-9].png"))
    if not cards:
        raise RuntimeError(f"no cards in {directory}; run prepare first")
    pointer = directory / "next"
    index = int(pointer.read_text()) % len(cards) if pointer.is_file() else 0
    pointer.write_text(str((index + 1) % len(cards)))
    return cards[index]


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
        for rank, repo in enumerate(prepare(args.directory, args.ollama, args.model, args.chrome, dt.date.today()), 1):
            print(f"{rank:2} {repo['name']}: {repo.get('summary', {}).get('tagline', '(no summary)')}")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
