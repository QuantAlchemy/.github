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
- configured `required_robots_disallow_paths` stay blocked for `User-agent: *`. Use this contract for private application routes and callbacks that should not be crawled. These paths are not fetched because their exclusion is the behavior under test.
- repositories with non-production homepage metadata are explicitly classified as `prototype`, `client`, or `retired`. The audit verifies each configured homepage policy. Non-production links are valid owner navigation shortcuts, not declarations of a public product. `REPOSITORY_HOMEPAGE_SHOULD_BE_EMPTY` applies only when an explicit owner-approved policy requires an empty field.
- every normal run inventories GitHub organizations derived from the repository owners in `sites` and `repository_homepages`. A nonempty homepage without either policy produces `REPOSITORY_HOMEPAGE_UNCLASSIFIED` with the repository URL and exact observed homepage. No discovery flag is required.

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

The default mode is read-only and prints copyable `gh api` commands. Apply mode checks the homepage returned by every GitHub update and stops if GitHub does not persist the canonical value. The same command also clears homepage fields for classified non-production repositories whose `expected_homepage` is empty.

## Non-production repository classification

Add public production sites to `sites`. Add repository-owned deployments that must not enter the production fleet to `repository_homepages`:

```json
{
  "repository": "QuantAlchemy/example",
  "classification": "prototype",
  "expected_homepage": "https://example-dev.vercel.app",
  "note": "Internal prototype. Preserve the owner navigation shortcut; exclude from the production fleet."
}
```

Use `prototype` for experiments, `client` for client-owned work that is not a QuantAlchemy production surface, and `retired` for superseded products. `expected_homepage` is required and must be a string. Set it to the intended HTTPS origin to preserve an owner-useful shortcut, including a private or authenticated deployment. Non-production status alone is not a reason to clear the field. Use an explicit empty string only when the owner has approved removing an obsolete link. A repository may appear only once across `sites` and `repository_homepages`; duplicate policies are rejected before any audit or update.

The following repositories are dev/test only. Their classification remains `prototype`, and their homepage links are preserved for owner navigation:

- `QuantAlchemy/hello-convex-workos`: `https://hello-convex-workos.vercel.app`
- `QuantAlchemy/insights-alembic`: `https://insights-alembic.vercel.app`
- `QuantAlchemy/ucount-self-headless`: `https://ucount-self-headless.vercel.app`

The real Count site is under the Count organization. Do not assign a guessed Count canonical URL to `QuantAlchemy/ucount-self-headless`. The eight configured production sites, including Netly's public contract, remain unchanged. `solbeauty` remains a client site, and `trading-journal` remains retired.

Classification keeps these links out of the production fleet; it does not fetch or validate their deployments. Keeping or removing a GitHub homepage does not establish deployment privacy, authentication, or indexability. Assess those separately with authorized deployment controls when needed.

Morning Edge and other dynamic inventory jobs must use this audit receipt as the classification source of truth. They must not treat every non-empty GitHub homepage field as a QuantAlchemy production website. The former empty-homepage findings for these three prototypes were policy false positives, not unresolved cleanup or router leaks.

## Inventory coverage and failure contract

Discovery is read-only. It reads repository metadata through the authenticated `gh` CLI. It does not fetch discovered homepage URLs, add sites or policies, or update GitHub settings. An owner must classify each new finding before it can enter the production fleet. The owner-action tool still acts only on configured targets.

Organization owners are matched without case sensitivity and queried once in sorted order. The inventory requests all repository types, including archived and forked repositories, in pages of 100. It stops after a short or empty page. Each request has a 10-second deadline and a 5 MiB combined stdout/stderr limit. The runner requires POSIX process support. The limit is 100 pages per organization; reaching it without a terminal page is an operational failure, not complete coverage.

Each page must be a JSON array. Every row must contain a valid `full_name` for the requested organization and an explicit `homepage` string or null. Null means no homepage. Duplicate repositories are matched without case sensitivity and counted once. Identical duplicates use deterministic spelling; conflicting homepage values fail the inventory. Homepage strings are not normalized. Strings must be valid UTF-8, and Markdown observations use code blocks that cannot be closed by the observed text. A malformed page is not counted; validated earlier pages and their findings remain in the receipt.

Coverage is limited to repositories visible to the current credential. A successful response cannot prove that the credential can see every private repository. Use a credential with metadata read access to all intended repositories. Missing CLI authentication, API errors, rate limits, invalid responses, and time or output limits produce `REPOSITORY_INVENTORY_LOOKUP_FAILED`. An empty inventory is valid but proves only that no repositories were returned. Configurations without repository owners have no organization inventory; legacy site-list configurations still derive owners from their repositories.

JSON receipts include a `repositoryInventory` array. Each organization reports `complete`, `healthy`, `pageCount`, `checkedRepositories`, `nonemptyHomepages`, `classifiedRepositories`, `unclassifiedHomepages`, and `findings`. Counts refer to unique repositories in validated pages. `classifiedRepositories` counts visible repositories with either policy, including those with empty homepages. `unclassifiedHomepages` counts visible repositories with a nonempty homepage and no policy. Markdown reports the same coverage counts and findings. The top-level JSON `healthy` value requires healthy site, homepage-policy, and inventory audits.

Exit statuses:

| Status | Meaning |
| --- | --- |
| `0` | All configured checks and visible inventory checks are healthy. |
| `1` | Defects exist, including unknown nonempty homepages or classified homepage drift. |
| `2` | A GitHub homepage or inventory lookup failed. Requested receipts are still written. This status takes priority over defects from successful checks or earlier pages. |

No repository Administration write permission is needed to accept the preserved prototype links. Do not run homepage removal as a follow-up for these repositories. The owner-action tool remains available for separately authorized metadata corrections, including the existing retired-repository policy.

## Local verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
python3 -B tools/seo_fleet_audit.py \
  --config config/public-sites.json \
  --markdown-out /tmp/weekly-seo-fleet-audit.md \
  --json-out /tmp/weekly-seo-fleet-audit.json
```

Add a public production site by adding its canonical HTTPS origin and source repository to `config/public-sites.json`. Use root-relative `expected_text_paths`, `expected_json_paths`, `required_canonical_paths`, `required_noindex_paths`, and `required_robots_disallow_paths` only for artifacts and routes that are part of that site's explicit public contract. Authenticated/internal-only products should not be added. A signed-in route of a public site belongs in `required_noindex_paths` when a signed-out visitor still gets HTML there and search engines need to read its `noindex`; use `required_robots_disallow_paths` for private routes and callbacks that should not be crawled at all. The two exclusion modes must not overlap.
