# Kimi quota fixtures

`limit-observed-redacted` preserves the visible ordering and editor-frame geometry
of a quota failure. User turns, response content, paths, links, and footer values
are replaced with neutral placeholders; the quota error is normalized to its
provider code and quota category. No conversation text or credentials are retained.
Blank/nonblank rows and native marker types remain in order. It is a redacted
screen structure, not an unmodified capture or a successful recovery recording.

The other `limit-*` fixtures are synthetic positive and negative cases built from
the observed error form and known editor layout. Recovery is synthetic; these
fixtures do not demonstrate a live subscription recovering or all CLI versions.
