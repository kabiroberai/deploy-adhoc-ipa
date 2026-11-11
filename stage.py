#!/usr/bin/env python3

"""Stage an IPA, its itms-services manifest, and an install page for Cloudflare Pages.

IPAs over the Pages per-file limit are uploaded to R2 instead, and the manifest
points at a presigned (expiring) download URL.

Writes `dir`, `page-dir`, `project`, `account-id`, `url`, `manifest-url`, `qr-url`, `install-url`,
`ipa-url`, `expires-at`, `expires-display`, `title` and `version` to $GITHUB_OUTPUT (or stdout when run locally).
"""

import argparse
import hashlib
import hmac
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
PAGES_FILE_LIMIT = 25 * 1024 * 1024
# As long as SigV4 presigned URLs allow.
LINK_EXPIRY = timedelta(days=7)


def cloudflare(method, path, body=None, ok_errors=(404,)):
    """Calls the Cloudflare API, returning None for any status in `ok_errors`."""
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
        if error.code in ok_errors:
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


def ensure_bucket(account, bucket):
    buckets = f"/accounts/{account}/r2/buckets"
    if cloudflare("GET", f"{buckets}/{bucket}") is not None:
        return
    print(f"Creating R2 bucket '{bucket}'", file=sys.stderr)
    cloudflare("POST", buckets, {"name": bucket})
    # Download links die within a week, so clean up shortly after.
    cloudflare("PUT", f"{buckets}/{bucket}/lifecycle", {"rules": [{
        "id": "delete-expired-ipas",
        "enabled": True,
        "conditions": {"prefix": ""},
        "deleteObjectsTransition": {"condition": {
            "type": "Age",
            "maxAge": int((LINK_EXPIRY + timedelta(days=1)).total_seconds()),
        }},
    }]})


def r2_credentials(account):
    # R2's S3 API accepts a Cloudflare API token as credentials: the token's
    # ID is the access key, and the SHA-256 of its value is the secret.
    verify_errors = (400, 401, 403, 404)
    verified = (cloudflare("GET", f"/accounts/{account}/tokens/verify", ok_errors=verify_errors)
                or cloudflare("GET", "/user/tokens/verify", ok_errors=verify_errors))
    if verified is None:
        sys.exit("Couldn't look up the Cloudflare API token's ID for R2")
    return verified["id"], hashlib.sha256(os.environ["CLOUDFLARE_API_TOKEN"].encode()).hexdigest()


def s3_quote(value, safe="-_.~"):
    return urllib.parse.quote(value, safe=safe)


def sigv4_signature(secret, scope, amz_date, method, path, query, headers, payload_hash):
    """AWS Signature Version 4. `headers` must have lowercase names."""
    canonical_request = "\n".join([
        method,
        path,
        "&".join(f"{s3_quote(k)}={s3_quote(v)}" for k, v in sorted(query.items())),
        "".join(f"{k}:{v.strip()}\n" for k, v in sorted(headers.items())),
        ";".join(sorted(headers)),
        payload_hash,
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    key = f"AWS4{secret}".encode()
    for part in scope.split("/"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def presign_get(host, path, access_key, secret, now, expires_in, region="auto"):
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{now:%Y%m%d}/{region}/s3/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires_in.total_seconds())),
        "X-Amz-SignedHeaders": "host",
    }
    query["X-Amz-Signature"] = sigv4_signature(
        secret, scope, amz_date, "GET", path, query, {"host": host}, "UNSIGNED-PAYLOAD",
    )
    return f"https://{host}{path}?" + "&".join(f"{s3_quote(k)}={s3_quote(v)}" for k, v in query.items())


def upload_to_r2(account, bucket, key, file, expires_in):
    """Uploads `file` to R2 and returns a presigned download URL."""
    access_key, secret = r2_credentials(account)
    ensure_bucket(account, bucket)

    host = f"{account}.r2.cloudflarestorage.com"
    path = "/" + s3_quote(f"{bucket}/{key}", safe="/-_.~")
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{now:%Y%m%d}/auto/s3/aws4_request"

    with open(file, "rb") as body:
        digest = hashlib.sha256()
        for chunk in iter(lambda: body.read(1 << 20), b""):
            digest.update(chunk)
        payload_hash = digest.hexdigest()
        body.seek(0)
        headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        signature = sigv4_signature(secret, scope, amz_date, "PUT", path, {}, headers, payload_hash)
        request = urllib.request.Request(f"https://{host}{path}", method="PUT", data=body, headers={
            **headers,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                             f"SignedHeaders={';'.join(sorted(headers))}, Signature={signature}",
            "Content-Length": str(os.fstat(body.fileno()).st_size),
            "Content-Type": "application/octet-stream",
        })
        try:
            urllib.request.urlopen(request).close()
        except urllib.error.HTTPError as error:
            sys.exit(f"R2 upload to {bucket}/{key} failed ({error.code}): {error.read().decode()}")

    return presign_get(host, path, access_key, secret, now, expires_in)


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
    parser.add_argument("--r2-bucket", default="")
    args = parser.parse_args()

    project = args.project or default_project()
    use_r2 = args.ipa.stat().st_size > PAGES_FILE_LIMIT

    # With a custom base URL and no R2 upload, nothing here needs the API;
    # wrangler resolves the account itself.
    base_url = args.base_url.rstrip("/")
    account = account_id() if use_r2 or not base_url else ""
    if not base_url:
        host = pages_subdomain(account, project, args.production_branch)
        if args.branch != args.production_branch:
            host = f"{branch_alias(args.branch)}.{host}"
        base_url = f"https://{host}"

    # Serve everything under a fresh unguessable path, so only people given
    # the link can find the IPA. Each deploy replaces the branch's previous one.
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
    expires_at = None
    if use_r2:
        ipa_url = upload_to_r2(account, args.r2_bucket or project, f"{token}/{args.ipa.name}", args.ipa, LINK_EXPIRY)
        expires_at = datetime.now(timezone.utc) + LINK_EXPIRY
    else:
        shutil.copyfile(args.ipa, page / args.ipa.name)
        ipa_url = f"{base_url}/{urllib.parse.quote(args.ipa.name)}"
    expires_display = f"{expires_at:%b %-d, %Y %H:%M} UTC" if expires_at else ""

    manifest_url = f"{base_url}/manifest.plist"
    install_url = "itms-services://?action=download-manifest&url=" + urllib.parse.quote(manifest_url, safe="")
    manifest = {
        "items": [{
            "assets": [{"kind": "software-package", "url": ipa_url}],
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
        expiry=f'<p class="meta">Link expires {html.escape(expires_display)}</p>' if expires_at else "",
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
        "ipa-url": ipa_url,
        "expires-at": f"{expires_at:%Y-%m-%dT%H:%M:%SZ}" if expires_at else "",
        "expires-display": expires_display,
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
