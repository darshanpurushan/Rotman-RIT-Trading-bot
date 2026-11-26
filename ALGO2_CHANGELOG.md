# ALGO2 market maker changelog

This changelog covers the RIT simulator market maker. The v1.0 and v3.0–v3.7 entries are historical notes; their original source snapshots are not included here.

| Version | What changed |
| --- | --- |
| v1.0 | Initial inventory-aware market maker: passive bid and ask quotes, position limits, and end-of-round closeout. |
| [v1.1](https://github.com/darshanpurushan/Rotman-RIT-Trading-bot/commit/153882604f4e77d6cd302385af6f89e4acf0f902) | Kept reservations for acknowledged orders while waiting for the API to show them, including when order detail returned 404. |
| [v2.0](https://github.com/darshanpurushan/Rotman-RIT-Trading-bot/commit/0388dea523f36b2611ccbbc3208c7fea0ea8fb23) | Added publication grace and paced cancellation retries, and let trading resume after transient read failures. Ambiguous submissions still block replacement. |
| v3.0 | Added continuous rounds, clean reset checks, and latency-sensitive end-of-round timing. Taper begins at tick 240, with latest reduce-only and flatten deadlines at 290 and 294. |
| v3.1 | Refined pricing in narrow spreads, capped closing orders to the remaining position, and improved cancellation and order visibility handling. |
| v3.2 | Kept the opposite quote active during eligible cancellation waits, expanded fill diagnostics, and added optional `--adaptive-queue`. |
| v3.3 | Added read-only `--preflight`, tracked observed round resets explicitly, fixed queue repricing, and stopped cancelling orders already reported fully filled. |
| v3.4 | Extended optional queue improvement to small residual positions, with reduce-only sizing so an exit cannot open inventory on the opposite side. |
| v3.5 | Added optional `--aged-exit` to improve the reducing quote for a position larger than 500 shares and no larger than the configured order size after a 10-second fill gap. |
| v3.6 | Added optional `--two-levels` to split each side's existing size between two prices. The ladder returns to one level at the start of taper. |
| v3.7 | Made ladder repricing independent by side, added per-level diagnostics, and allowed a tighter first quote under qualifying queue conditions. |
| v3.8 | Allowed the standard single-level strategy to refill the opposite side while a cancellation is unresolved, subject to fresh order and position checks. |

The current defaults, command, and validation instructions are in the [ALGO2 guide](ALGO2_README.md).
