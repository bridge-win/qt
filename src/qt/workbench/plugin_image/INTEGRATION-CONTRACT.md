# Plugin temporal-integrity integration contract

Docker isolation protects the host and constrains resources. It does **not**
prove point-in-time integrity for submitted Python source. A plugin can open the
mounted full-history Parquet file or alter imported native modules, bypassing a
causal `StrategyContext` slice.

`IsolatedPluginRuntime` and the container entrypoint therefore emit:

```json
{
  "temporal_integrity": "unverified",
  "verified_leaderboard_eligible": false
}
```

The worker/result/leaderboard integration must preserve both fields, exclude
these runs from verified leaderboards, and avoid claiming causal verification.
This is independent of Docker network, UID, read-only, and resource controls.

## Artifact and result publishing

Plugin `result.json` is untrusted. `IsolatedPluginRuntime` rejects artifact
paths outside `/output/artifacts/<run_id>/`, then replaces every descriptor with
a relative path, hash, media type, size, and metadata source computed from the
safe copied host artifact. It never forwards container or host paths, submitted
hashes, media types, row counts, or arbitrary descriptor fields.

The R2 report publisher must still independently enforce its artifact root,
reject symlinks and non-regular/hard-linked files, recompute hashes and sizes,
and reject paths outside the copied artifact tree. It must never trust
`artifacts[].path` from a plugin result. Plugin metrics and result fields remain
`unverified_plugin_origin` until temporal-integrity audits pass. The runtime
sets top-level `metrics_integrity` and `verification.status` accordingly.

Strict future enforcement requires a separate plugin process that receives only
causal snapshots, plus lookahead and prefix-perturbation audits. AST import
restrictions and a sliced `StrategyContext` are not substitutes for that
boundary.
