$path = "C:\Users\steve\prospect-engine\server\api.py"
$src = [System.IO.File]::ReadAllText($path)

$func = @"
@app.get("/hot-leads", response_class=HTMLResponse)
async def hot_leads_page():
    return HTMLResponse((UI_DIR / "hot_leads.html").read_text(encoding="utf-8"))


@app.get("/api/contacts/{contact_id}/generate-recommendations")
"@

if ($src -match 'def hot_leads_page') {
  Write-Host "ALREADY PRESENT - no change made"
} elseif ($src -notmatch [regex]::Escape('@app.get("/api/contacts/{contact_id}/generate-recommendations")')) {
  Write-Host "ERROR - anchor not found"
} else {
  $src = $src -replace [regex]::Escape('@app.get("/api/contacts/{contact_id}/generate-recommendations")'), $func
  $enc = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($path, $src, $enc)
  Write-Host "DONE - hot-leads page route inserted"
}