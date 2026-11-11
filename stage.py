#!/usr/bin/env python3

"""Stage an IPA, its itms-services manifest, and an install page for Cloudflare Pages.

Writes `dir`, `page-dir`, `project`, `account-id`, `url`, `manifest-url`, `qr-url`, `install-url`, `title` and `version` to
$GITHUB_OUTPUT (or stdout when run locally).
"""

import argparse
import html
import json
import os
import plistlib
import re
import secrets
import shutil
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"


def cloudflare(method, path, body=None):
    request = urllib.request.Request(
        f"{CLOUDFLARE_API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)["result"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        sys.exit(f"Cloudflare API {method} {path} failed ({error.code}): {error.read().decode()}")


def account_id():
    if os.environ.get("CLOUDFLARE_ACCOUNT_ID"):
        return os.environ["CLOUDFLARE_ACCOUNT_ID"]
    accounts = cloudflare("GET", "/accounts?per_page=2") or []
    if len(accounts) != 1:
        sys.exit(f"The Cloudflare API token can access {len(accounts)} accounts; set cloudflare-account-id")
    return accounts[0]["id"]


def pages_subdomain(account, project, production_branch):
    """Returns the project's *.pages.dev host, creating the project if needed.

    This isn't always `<project>.pages.dev`: Cloudflare adds a suffix when
    that name is already taken by another account.
    """
    projects = f"/accounts/{account}/pages/projects"
    result = cloudflare("GET", f"{projects}/{project}")
    if result is None:
        print(f"Creating Cloudflare Pages project '{project}'", file=sys.stderr)
        result = cloudflare("POST", projects, {"name": project, "production_branch": production_branch})
    return result["subdomain"]


def default_project():
    # Pages project names are lowercase alphanumerics and hyphens, at most 58 characters.
    repo = os.environ["GITHUB_REPOSITORY"].split("/")[-1]
    suffix = "-ipa-installer"
    return re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")[:58 - len(suffix)] + suffix


def branch_alias(branch):
    # Mirrors how Pages derives preview aliases from branch names.
    return re.sub(r"[^a-z0-9]", "-", branch.lower())[:28].strip("-")


def read_info_plist(ipa):
    with zipfile.ZipFile(ipa) as archive:
        name = next(n for n in archive.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n))
        return plistlib.loads(archive.read(name))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipa", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--project", default="")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--production-branch", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--commit", default="")
    args = parser.parse_args()

    project = args.project or default_project()

    # With a custom base URL, nothing here needs the API; wrangler resolves the account itself.
    account = ""
    base_url = args.base_url.rstrip("/")
    if not base_url:
        account = account_id()
        host = pages_subdomain(account, project, args.production_branch)
        if args.branch != args.production_branch:
            host = f"{branch_alias(args.branch)}.{host}"
        base_url = f"https://{host}"

    # Serve everything under a fresh unguessable path, so only people given
    # the link can fetch the IPA. Each deploy replaces the branch's previous one.
    token = secrets.token_urlsafe(16)
    base_url = f"{base_url}/{token}"

    info = read_info_plist(args.ipa)
    bundle_id = info["CFBundleIdentifier"]
    version = info.get("CFBundleShortVersionString") or info["CFBundleVersion"]
    build = info.get("CFBundleVersion", "")
    title = args.title or info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle_id

    shutil.rmtree(args.out, ignore_errors=True)
    page = args.out / token
    page.mkdir(parents=True)
    ipa_name = args.ipa.name
    shutil.copyfile(args.ipa, page / ipa_name)

    manifest_url = f"{base_url}/manifest.plist"
    install_url = "itms-services://?action=download-manifest&url=" + urllib.parse.quote(manifest_url, safe="")
    manifest = {
        "items": [{
            "assets": [{"kind": "software-package", "url": f"{base_url}/{urllib.parse.quote(ipa_name)}"}],
            "metadata": {
                "bundle-identifier": bundle_id,
                "bundle-version": version,
                "kind": "software",
                "title": title,
            },
        }],
    }
    (page / "manifest.plist").write_bytes(plistlib.dumps(manifest))

    # Consumed by Pages, not served.
    (args.out / "_headers").write_text(
        "/*\n  X-Robots-Tag: noindex\n  Referrer-Policy: no-referrer\n  Cache-Control: no-cache\n"
        "/:token/manifest.plist\n  Content-Type: text/xml\n"
    )
    # Without a top-level 404.html, Pages treats the site as a SPA and
    # answers every path instead of returning 404.
    (args.out / "404.html").write_text("<!doctype html><title>Not found</title>Not found\n")

    template = string.Template((Path(__file__).parent / "index.html").read_text())
    (page / "index.html").write_text(template.substitute(
        title=html.escape(title),
        version=html.escape(f"{version} ({build})" if build and build != version else version),
        bundle_id=html.escape(bundle_id),
        commit=html.escape(args.commit[:7]),
        install_url=html.escape(install_url),
    ))

    outputs = {
        "dir": str(args.out),
        "page-dir": str(page),
        "project": project,
        "account-id": account,
        "url": f"{base_url}/",
        "manifest-url": manifest_url,
        "qr-url": f"{base_url}/qr.svg",
        "install-url": install_url,
        "title": title,
        "version": version,
    }
    lines = "".join(f"{key}={value}\n" for key, value in outputs.items())
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as file:
            file.write(lines)
    else:
        print(lines, end="")


if __name__ == "__main__":
    main()
