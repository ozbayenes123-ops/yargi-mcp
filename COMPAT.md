# COMPAT — yargi sunucusunu diger uygulamalara baglama

Tek komut: `uvrun --project C:\dev\mcp\yargi-mcp yargi-mcp`
Bu komut asagidaki uclude aynen boyle kullanilir.

## Claude Desktop
Konum: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "yargi": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "C:\\dev\\mcp\\yargi-mcp",
        "yargi-mcp"
      ]
    }
  }
}
```

## Codex CLI
Konum: `%USERPROFILE%\.codex\config.toml` (satirlari ekleyin)

```toml
# Codex CLI: %USERPROFILE%\.codex\config.toml

[mcp_servers.yargi]
command = "uv"
args = ["run", "--project", "C:\dev\mcp\yargi-mcp", "yargi-mcp"]

```

## ChatGPT (masaustu/web)
Yerel stdio sunuculari baglanti olarak dogrudan takilmaz; relay/Developer Mode gerekir.
Surum: `cmdc-stack/mcp/hostconfigs/CHATGPT.md`

> Merkezi kurulum/senkron icin: `cmdc-stack` deposu (`scripts/install.ps1`) tumunu
> tek manifestten kaydeder (Hermes / CommandCode dahil).
