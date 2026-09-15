# Manual OAuth checklist (Phase 0)

Desktop-only. CI uses Fake MCP — never run this in automated tests.

## Candidate OAuth endpoints (confirm before first auth)

Re-check live discovery if anything 404s / rejects:

```sh
curl -s https://agent.robinhood.com/.well-known/oauth-authorization-server | python3 -m json.tool
```

Expected candidates (documented 2026-07-20):

| Role | URL |
|------|-----|
| authorize | `https://robinhood.com/oauth` |
| token | `https://api.robinhood.com/oauth2/token/` |
| register | `https://agent.robinhood.com/oauth/trading/register` |
| MCP | `https://agent.robinhood.com/mcp/trading` |

PKCE: S256. Public client (no client secret). Scope: `internal`.

## Operator steps

1. **Create / fund Agentic account** on a desktop browser via Robinhood (mobile cannot complete setup).
2. Copy config: `cp config/agentic.example.toml config/agentic.toml` and edit paths if needed.
3. **Auth:** `agentic-trading auth --config config/agentic.toml`  
   - Opens/prints authorize URL; local `127.0.0.1` redirect or paste-code fallback.  
   - Tokens land at `token_path` with mode `0600` (never commit).
4. **Snapshot tools:** `agentic-trading snapshot-tools --config config/agentic.toml`  
   - Writes `tools_snapshot_path` + dated sibling.
5. **Shadow run:** `agentic-trading run --config config/agentic.toml`  
   - Default is shadow (review/simulate only; no place).  
   - Live requires `flip-mode live` **and** `AGENTIC_ALLOW_LIVE=1`.

## If auth fails

- Confirm endpoints via discovery (table above may drift).
- Ensure desktop browser + loopback redirect allowed.
- Delete stale `token_path` and re-run `auth`.
