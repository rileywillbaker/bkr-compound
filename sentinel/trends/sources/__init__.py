"""Free, keyless collectors for the Trend Discovery Agent.

Every module here obeys the same three rules:

1. **No key, no subscription, no paid tier.** If a source starts requiring
   payment or auth, the collector returns nothing and the run continues.
2. **Never raise into the caller.** A source being down, rate-limited,
   Cloudflare-blocked or reshaped is the normal case for free endpoints, not
   an exception. Collectors return `[]` and log; the trend score then rests on
   the sources that did answer, and the report states its coverage.
3. **Be a polite client.** Real User-Agent, conservative pacing through the
   shared rate limiter, small page sizes.

Because sources degrade independently, `collect.py` records which ones
answered so a low trend score caused by a dead feed is never mistaken for a
genuine absence of news.
"""
