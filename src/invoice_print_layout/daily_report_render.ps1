param([string]$InputDeck,[string]$EvidenceDir)
$ErrorActionPreference = 'Stop'
$app = $null
$deck = $null
try {
  $app = New-Object -ComObject PowerPoint.Application
  $deck = $app.Presentations.Open($InputDeck, -1, 0, 0)
  $items = @()
  for ($i=1; $i -le $deck.Slides.Count; $i++) {
    $slide = $deck.Slides.Item($i)
    $pictures = 0
    for ($j=1; $j -le $slide.Shapes.Count; $j++) {
      if ($slide.Shapes.Item($j).Type -eq 13) { $pictures++ }
    }
    $items += @{slide=$i;pictures=$pictures}
    $slide.Export((Join-Path $EvidenceDir ('slide-'+$i+'.png')), 'PNG', 1280, 720)
  }
  @{opened=$true;slides=$deck.Slides.Count;items=$items} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $EvidenceDir 'powerpoint.json') -Encoding utf8
} catch { exit 1 }
finally {
  if ($deck) { $deck.Close(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($deck) }
  if ($app) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) }
}
