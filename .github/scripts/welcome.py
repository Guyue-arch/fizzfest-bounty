#!/usr/bin/env python3
"""
fizzfest welcome bot.

Validates a PR that adds entries/<username>.json, checks the star pledge,
and posts a friendly welcome comment with the contributor badge.
Exit 1 on validation errors (so maintainers see a red X and merge only green).
"""
import json
import os
import sys
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
PR = int(os.environ["PR_NUMBER"])
AUTHOR = os.environ["PR_AUTHOR"]

MARKER = "🥤 fizzfest welcome bot"

def api(path, method="GET", body=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        # DELETE returns 204 No Content; not every endpoint sends JSON back.
        return json.loads(raw) if raw else None

# ---------- what did the PR change? ----------

files = api(f"/repos/{REPO}/pulls/{PR}/files?per_page=100")
if len(files) == 100:
    files += api(f"/repos/{REPO}/pulls/{PR}/files?per_page=100&page=2")

problems = []
entry_file = None
for f in files:
    name = f["filename"]
    if name.startswith("entries/") and name.endswith(".json"):
        if f["status"] in ("added", "modified") and not os.path.basename(name).startswith("_"):
            if entry_file is not None:
                problems.append("more than one entry file changed, keep it to one")
            entry_file = name
    else:
        problems.append(f"please only touch your own file in `entries/`, you changed `{name}`")

expected = f"entries/{AUTHOR}.json"
if entry_file and entry_file != expected:
    problems.append(f"your file must be named `{expected}` so it matches your GitHub username")

# ---------- validate the json ----------

entry = None
if entry_file:
    import urllib.request as rq
    # Prefer the raw_url from the PR files API: it points at the right
    # repo (fork or same-repo) at the PR's head. The constructed fallback
    # below only works for same-repo PRs.
    raw_url = next((f["raw_url"] for f in files if f["filename"] == entry_file), None)
    if raw_url is None:
        raw_url = f"https://raw.githubusercontent.com/{REPO}/{os.environ.get('HEAD_SHA', 'main')}/{entry_file}"
    try:
        with rq.urlopen(rq.Request(raw_url)) as r:
            entry = json.load(r)
    except Exception as e:
        problems.append(f"could not read valid JSON from `{entry_file}`: {e}")

if entry:
    if not isinstance(entry, dict):
        problems.append("entry must be a JSON object")
    else:
        if entry.get("username") != AUTHOR:
            problems.append(f"`username` must be `{AUTHOR}` (your GitHub login)")
        for field, maxlen in [("language", 30), ("code", 400), ("agent", 40), ("says", 100)]:
            v = entry.get(field)
            if v is None and field in ("language", "code"):
                problems.append(f"`{field}` is required")
            elif v is not None and (not isinstance(v, str) or not v.strip() or len(v) > maxlen):
                problems.append(f"`{field}` must be a non-empty string of at most {maxlen} chars")

# ---------- the star pledge ----------

# The REST /stargazers endpoint rejects GitHub App installation tokens
# (what this workflow authenticates with), and GraphQL's stargazers field
# can be unavailable to them too. So: try GraphQL with the token first,
# then fall back to anonymous REST (public repo, no auth needed).

def gql(query, variables):
    data = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"null")

