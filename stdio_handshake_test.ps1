$ErrorActionPreference = 'Stop'
$proj = 'C:\Users\ozbayenes123-ops\Desktop\yargi-mcp'
Set-Location $proj

# MCP stdio handshake: initialize -> notifications/initialized -> tools/list
$rpc1 = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"cline-test","version":"1.0"}}}'
$rpc2 = '{"jsonrpc":"2.0","method":"notifications/initialized"}'
$rpc3 = '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
$input = ($rpc1 + "`n" + $rpc2 + "`n" + $rpc3 + "`n")

# Pipe JSON-RPC into the stdio server; capture stdout, give server time, then EOF.
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = 'uv'
$psi.Arguments = 'run --project "C:\Users\ozbayenes123-ops\Desktop\yargi-mcp" yargi-mcp'
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$p = [System.Diagnostics.Process]::Start($psi)

# write all requests then close stdin so the server can finish and exit
$p.StandardInput.Write($input)
$p.StandardInput.Flush()
$p.StandardInput.Close()

# collect stdout
$out = $p.StandardOutput.ReadToEnd()
$err = $p.StandardError.ReadToEnd()
if (-not $p.WaitForExit(60000)) { try { $p.Kill() } catch {} }
Set-Content -Path "$proj\stdio_stdout.txt" -Value $out -Encoding utf8
Set-Content -Path "$proj\stdio_test.log" -Value $err -Encoding utf8
if ($p.ExitCode -eq 0 -or $out) { Set-Content "$proj\stdio_test.done" 'OK' } else { Set-Content "$proj\stdio_test.done" 'FAIL' }
