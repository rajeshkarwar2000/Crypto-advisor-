"""Durable server-side backup of the demo account in a PRIVATE GitHub Gist (free). Streamlit Cloud's disk is wiped on every
restart, so without this the server copy can disappear; the phone copy then restores it. Needs a GitHub token with the
'gist' permission in Streamlit secrets (GITHUB_TOKEN). The gist is found by its description, so no id has to be copied."""
import json
import requests

API = "https://api.github.com"
DESC = "crypto-advisor-backup"


def _h(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def find_gist(token, http=requests):
    """Id of this app's backup gist, or None."""
    r = http.get(f"{API}/gists", headers=_h(token), params={"per_page": 100}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"GitHub said {r.status_code}: {r.text[:150]}")
    for g in r.json():
        if g.get("description") == DESC:
            return g["id"]
    return None


def push(token, user, data, gist_id=None, http=requests):
    """Save `data` (a dict) as <user>.json in the gist, creating the gist the first time. Returns the gist id."""
    files = {f"{user}.json": {"content": json.dumps(data)}}
    gist_id = gist_id or find_gist(token, http)
    if gist_id:
        r = http.patch(f"{API}/gists/{gist_id}", headers=_h(token), json={"files": files}, timeout=20)
    else:
        r = http.post(f"{API}/gists", headers=_h(token), json={"description": DESC, "public": False, "files": files}, timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub said {r.status_code}: {r.text[:150]}")
    return r.json().get("id", gist_id)


def pull(token, user, gist_id=None, http=requests):
    """The saved account dict for `user`, or None if there is none."""
    gist_id = gist_id or find_gist(token, http)
    if not gist_id:
        return None
    r = http.get(f"{API}/gists/{gist_id}", headers=_h(token), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"GitHub said {r.status_code}: {r.text[:150]}")
    f = (r.json().get("files") or {}).get(f"{user}.json")
    if not f:
        return None
    text = f.get("content")
    if f.get("truncated") or text is None:
        text = http.get(f["raw_url"], headers=_h(token), timeout=20).text
    return json.loads(text)
