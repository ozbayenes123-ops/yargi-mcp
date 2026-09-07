Set-Location 'C:\dev\mcp\yargi-mcp'
Remove-Item .\stdio_test.done, .\stdio_out.txt -ErrorAction SilentlyContinue
& '.\.venv\Scripts\python.exe' stdio_handshake.py *> .\stdio_out.txt
if ($LASTEXITCODE -eq 0) { Set-Content .\stdio_test.done 'OK' } else { Set-Content .\stdio_test.done 'FAIL' }
