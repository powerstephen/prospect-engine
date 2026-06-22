$path = "C:\Users\steve\prospect-engine\ui\roast_report.html"
$html = [System.IO.File]::ReadAllText($path)

$beacon = @"
<script>
(function () {
  try {
    var d = window.REPORT_DATA || {};
    var cid = d.id || null;
    var slug = (location.pathname.split("/report/")[1] || "").split("?")[0];
    function track(event) {
      try {
        fetch("/api/track/view", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ contact_id: cid, slug: slug, event: event }),
          keepalive: true
        });
      } catch (e) {}
    }
    track("loaded");
    setTimeout(function () {
      if (!document.hidden) track("engaged");
    }, 10000);
  } catch (e) {}
})();
</script>
</body>
"@

if ($html -match "track/view") {
  Write-Host "ALREADY PRESENT - no change made"
} elseif ($html -notmatch "</body>") {
  Write-Host "ERROR - no </body> tag found"
} else {
  $html = $html -replace "</body>", $beacon
  $enc = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($path, $html, $enc)
  Write-Host "DONE - beacon inserted"
}