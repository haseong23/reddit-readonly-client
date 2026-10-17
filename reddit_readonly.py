#!/usr/bin/env python3
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
APP_ID = "io.github.haseong23.reddit-readonly-client"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
MIN_INTERVAL = 1.0
MAX_TRIES = 4
RETRY_WAIT = 30
TEXT_LIMIT = 4000
REMOVED = ("[deleted]", "[removed]")
RETRY_CODES = (429, 500, 502, 503, 504)


class RedditError(RuntimeError):
    pass


def user_agent(username):
    return f"script:{APP_ID}:v{VERSION} (by /u/{username})"


class Client:
    def __init__(self, client_id, client_secret, username, opener=None, sleep=time.sleep, clock=time.monotonic):
        if not (client_id and client_secret and username):
            raise RedditError("REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and REDDIT_USERNAME must be set")
        self.client_id = client_id
        self.client_secret = client_secret
        self.ua = user_agent(username)
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.clock = clock
        self.token = None
        self.token_expires = 0.0
        self.last_request = None
        self.remaining = None
        self.reset = None

    def _token(self):
        if self.token and self.clock() < self.token_expires - 60:
            return self.token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
            "Authorization": f"Basic {basic}",
            "User-Agent": self.ua,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        with self.opener(req, timeout=30) as resp:
            data = json.load(resp)
        if "access_token" not in data:
            raise RedditError(f"token request failed: {data.get('error', 'no access_token')}")
        self.token = data["access_token"]
        self.token_expires = self.clock() + float(data.get("expires_in", 3600))
        return self.token

    def _pace(self):
        if self.last_request is not None:
            wait = MIN_INTERVAL - (self.clock() - self.last_request)
            if wait > 0:
                self.sleep(wait)
        if self.remaining is not None and self.remaining < 2 and self.reset is not None:
            self.sleep(self.reset + 1)
            self.remaining = None
        self.last_request = self.clock()

    def _record_limits(self, headers):
        if headers is None:
            return
        try:
            self.remaining = float(headers.get("X-Ratelimit-Remaining"))
            self.reset = float(headers.get("X-Ratelimit-Reset"))
        except (TypeError, ValueError):
            pass

    def get(self, path, params=None):
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
        for attempt in range(1, MAX_TRIES + 1):
            self._pace()
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self._token()}",
                "User-Agent": self.ua,
            })
            try:
                with self.opener(req, timeout=30) as resp:
                    self._record_limits(resp.headers)
                    return json.load(resp)
            except urllib.error.HTTPError as err:
                self._record_limits(err.headers)
                if attempt == MAX_TRIES:
                    raise RedditError(f"HTTP {err.code} for {path}") from err
                if err.code == 401:
                    self.token = None
                elif err.code in RETRY_CODES:
                    self.sleep(RETRY_WAIT * attempt)
                else:
                    raise RedditError(f"HTTP {err.code} for {path}") from err
        raise RedditError(f"giving up on {path}")


def shape_post(d):
    text = d.get("selftext") or ""
    return {
        "kind": "post",
        "id": d.get("id"),
        "subreddit": d.get("subreddit"),
        "title": d.get("title"),
        "text": "" if text in REMOVED else text[:TEXT_LIMIT],
        "url": d.get("url"),
        "score": d.get("score"),
        "num_comments": d.get("num_comments"),
        "created_utc": d.get("created_utc"),
        "permalink": "https://www.reddit.com" + (d.get("permalink") or ""),
    }


def shape_comment(d, depth):
    return {
        "kind": "comment",
        "id": d.get("id"),
        "parent_id": d.get("parent_id"),
        "link_id": d.get("link_id"),
        "depth": depth,
        "text": (d.get("body") or "")[:TEXT_LIMIT],
        "score": d.get("score"),
        "created_utc": d.get("created_utc"),
    }


def walk_comments(children, depth=0):
    for child in children:
        if child.get("kind") != "t1":
            continue
        d = child.get("data", {})
        if (d.get("body") or "") not in REMOVED:
            yield shape_comment(d, depth)
        replies = d.get("replies")
        if isinstance(replies, dict):
            yield from walk_comments(replies.get("data", {}).get("children", []), depth + 1)


def posts_from(listing):
    for child in listing.get("data", {}).get("children", []):
        if child.get("kind") == "t3":
            yield shape_post(child.get("data", {}))


def clamp(limit):
    return max(1, min(100, limit))


def cmd_listing(client, args):
    params = {"limit": clamp(args.limit), "raw_json": 1}
    if args.sort == "top":
        params["t"] = args.t
    return posts_from(client.get(f"/r/{args.subreddit}/{args.sort}", params))


def cmd_search(client, args):
    params = {"q": args.query, "sort": args.sort, "t": args.t, "limit": clamp(args.limit), "type": "link", "raw_json": 1}
    if args.subreddit == "all":
        path = "/search"
    else:
        path = f"/r/{args.subreddit}/search"
        params["restrict_sr"] = 1
    return posts_from(client.get(path, params))


def cmd_thread(client, args):
    data = client.get(f"/comments/{args.id}", {"limit": clamp(args.limit), "sort": args.sort, "raw_json": 1})
    rows = list(posts_from(data[0]))
    rows.extend(walk_comments(data[1].get("data", {}).get("children", [])))
    return rows


def purge(directory, hours, now=None):
    cutoff = (now if now is not None else time.time()) - hours * 3600
    removed = []
    for path in sorted(Path(directory).rglob("*.jsonl")):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def write_rows(rows, out):
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        handle = open(out, "w", encoding="utf-8")
    else:
        handle = sys.stdout
    count = 0
    try:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    finally:
        if out:
            handle.close()
    return count


def build_parser():
    parser = argparse.ArgumentParser(prog="reddit_readonly", description="Read-only Reddit Data API client (application-only OAuth).")
    sub = parser.add_subparsers(dest="command", required=True)
    periods = ["hour", "day", "week", "month", "year", "all"]

    p = sub.add_parser("listing", help="posts of one subreddit")
    p.add_argument("subreddit")
    p.add_argument("sort", choices=["hot", "new", "top", "rising"])
    p.add_argument("--t", choices=periods, default="week")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--out")

    p = sub.add_parser("search", help="keyword search in one subreddit or all")
    p.add_argument("subreddit")
    p.add_argument("query")
    p.add_argument("--sort", choices=["relevance", "new", "top", "comments"], default="relevance")
    p.add_argument("--t", choices=periods, default="year")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--out")

    p = sub.add_parser("thread", help="one post and its comments")
    p.add_argument("id")
    p.add_argument("--sort", choices=["top", "new", "best"], default="top")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--out")

    p = sub.add_parser("purge", help="delete saved .jsonl files older than the retention window")
    p.add_argument("directory")
    p.add_argument("--hours", type=float, default=48)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "purge":
        for path in purge(args.directory, args.hours):
            print(path)
        return 0
    commands = {"listing": cmd_listing, "search": cmd_search, "thread": cmd_thread}
    try:
        client = Client(
            os.environ.get("REDDIT_CLIENT_ID"),
            os.environ.get("REDDIT_CLIENT_SECRET"),
            os.environ.get("REDDIT_USERNAME"),
        )
        count = write_rows(commands[args.command](client, args), args.out)
    except RedditError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if args.out:
        print(f"{count} rows -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
