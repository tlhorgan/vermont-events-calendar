# Combined Vermont Events → Proton Calendar

Combines:
- Vermont.com Calendar of Events
- Vermont Public Community Calendar
- Vermont Arts Council Arts Calendar

Output: `vermont-events.ics`

## Proton Calendar URL

`https://raw.githubusercontent.com/YOUR-GITHUB-USERNAME/vermont-events-calendar/main/vermont-events.ics`

## Deduplication

Same-date events are merged when titles are nearly identical, or when titles and locations both strongly match. The merged event records all contributing sources in its description.

## Safety

The workflow fails instead of publishing if fewer than 10 unique events are generated.
