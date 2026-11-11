# deploy-adhoc-ipa

A GitHub Action that takes an ad-hoc signed ipa and produces a one-click install link, for devices registered to your Apple Developer account. Uses Cloudflare Pages and R2.

(Made with lots of help from Claude, but verified to work)

## Usage

1. Create a Cloudflare API token with the **Account → Cloudflare Pages → Edit**.
    - If your IPAs can exceed 25 MiB, you also need **Account → Workers R2 Storage → Edit**
2. In your GitHub Workflow, build an `ad-hoc` signed ipa
3. Invoke this action with the path to the ipa + the API token

```yaml
jobs:
  adhoc:
    runs-on: macos-latest
    permissions:
      contents: read
      pull-requests: write # for the PR comment
    steps:
      # ...build and export an ad-hoc signed IPA
      - uses: kabiroberai/deploy-adhoc-ipa@v1
        with:
          ipa: out/MyApp.ipa
          cloudflare-api-token: ${{ secrets.CLOUDFLARE_API_TOKEN }}
```

## Requirements

The action runs on macOS and Linux. Standard GitHub-hosted runners have all prerequisites installed.

If you're self-hosting a runner, install Python3, Node.js 22+, and `gh`.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `ipa` | **required** | Path to the `.ipa` |
| `cloudflare-api-token` | **required** | Cloudflare API token with **Pages: Edit** (and **Workers R2 Storage: Edit** for IPAs over 25 MiB) |
| `r2-bucket` | the project name | R2 bucket for IPAs over 25 MiB |
| `project-name` | `<repo name>-ipa-installer` | Cloudflare Pages project |
| `cloudflare-account-id` | inferred | Required only if the token can access multiple accounts |
| `branch` | `pr-<number>` or the ref name | Pages branch to deploy to |
| `production-branch` | `main` | Production branch for a newly created project |
| `base-url` | the `pages.dev` URL | Public URL the deployment is served from, e.g. a custom domain |
| `title` | the app name | Title on the page and in the iOS install prompt |
| `comment-on-pr` | `true` | Post and keep updating a comment with the install link on PRs |
| `github-token` | `github.token` | Token used to comment on PRs |
| `wrangler-version` | automatic | Version of wrangler to deploy with |

## Outputs

| Output | Description |
| --- | --- |
| `url` | URL of the install page |
| `manifest-url` | URL of `manifest.plist` |
| `install-url` | The `itms-services://` link |
| `qr-url` | URL of an SVG QR code for the `itms-services://` link |
| `ipa-url` | Download URL of the `.ipa` |
| `expires-at` | When the `.ipa` download link expires (ISO 8601), or empty if it doesn't |

## Notes

- Only devices in the IPA's provisioning profile can install it, but anyone
  with the link can download the (unencrypted) IPA. The link appears in the PR
  comment and job summary, so treat it accordingly in public repositories.
- Pages caps files at 25 MiB, so larger IPAs will be uploaded to an R2 bucket
  instead, with a presigned download link that expires after 7 days.
  The bucket is created on first use and deletes IPAs after 8 days.
- iOS fetches the manifest and IPA without Safari's cookies, so the deployment
  can't sit behind Cloudflare Access or other cookie-based auth.
