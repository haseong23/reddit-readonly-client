# reddit-readonly-client

A small, personal, non-commercial, **read-only** client for the Reddit Data API.

I use it to follow public discussions about AI coding tools (Claude Code, Codex, Cursor, MCP, local LLMs)
in a handful of subreddits, so I can keep up with how other developers use these tools and improve my own
development workflow.

Status: waiting for Reddit Data API approval under the
[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy).
It will not be run against the API until access is approved.

## What it does

- Reads post listings of a subreddit (`hot`, `new`, `top`, `rising`).
- Runs keyword search inside one subreddit (or across Reddit).
- Reads one post and its comments.
- Writes the result as JSON Lines on my own machine.

## What it never does

- No posting, commenting, voting, messaging, chat, or moderation actions. It only sends `GET` requests
  with an application-only token, so it cannot act as any user.
- No usernames or other author information are stored. Deleted and removed comments are skipped.
- No redistribution, publishing, selling, or sharing of Reddit data.
- No training or fine-tuning of machine-learning or AI models. I read the output myself, and sometimes ask
  an AI assistant to summarize it for my own notes; the data is not kept for or used in model training.
- No commercial use.

## Authentication

Application-only OAuth for a `script` app (`grant_type=client_credentials`). The token is kept in memory
only. Credentials come from environment variables and are never written to disk by this tool.

```sh
export REDDIT_CLIENT_ID=...
export REDDIT_CLIENT_SECRET=...
export REDDIT_USERNAME=...   # used only in the User-Agent contact field
```

User-Agent follows the format in the Reddit Data API wiki:

```
script:io.github.haseong23.reddit-readonly-client:v0.1.0 (by /u/<username>)
```

## Rate limits

- One request at a time, at least 1 second apart (at most 60 requests per minute, below the
  100 QPM free limit).
- Reads `X-Ratelimit-Remaining` and `X-Ratelimit-Reset`; when the remaining quota is almost used up,
  it waits for the window to reset.
- HTTP 429 and 5xx are retried at most 3 times with a growing wait (30, 60, 90 seconds). Other errors stop the run.
- Expected volume: a few hundred requests per week, run by hand.

## Data retention

Reddit asks API users to remove content that has been deleted on Reddit and recommends deleting stored
data within 48 hours. The `purge` command deletes saved `.jsonl` files older than 48 hours, and I run it
after each reading session.

```sh
python3 reddit_readonly.py purge data/ --hours 48
```

## Usage

Python 3.8+ standard library only.

```sh
python3 reddit_readonly.py listing ClaudeAI top --t week --limit 25 --out data/claudeai_top.jsonl
python3 reddit_readonly.py search LocalLLaMA "mcp server" --sort top --t month --out data/search.jsonl
python3 reddit_readonly.py thread 1abcde2 --limit 100 --out data/thread_1abcde2.jsonl
python3 reddit_readonly.py purge data/ --hours 48
```

Output rows:

| kind | fields |
|---|---|
| post | id, subreddit, title, text (first 4000 chars), url, score, num_comments, created_utc, permalink |
| comment | id, parent_id, link_id, depth, text (first 4000 chars), score, created_utc |

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## License

MIT