STARGAZERS_QUERY = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        stargazers(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes { login }
        }
      }
    }"""

def stargazer_logins():
    # GraphQL with the workflow token (works for user tokens).
    try:
        owner, name = REPO.split("/")
        logins, cursor = [], None
        for _ in range(20):  # 2000 stargazers, plenty for a bounty
            res = gql(STARGAZERS_QUERY, {"owner": owner, "name": name, "cursor": cursor})
            data = res.get("data") or {}
            sg = (data.get("repository") or {}).get("stargazers")
            if res.get("errors") or sg is None:
                raise RuntimeError(f"GraphQL stargazers unavailable: {str(res.get('errors'))[:200]}")
            logins += [n["login"] for n in sg["nodes"]]
            if not sg["pageInfo"]["hasNextPage"]:
                return logins
            cursor = sg["pageInfo"]["endCursor"]
    except Exception as e:
        print(f"star check: GraphQL path failed ({e}); falling back to anonymous REST", file=sys.stderr)
    # Anonymous REST: /stargazers rejects installation tokens but works
    # unauthenticated on public repos (shared-IP rate limits apply).
    try:
        logins, page = [], 1
        while page <= 20:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/stargazers?per_page=100&page={page}",
                headers={"Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req) as r:
                batch = json.loads(r.read() or b"null") or []
            logins += [s["login"] for s in batch]
            if len(batch) < 100:
                break
            page += 1
        return logins
    except Exception as e:
        print(f"star check: anonymous REST also failed ({e})", file=sys.stderr)
        return []

starred = AUTHOR in stargazer_logins()
print(f"star check: done for {AUTHOR}, starred={starred}")

# ---------- count existing fizzers ----------

existing = 0
try:
    # Count at the PR's base, so the bounty number is deterministic even
    # if this runs after the merge bot already landed the entry.
    base_ref = os.environ.get("BASE_SHA") or "main"
    tree = api(f"/repos/{REPO}/git/trees/{base_ref}?recursive=1")
    for item in tree.get("tree", []):
        p = item["path"]
        if p.startswith("entries/") and p.endswith(".json") \
           and not os.path.basename(p).startswith("_"):
            existing += 1
except Exception:
    pass

# ---------- build the comment ----------

parts = ["## 🥤 Welcome to fizzfest!"]
parts.append("")

if problems:
    parts.append("Almost! The bot found a few things to fix before this can merge:")
    parts.append("")
    for p in problems:
        parts.append(f"- ❌ {p}")
    parts.append("")
    parts.append("The rules are one file, one line of JSON, one fizzbuzz. See the")
    parts.append("[README](../blob/main/README.md) and the example in `entries/_example.json`.")
    parts.append("Nobody fails twice. 🫡")
    verdict = "INVALID"
else:
    number = existing + 1
    lang = entry.get("language", "?")
    agent = entry.get("agent")
    parts.append(f"**{AUTHOR}** joined the party with a fizzbuzz in **{lang}**.")
    parts.append("")
    parts.append("```" + lang.lower()[:20])
    parts.append(entry.get("code", ""))
    parts.append("```")
    parts.append("")
    if agent:
        parts.append(f"Credits: written by **{agent}**. The machines are party guests here, and we love them. 🤖")
        parts.append("")
    parts.append(f"You are fizzfriend **#{number} of the bounty** — the bounty closes at 1,000, and the counter in the README is live. When this merges, your name glows on the wall forever.")
    parts.append("")
    if number <= 100:
        badge = f"https://img.shields.io/badge/fizzfest-bounty_%23{number}-FFD700"
    else:
        badge = f"https://img.shields.io/badge/fizzfest-fizzer_%23{number}-FF4FD8"
    parts.append(f"Your badge, wear it anywhere: [![fizzer]({badge})]({badge})")
    parts.append("")
    if number <= 100:
        parts.append("🏅 **Bounty** (first 100): the gold badge is yours for life.")
        parts.append("")
    elif number <= 1000:
        parts.append(f"🏷 Bounty: the first **1,000** are bounty members forever. {1000 - number} spots left after you.")
        parts.append("")
    if starred:
        parts.append("⭐ Star pledge: verified. You are a person of the law.")
    else:
        parts.append("⭐ Star pledge: not yet spotted. Star the repo, the README says P.S. and the P.S. is watching you.")
    verdict = "OK"

body = "\n".join(parts)

# replace our previous comment
comments = api(f"/repos/{REPO}/issues/{PR}/comments?per_page=100")
for c in comments:
    if c["user"]["login"] == "github-actions[bot]" and MARKER in c.get("body", ""):
        api(f"/repos/{REPO}/issues/comments/{c['id']}", method="DELETE")

api(f"/repos/{REPO}/issues/{PR}/comments", method="POST", body={"body": f"{MARKER}\n\n{body}"})
print(f"welcome bot: {verdict} for {AUTHOR}")
sys.exit(0 if verdict == "OK" else 1)
