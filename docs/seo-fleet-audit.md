# Public-site fleet SEO audit

`tools/seo_fleet_audit.py` is the executable evidence layer for Quant Alchemy's public search-discovery surfaces. The production sensor is scheduled through Hermes every Monday at 13:15 UTC, and the crawler can also be run manually.

For every site in `config/public-sites.json`, the crawler verifies:

- the GitHub repository homepage matches the configured canonical production origin;
- the root resolves to canonical HTTP 200 HTML;
- the root canonical link, when declared, matches the configured production origin;
- `robots.txt` is direct, plain text, and references the canonical sitemap;
- every robots sitemap reference stays on the canonical host and serves parseable XML;
- every sitemap and nested sitemap-index `<loc>` is fetched;
- every page `<loc>` stays on the canonical host, returns direct HTTP 200, and—when HTML declares a canonical—uses the same URL.
- configured `expected_text_paths` such as `llms.txt` and `ai.txt` return direct HTTP 200 plain text;
- configured `expected_json_paths` such as `claim-receipts.json` return direct HTTP 200 as the exact `application/json` media type (parameters such as `charset` are allowed) and parse as strict RFC 8259 JSON; redirects, `NaN`, and infinities fail the contract;
- configured `required_canonical_paths` return direct HTTP 200 HTML and declare exactly one matching canonical link;
- configured `required_noindex_paths` return direct HTTP 200 HTML, declare `noindex` through a `robots` meta tag or an `X-Robots-Tag` header, and stay crawlable for `User-agent: *`. A page `robots.txt` disallows never gets its `noindex` read, so that combination is reported as `REQUIRED_NOINDEX_UNREACHABLE` rather than passing.

The run produces Markdown and JSON receipts with the exact requested URL, expected result, observed status/final URL, and defect code. The scheduled Hermes job delivers the Markdown receipt to the task thread; JSON is retained locally for machine processing. An authenticated `gh` CLI is required because some mapped repositories are private. A GitHub lookup failure exits with operational status `2`; homepage drift remains the normal defect status `1`.

## Repository homepage owner action

Repository homepage settings require repository administration permission. Preview the exact settings that differ from the canonical fleet map:

```bash
python3 tools/update_repo_homepages.py
```

Apply and verify only the reported differences with an authenticated owner credential:

```bash
gh auth status
python3 tools/update_repo_homepages.py --apply
```

The default mode is read-only and prints copyable `gh api` commands. Apply mode checks the homepage returned by every GitHub update and stops if GitHub does not persist the canonical value.

## Local verification

```bash
python3 -m unittest tests/test_seo_fleet_audit.py tests/test_update_repo_homepages.py -v
python3 tools/seo_fleet_audit.py \
  --config config/public-sites.json \
  --markdown-out /tmp/weekly-seo-fleet-audit.md \
  --json-out /tmp/weekly-seo-fleet-audit.json
```

Add a public production site by adding its canonical HTTPS origin and source repository to `config/public-sites.json`. Use root-relative `expected_text_paths`, `expected_json_paths`, `required_canonical_paths`, and `required_noindex_paths` only for artifacts and routes that are part of that site's explicit public contract. Authenticated/internal-only products should not be added. A signed-in route of a public site belongs in `required_noindex_paths` when a signed-out visitor still gets HTML there, so the crawler can prove the page is kept out of the index instead of reading as a copy of the home page.
