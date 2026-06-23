$path = "C:\Users\steve\prospect-engine\server\api.py"
$src = [System.IO.File]::ReadAllText($path)

$anchor = 'async def track_view(request: Request):'

$func = @"
@app.get("/api/hot-leads")
async def hot_leads():
    import os as _os, httpx as _httpx
    supabase_key = _os.environ.get("SUPABASE_SERVICE_KEY", "")
    supabase_url = "https://neonmrgszujadgfidlbj.supabase.co"
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{supabase_url}/rest/v1/hot_leads?select=*", headers=headers)
            rows = r.json()
    except Exception:
        rows = []
    return {"leads": rows}


@app.get("/api/contacts/{contact_id}/generate-recommendations")
"@

if ($src -match "/api/hot-leads") {
  Write-Host "ALREADY PRESENT - no change made"
} elseif ($src -notmatch [regex]::Escape('@app.get("/api/contacts/{contact_id}/generate-recommendations")')) {
  Write-Host "ERROR - anchor not found"
} else {
  $src = $src -replace [regex]::Escape('@app.get("/api/contacts/{contact_id}/generate-recommendations")'), $func
  $enc = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($path, $src, $enc)
  Write-Host "DONE - hot-leads endpoint inserted"
}