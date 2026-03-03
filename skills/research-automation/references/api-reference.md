# API Reference

Verified API endpoints for paper search, SOTA discovery, and metadata retrieval.
Venue IDs from OpenReview docs. S2 Bulk API tested with CVPR 2025 (1934 papers). CVF URL tested.

## OpenReview API v2

**Covers**: NeurIPS, ICML, ICLR, CVPR, ECCV (all hosted on OpenReview)

**Two search parameters** (different!):
- `venueid` = structural ID (e.g., `thecvf.com/CVPR/2025/Conference`) — for filtering accepted papers
- `venue` = display name (e.g., `CVPR 2025`) — simpler, tested in CVPR 2025 scout (1783 papers)

```python
import openreview
client = openreview.api.OpenReviewClient(baseurl='https://api2.openreview.net')

# Primary: search by structural venueid (from OpenReview docs)
# NeurIPS 2024
papers = client.get_all_notes(content={'venueid': 'NeurIPS.cc/2024/Conference'})
# CVPR 2024
papers = client.get_all_notes(content={'venueid': 'thecvf.com/CVPR/2024/Conference'})
# ICML 2024
papers = client.get_all_notes(content={'venueid': 'ICML.cc/2024/Conference'})
# ICLR 2025
papers = client.get_all_notes(content={'venueid': 'ICLR.cc/2025/Conference'})
# ECCV 2024
papers = client.get_all_notes(content={'venueid': 'thecvf.com/ECCV/2024/Conference'})

# Fallback: if venueid returns empty, try display name
papers = client.get_all_notes(content={'venue': 'CVPR 2025'})
```

**Key details**:
- No authentication needed (guest mode for public papers)
- Auto-pagination (1000 per batch, `get_all_notes` handles automatically)
- Field access varies by API version:
  - API v2: `note.content['title']['value']` (nested 'value' dict)
  - API v1: `note.content['title']` (flat string)
  - Handle both: check `isinstance(field, dict)` before accessing `.get('value')`
- PDF URL: `https://openreview.net/pdf?id={note.forum}`
- Install: `pip install openreview-py`
- Rate limit: No official documentation; use 500ms interval between requests

**⚠️ CVPR/ECCV**: May use API v1 format (flat content fields). The fallback with `venue` display name is confirmed to work. Cross-reference with CVF Open Access for comprehensive coverage.

## Semantic Scholar Bulk API

**Covers**: All venues via search, including non-top-conference papers.

```
GET https://api.semanticscholar.org/graph/v1/paper/search/bulk
  ?query=&venue=CVPR&year=2025
  &fields=title,abstract,year,venue,citationCount,openAccessPdf,externalIds
```

**Verified**: CVPR 2025 returns 1934 papers, 1000 per page, token-based pagination.

**Key details**:
- API key recommended but not required
  - Without key: shared pool ~5000 requests / 5 minutes (widely reported, not explicitly in official docs — use 4500 with safety margin)
  - With key: 1 RPS (search endpoints, confirmed in official docs)
  - Set via header: `x-api-key: YOUR_KEY`
- `openAccessPdf` field sometimes null; construct backup: `https://arxiv.org/pdf/{arxiv_id}`
- Pagination: response includes `token` field; pass as `?token=XXX` for next page
- `externalIds` contains `DOI`, `ArXiv`, `CorpusId`

**Rate limiting strategy**:
```
Without key: 4500 calls per 5-min window (10% safety margin)
With key: 1.1s between requests (10% safety margin on 1 RPS)
On 429: exponential backoff (1s → 2s → 4s, max 30s, jitter 500ms)
```

## CVF Open Access

**Covers**: CVPR, ECCV PDFs (direct download, no paywall)

```
URL: https://openaccess.thecvf.com/CVPR2025?day=all
```

**Key details**:
- No API — HTML scraping required
- PDF URL pattern: `https://openaccess.thecvf.com/content/CVPR2025/papers/{Author}_{Title}_CVPR_2025_paper.pdf`
- Rate limit: use 2s interval (polite crawling)
- Useful for: when OpenReview/S2 don't have the PDF

## CrossRef API

**For**: DOI-based paper metadata and BibTeX retrieval.

```
GET https://api.crossref.org/works/{DOI}
```

**BibTeX via DOI**:
```
GET https://doi.org/{DOI}
Headers: Accept: application/x-bibtex
```

**Rate limit**: 50 requests per minute (generous).

## arXiv API

**For**: arXiv paper search and metadata.

```
GET http://export.arxiv.org/api/query?search_query=ti:"{title}"&max_results=5
```

**Key details**:
- Use `feedparser` to parse Atom XML response
- Rate limit: 20 requests per minute
- arXiv ID formats: `2401.12345` (new) or `cs/0703001` (old)

## PDF Reading Strategy

**CRITICAL: WebFetch CANNOT reliably read PDFs — it will hallucinate content!**

**Correct approach**:
```bash
# Step 1: Download PDF locally (unset proxy for speed)
unset http_proxy https_proxy
curl -sL -o /tmp/paper.pdf "https://arxiv.org/pdf/2401.12345"

# Step 2: Convert to text (MANDATORY — do NOT use Read tool on PDFs)
# The Read tool's PDF parser rejects some valid academic PDFs as "not valid"
# pdftotext handles 97%+ of academic PDFs reliably
pdftotext /tmp/paper.pdf /tmp/paper.txt
# → Then use Read tool on /tmp/paper.txt (NOT the .pdf)
```

**For extracting tables from PDFs** (experiment results):
- `pdftotext -layout` preserves table formatting
- GROBID (Docker) can parse tables into structured XML (optional, more accurate)
- Always cross-validate extracted numbers with ≥2 papers

## Zotero MCP Tools

When Zotero MCP is available, use these tools for library management:

| Tool | Purpose |
|------|---------|
| `create_collection` | Create Zotero collection/sub-collection |
| `add_items_by_doi` | Import papers by DOI (auto-attaches OA PDFs) |
| `add_web_item` | Import from URL (fallback for no-DOI papers) |
| `search_library` | Search existing library (for dedup) |
| `zotero_get_item_metadata` | Get paper metadata + abstract |
| `zotero_get_item_fulltext` | Read full text of attached PDFs |
| `zotero_get_collection_items` | List items in collection |
| `find_and_attach_pdfs` | Attach PDFs to existing items |

**Fallback when Zotero unavailable**: All operations degrade to local `papers-metadata.json` management. Functionality is preserved but papers are not imported into Zotero desktop app.

## Python Dependencies

Required packages for API access:
```
requests          # HTTP client (all APIs)
openreview-py     # OpenReview API v2
feedparser        # arXiv API response parsing
```

Optional:
```
semanticscholar   # S2 Python client (alternative to raw HTTP)
arxiv             # arXiv Python client (alternative to raw HTTP)
```

Install (handle proxy/mirror as needed):
```bash
unset http_proxy https_proxy  # if default proxy is slow
pip install requests openreview-py feedparser  # add -i $MIRROR if configured
```
