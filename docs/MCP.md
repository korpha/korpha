# MCP servers — wire external tools into your cofounder

**Audience**: anyone who wants Korpha to access tools that aren't
built-in (filesystem, GitHub, Linear, Notion, etc.).

[Model Context Protocol](https://modelcontextprotocol.io) (MCP) is
Anthropic's open spec for connecting LLMs to external tools. It's
become the de-facto standard — Claude Desktop, Cursor, Continue,
and now Korpha all speak it. Any MCP server out there works with
Korpha unmodified.

---

## Quick start: filesystem access

```bash
# Add the filesystem server (lets agents read/write files in a directory)
cat > ~/.korpha/mcp.yaml << 'EOF'
servers:
  - name: filesystem
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/Users/you/projects"]
    enabled: true
EOF

# Restart the server so the loop picks up the new manifest
# (or hit /api/mcp/rescan if running)
```

After adding, list what's available:

```bash
korpha mcp-list
```

You should see `filesystem` registered with its tools (read_file,
write_file, list_directory, etc.). The agents can now call them.

---

## Manifest format

`~/.korpha/mcp.yaml`:

```yaml
servers:
  - name: filesystem                 # required: stable id
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/path"]
    env:                             # optional: extra env vars
      MCP_LOG_LEVEL: "info"
    cwd: "/optional/working/dir"     # optional: server's cwd
    enabled: true                    # optional: default true
    request_timeout_seconds: 60      # optional: defaults to request_timeout()

  - name: github
    command: ["npx", "-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "{{env.GITHUB_TOKEN}}"
    enabled: true
```

Templating: `{{env.NAME}}` substitutes from your shell env (or
`~/.korpha/.env`). Stops you from pasting raw tokens into YAML.

---

## Common MCP servers

### Built by Anthropic

| Server | What it gives you | Install |
| --- | --- | --- |
| `server-filesystem` | Read/write files in a sandboxed dir | `npx @modelcontextprotocol/server-filesystem <dir>` |
| `server-github` | Issues / PRs / repo browsing | `npx @modelcontextprotocol/server-github` |
| `server-postgres` | Query a Postgres database | `npx @modelcontextprotocol/server-postgres <url>` |
| `server-puppeteer` | Browser automation (alternative to Korpha's built-in) | `npx @modelcontextprotocol/server-puppeteer` |
| `server-slack` | Read / post to Slack | `npx @modelcontextprotocol/server-slack` |
| `server-google-drive` | Read Google Docs / Sheets | `npx @modelcontextprotocol/server-gdrive` |

Full list: https://github.com/modelcontextprotocol/servers

### Community / third-party

The [MCP servers GitHub topic](https://github.com/topics/mcp-server)
has hundreds. Common ones:

- Linear, Notion, Airtable, Stripe, HubSpot
- Brave Search, Tavily, Exa (web search)
- Memory backends (Mem0, Chroma)
- Email (Gmail, IMAP)

Install pattern is consistent: most ship as npm packages or
PyPI packages, you point Korpha at the binary or `npx` invocation.

---

## How agents discover + call MCP tools

When an MCP server is enabled, Korpha:

1. Spawns the server as a subprocess at startup (or on `mcp-list`)
2. Calls the MCP `initialize` handshake — server returns its tool
   manifest
3. Registers the tools in the agent's available-tools list
4. When the agent reasons "I need to read a file," the LLM emits a
   tool-use call, Korpha routes it to the appropriate MCP server,
   server executes, response goes back to the agent

The tools appear in agent prompts as `<server_name>.<tool_name>`
(e.g. `filesystem.read_file`).

---

## Per-business MCP scoping

You can scope MCP servers per business — for `widgetco` to only see
its own filesystem, not `otherbiz`'s:

```yaml
servers:
  - name: widgetco-fs
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/projects/widgetco"]
    business: widgetco                  # scope to this business only
    enabled: true

  - name: otherbiz-fs
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/projects/otherbiz"]
    business: otherbiz
    enabled: true
```

Without `business:`, the server is available to all businesses on
this Korpha install.

---

## Security model

MCP servers are **subprocesses you spawn**. Korpha doesn't
sandbox them beyond what the server itself implements. Implications:

- A `server-filesystem` pointed at `/` has root read/write — don't
  do that. Always scope to a project subdir.
- A `server-github` with your PAT can do anything that PAT can —
  use a PAT with the minimum scopes you need.
- Prefer official Anthropic-built servers + audit-able community
  ones. The MCP ecosystem is young; bad actors are possible.

Best practice: one MCP server per concrete need, scoped to the
narrowest path / repo / token possible.

---

## Listing + diagnostics

```bash
korpha mcp-list                  # which servers are configured + their tool counts
korpha mcp-list --tools          # full tool listing
```

`korpha doctor` doesn't report MCP status by default (servers are
optional). If something's broken, `mcp-list` is the place to see it
— it surfaces handshake failures, missing binaries, and timeout
errors.

---

## Troubleshooting

**"npx: command not found"**
→ Install Node.js: https://nodejs.org. Most MCP servers ship as
npm packages and need npx to invoke.

**"MCP handshake timed out"**
→ The server didn't respond within `request_timeout_seconds`
(default 60). Some servers do heavy init (downloading models, etc.);
bump the timeout in your manifest entry.

**"Server: tool not found"**
→ The agent tried to call a tool the MCP server doesn't actually
expose. Either the LLM hallucinated the tool name, OR the manifest
the server reported back is stale. Restart the server (re-spawn
clears any cached state).

**Server crashes on startup**
→ Check `~/.korpha/activity.log` for the stderr of the spawn.
Usually a missing env var or wrong command path.

---

## Reference

- MCP spec: https://modelcontextprotocol.io
- Anthropic-built servers: https://github.com/modelcontextprotocol/servers
- Korpha MCP client: [`korpha/mcp/client.py`](../korpha/mcp/client.py)
- Korpha MCP config: [`korpha/mcp/config.py`](../korpha/mcp/config.py)
- CLI: `korpha mcp-list` (everything else is via the YAML manifest)
