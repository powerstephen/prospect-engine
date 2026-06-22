$path = "C:\Users\steve\prospect-engine\ui\roast_report.working.html"
$enc  = New-Object System.Text.UTF8Encoding $false
$html = [System.IO.File]::ReadAllText($path, $enc)
$n = 0

# 1. Remove the early overall-donut draw (it uses ws=79, drawn before ms/ds exist)
$o1 = "  setArc('r-donut-arc','r-score','r-grade',ws,66);" + "`r`n"
if($html.Contains($o1)){$html=$html.Replace($o1,'');$n++}

# 2. After the desktop donut draw, add the blended overall draw (avg of ms+ds)
$o2 = "  setArc('r-desktop-arc','r-desktop-num','r-desktop-grade',ds,46);"
$x2 = "  setArc('r-desktop-arc','r-desktop-num','r-desktop-grade',ds,46);" + "`r`n" + "  const wsBlend=Math.round((ms+ds)/2);" + "`r`n" + "  setArc('r-donut-arc','r-score','r-grade',wsBlend,66);"
if($html.Contains($o2)){$html=$html.Replace($o2,$x2);$n++}

[System.IO.File]::WriteAllText($path, $html, $enc)
Write-Host "changes: $n of 2"