param(
    [Parameter(Mandatory=$true)][string]$JobPath,
    [Parameter(Mandatory=$true)][ValidateSet('compose','verify')][string]$Mode
)
$ErrorActionPreference = 'Stop'
$job = Get-Content -LiteralPath $JobPath -Raw -Encoding UTF8 | ConvertFrom-Json
$build = Join-Path $job.run_dir 'build'
$output = Join-Path $job.run_dir 'output'
$candidate = Join-Path $build 'candidate.pptx'
$final = Join-Path $output 'report.pptx'
$app = $null
$deck = $null
$openedBefore = @()

function Save-Json($Value, [string]$Path) {
    [IO.File]::WriteAllText($Path,(ConvertTo-Json -InputObject $Value -Depth 16),(New-Object Text.UTF8Encoding($false)))
}
function Get-Sha([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Open-Private([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -in $openedBefore) { throw 'Refusing to operate a presentation already open before this run' }
    return $app.Presentations.Open($full,-1,0,0)
}
function Assert-Size($Presentation) {
    $actual = @([long][Math]::Round($Presentation.PageSetup.SlideWidth*12700),[long][Math]::Round($Presentation.PageSetup.SlideHeight*12700))
    if ($actual[0] -ne $job.slide_size_emu[0] -or $actual[1] -ne $job.slide_size_emu[1]) { throw 'Presentation size changed' }
}
function Assert-Tables($Slide) {
    $tables = 0
    foreach ($shape in $Slide.Shapes) {
        if ($shape.HasTable -eq -1) {
            $tables++
            for ($r=2;$r -le $shape.Table.Rows.Count;$r++) {
                $cell = $shape.Table.Cell($r,([int]$job.settings.actual_acceptance_column+1))
                if (-not [string]::IsNullOrWhiteSpace([string]$cell.Shape.TextFrame.TextRange.Text)) { throw 'Actual acceptance cell is not empty' }
            }
        }
    }
    return $tables
}
try {
    $app = New-Object -ComObject PowerPoint.Application
    for ($i=1;$i -le $app.Presentations.Count;$i++) { $openedBefore += [string]$app.Presentations.Item($i).FullName }
    if ($Mode -eq 'compose') {
        if (Test-Path -LiteralPath $candidate) { throw 'Candidate exists; create a fresh attempt' }
        $deck = Open-Private (Join-Path $build 'authored-verified.pptx')
        if ($deck.Slides.Count -lt $job.expected_slide_count) { throw 'Authored presentation is incomplete' }
        # Author places selected originals/duplicates first. Delete only its unused imported tail.
        while ($deck.Slides.Count -gt $job.expected_slide_count) { $deck.Slides.Item($deck.Slides.Count).Delete() }
        Assert-Size $deck
        for ($i=1;$i -le $deck.Slides.Count;$i++) {
            $slide = $deck.Slides.Item($i)
            $spec = $job.slides[$i-1]
            # Python verified ordered original bytes and removed template picture
            # nodes before opening this private package. Never trust exporter alt.
            for ($s=1;$s -le $slide.NotesPage.Shapes.Count;$s++) {
                $shape=$slide.NotesPage.Shapes.Item($s)
                if ($shape.HasTextFrame -eq -1) { $shape.TextFrame.TextRange.Text='' }
            }
            $count = Assert-Tables $slide
            if ($count -ne @($spec.expected_tables).Count) { throw 'Native table count differs from source' }
        }
        $deck.SaveAs($candidate,24)
        Save-Json @{status='passed';slide_count=$deck.Slides.Count;notes_cleared=$true;slide_size_emu=$job.slide_size_emu} (Join-Path $build 'composition.json')
        $deck.Close()
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($deck)
        $deck=$null
    } else {
        $pages=Join-Path $output 'pages'
        [void][IO.Directory]::CreateDirectory($pages)
        $finalSha=Get-Sha $final
        $checks=@()
        $width=1920
        $height=[int][Math]::Round($width*$job.slide_size_emu[1]/$job.slide_size_emu[0])
        for ($i=1;$i -le $job.expected_slide_count;$i++) {
            $deck=Open-Private $final
            Assert-Size $deck
            if ($deck.Slides.Count -ne $job.expected_slide_count) { throw 'Final presentation page count differs' }
            $slide=$deck.Slides.Item($i)
            $tables=Assert-Tables $slide
            if ($tables -ne @($job.slides[$i-1].expected_tables).Count) { throw 'Final native tables missing' }
            foreach ($shape in $slide.NotesPage.Shapes) {
                if ($shape.HasTextFrame -eq -1 -and -not [string]::IsNullOrWhiteSpace([string]$shape.TextFrame.TextRange.Text)) { throw 'Final notes are not empty' }
            }
            $png=Join-Path $pages ('slide-{0:D3}.png' -f $i)
            $slide.Export($png,'PNG',$width,$height)
            $checks+=@{slide=$i;sha256=(Get-Sha $png);native_tables=$tables;actual_acceptance_empty=$true;isolated_render=$true}
            $deck.Close()
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($deck)
            $deck=$null
        }
        if ((Get-Sha $final) -ne $finalSha) { throw 'Final presentation changed during native read-only export' }
        Save-Json @{status='passed';reopened_final=$true;notes_cleared=$true;slide_count=$job.expected_slide_count;final_sha256=$finalSha;slides=$checks;powerpoint_version=$app.Version;already_open_presentations=$openedBefore} (Join-Path $build 'native-receipt.json')
    }
} finally {
    # Close only the private presentation opened by this invocation. Never Quit or kill PowerPoint.
    if ($null -ne $deck) { $deck.Close();[void][Runtime.InteropServices.Marshal]::ReleaseComObject($deck) }
    if ($null -ne $app) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) }
}
