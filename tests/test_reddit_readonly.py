import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reddit_readonly as rr


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}


class FakeOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def token_reply():
    return FakeResponse({"access_token": "tok", "expires_in": 86400})


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://oauth.reddit.com/x", code, "err", headers or {}, None)


def make_client(replies):
    opener = FakeOpener(replies)
    sleeps = []
    client = rr.Client("cid", "secret", "someone", opener=opener, sleep=sleeps.append, clock=lambda: 1000.0)
    return client, opener, sleeps


def post(pid, **extra):
    data = {"id": pid, "subreddit": "ClaudeAI", "title": f"t{pid}", "selftext": "body", "author": "alice",
            "score": 5, "num_comments": 2, "created_utc": 1.0, "permalink": f"/r/ClaudeAI/comments/{pid}/x/"}
    data.update(extra)
    return {"kind": "t3", "data": data}


def comment(cid, body, replies=""):
    return {"kind": "t1", "data": {"id": cid, "parent_id": "t3_p1", "link_id": "t3_p1", "body": body,
                                   "author": "bob", "score": 1, "created_utc": 2.0, "replies": replies}}


class ClientTest(unittest.TestCase):
    def test_token_request_uses_client_credentials(self):
        client, opener, _ = make_client([token_reply(), FakeResponse({"data": {"children": []}})])
        client.get("/r/ClaudeAI/new")
        token_req = opener.requests[0]
        self.assertEqual(token_req.full_url, rr.TOKEN_URL)
        self.assertEqual(token_req.data, b"grant_type=client_credentials")
        self.assertTrue(token_req.get_header("Authorization").startswith("Basic "))
        api_req = opener.requests[1]
        self.assertEqual(api_req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(api_req.get_header("User-agent"),
                         "script:io.github.haseong23.reddit-readonly-client:v0.1.0 (by /u/someone)")

    def test_missing_credentials(self):
        with self.assertRaises(rr.RedditError):
            rr.Client("", "secret", "someone")

    def test_retry_after_429(self):
        client, _, sleeps = make_client([token_reply(), http_error(429), FakeResponse({"ok": 1})])
        self.assertEqual(client.get("/x"), {"ok": 1})
        self.assertIn(rr.RETRY_WAIT, sleeps)

    def test_forbidden_is_not_retried(self):
        client, opener, _ = make_client([token_reply(), http_error(403)])
        with self.assertRaises(rr.RedditError):
            client.get("/x")
        self.assertEqual(len(opener.requests), 2)

    def test_waits_for_reset_when_quota_is_low(self):
        low = {"X-Ratelimit-Remaining": "1", "X-Ratelimit-Reset": "40"}
        client, _, sleeps = make_client([token_reply(), FakeResponse({}, low), FakeResponse({})])
        client.get("/a")
        client.get("/b")
        self.assertIn(41.0, sleeps)

    def test_spacing_between_requests(self):
        client, _, sleeps = make_client([token_reply(), FakeResponse({}), FakeResponse({})])
        client.get("/a")
        client.get("/b")
        self.assertEqual(sleeps, [rr.MIN_INTERVAL])


class ShapeTest(unittest.TestCase):
    def test_listing_drops_author(self):
        listing = {"data": {"children": [post("p1"), {"kind": "t1", "data": {}}]}}
        rows = list(rr.posts_from(listing))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("author", rows[0])
        self.assertEqual(rows[0]["permalink"], "https://www.reddit.com/r/ClaudeAI/comments/p1/x/")

    def test_removed_post_text_is_blank(self):
        rows = list(rr.posts_from({"data": {"children": [post("p2", selftext="[removed]")]}}))
        self.assertEqual(rows[0]["text"], "")

    def test_comments_nested_and_deleted_skipped(self):
        nested = {"data": {"children": [comment("c2", "reply"), comment("c3", "[deleted]")]}}
        rows = list(rr.walk_comments([comment("c1", "top", nested), {"kind": "more", "data": {}}]))
        self.assertEqual([(r["id"], r["depth"]) for r in rows], [("c1", 0), ("c2", 1)])
        self.assertTrue(all("author" not in r for r in rows))

    def test_thread_command(self):
        thread = [{"data": {"children": [post("p1")]}}, {"data": {"children": [comment("c1", "hi")]}}]
        client, _, _ = make_client([token_reply(), FakeResponse(thread)])
        args = rr.build_parser().parse_args(["thread", "p1"])
        rows = rr.cmd_thread(client, args)
        self.assertEqual([r["kind"] for r in rows], ["post", "comment"])

    def test_search_restricts_to_subreddit(self):
        client, opener, _ = make_client([token_reply(), FakeResponse({"data": {"children": []}})])
        args = rr.build_parser().parse_args(["search", "ClaudeAI", "mcp server", "--limit", "500"])
        list(rr.cmd_search(client, args))
        url = opener.requests[1].full_url
        self.assertIn("/r/ClaudeAI/search?", url)
        self.assertIn("restrict_sr=1", url)
        self.assertIn("limit=100", url)


class PurgeTest(unittest.TestCase):
    def test_purge_removes_only_old_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp, "old.jsonl")
            new = Path(tmp, "new.jsonl")
            other = Path(tmp, "notes.md")
            for p in (old, new, other):
                p.write_text("x")
            os.utime(old, (1000, 1000))
            os.utime(other, (1000, 1000))
            removed = rr.purge(tmp, 48, now=1000 + 49 * 3600)
            self.assertEqual(removed, [old])
            self.assertTrue(new.exists())
            self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()
