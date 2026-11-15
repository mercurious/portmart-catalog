#!/usr/bin/env python3
"""Resolve every github-release source in catalog.json (CONTRACT R8).

Runs in the public catalog repo's GitHub Actions workflow with the workflow's
GITHUB_TOKEN, so devices never call api.github.com. For each platform block
with a github-release source and no "resolved" yet, one releases-API call
yields the tag, the asset (exact name or glob), its size and its sha256: the
"<asset>.sha256" sidecar, else the asset's "sha256:<hex>" digest. The result is
written to <out>/catalog.json with media/ copied beside it, ready for Pages.

A transient failure (network, rate limit, 5xx) reuses the block from the
currently published catalog (--previous) so a bad run never drops a release.

python3 resolve.py --catalog catalog.json --media media --out site \
    --base-url https://<owner>.github.io/<repo>/ [--previous <url of live catalog.json>]
Env: GITHUB_TOKEN, PORTMART_GITHUB_API (default https://api.github.com),
     PORTMART_GITHUB (default https://github.com).
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request

API = os.environ.get("PORTMART_GITHUB_API", "https://api.github.com").rstrip("/")
GH = os.environ.get("PORTMART_GITHUB", "https://github.com").rstrip("/")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")


class Transient(Exception):
    pass


def get(url, accept=None, auth=False):
    req = urllib.request.Request(url, headers={"User-Agent": "portmart-catalog-resolver"})
    if accept:
        req.add_header("Accept", accept)
    if auth and TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise Transient(str(e))


def resolve(src):
    repo = src["repo"]
    code, body = get(f"{API}/repos/{repo}/releases/latest", "application/vnd.github+json", auth=True)
    if code == 404:
        return {"status": "unavailable", "reason": f"no published release of {repo} (or the repository is private)"}
    if code != 200:
        raise Transient(f"HTTP {code} for {repo}")
    rel = json.loads(body)
    tag = rel.get("tag_name") or ""
    assets = {a["name"]: a for a in rel.get("assets", [])}
    if src.get("asset_pattern"):
        names = [n for n in assets if fnmatch.fnmatchcase(n, src["asset_pattern"])]
        name = sorted(names)[0] if names else None
    else:
        name = src.get("asset") if src.get("asset") in assets else None
    if not name:
        return {"status": "unavailable", "reason": f"release {tag} has no asset {src.get('asset_pattern') or src.get('asset')}"}
    url = f"{GH}/{repo}/releases/download/{tag}/{name}"
    sha = None
    if name + ".sha256" in assets:
        code, body = get(url + ".sha256")
        if code != 200:
            raise Transient(f"HTTP {code} for {name}.sha256")
        m = HEX64.search(body.decode("utf-8", "replace"))
        sha = m.group(0).lower() if m else None
    if not sha:
        digest = assets[name].get("digest") or ""
        if digest.startswith("sha256:") and len(digest) == 71:
            sha = digest[7:].lower()
    if not sha:
        return {"status": "unavailable", "reason": "release has no checksum"}
    version = tag[1:] if re.match(r"^[vV][0-9]", tag) else tag
    return {"status": "ok", "version": version, "tag": tag, "asset": name, "url": url,
            "sha256": sha, "size": assets[name].get("size")}


def previous_blocks(url):
    if not url:
        return {}
    try:
        code, body = get(url)
        if code != 200:
            return {}
        doc = json.loads(body)
    except (Transient, ValueError):
        return {}
    out = {}
    for a in doc.get("apps", []):
        for pk, pv in a.get("platforms", {}).items():
            if isinstance(pv, dict) and pv.get("resolved"):
                out[(a["id"], pk)] = pv["resolved"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--media", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--previous")
    a = ap.parse_args()
    with open(a.catalog, encoding="utf-8") as f:
        doc = json.load(f)
    prev = previous_blocks(a.previous)
    failed = 0
    for app in doc["apps"]:
        for pk, pv in app.get("platforms", {}).items():
            src = pv.get("source") or {}
            if src.get("type") != "github-release" or pv.get("resolved"):
                continue
            try:
                pv["resolved"] = resolve(src)
            except Transient as e:
                failed += 1
                if (app["id"], pk) in prev:
                    pv["resolved"] = prev[(app["id"], pk)]
                print(f"{app['id']}[{pk}]: {e}; {'kept the published block' if 'resolved' in pv else 'left unresolved'}",
                      file=sys.stderr)
            r = pv.get("resolved", {})
            print(f"{app['id']}[{pk}]: {r.get('status')} {r.get('version') or r.get('reason') or ''}")
    base = a.base_url.rstrip("/") + "/"
    doc["media_base"] = base + "media/"
    doc["resolved_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if os.path.isdir(a.out):
        shutil.rmtree(a.out)
    os.makedirs(a.out)
    if os.path.isdir(a.media):
        shutil.copytree(a.media, os.path.join(a.out, "media"))
    with open(os.path.join(a.out, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"resolved catalog written to {a.out} ({failed} transient failures)")


if __name__ == "__main__":
    main()
