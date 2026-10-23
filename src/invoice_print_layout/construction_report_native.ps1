param(
    [Parameter(Mandatory = $true)][string]$JobPath,
    [Parameter(Mandatory = $true)][ValidateSet('compose', 'verify')][string]$Mode
)
$ErrorActionPreference = 'Stop'
$job = Get-Content -LiteralPath $JobPath -Raw -Encoding UTF8 | ConvertFrom-Json
$buildDir = Join-Path $job.run_dir 'build'
$outputDir = Join-Path $job.run_dir 'output'
$basePages = Join-Path $buildDir 'base-pages'
$pagesDir = Join-Path $outputDir 'pages'
$candidatePath = Join-Path $buildDir 'candidate.pptx'
$finalPath = Join-Path $outputDir 'report.pptx'
$retainedCount = [int]$job.expected_base_slides - [int][bool]$job.drop_blank_last
$blockCount = [int]$job.block_end - [int]$job.block_start + 1
$app = $null
$deck = $null
$stage = $null
$openedBefore = @()

function Open-PrivatePresentation([string]$Path) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    if ($fullPath -in $openedBefore) { throw 'Refusing to operate a presentation that was already open before this run' }
    return $app.Presentations.Open($fullPath, -1, 0, 0)
}

function Save-Json($Value, [string]$Path) {
    $json = ConvertTo-Json -InputObject $Value -Depth 12
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}

function Get-Sha256([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::OpenRead($Path)
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '')
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}

function Get-ShapeCounts($Shapes) {
    $result = @{ text = 0; image = 0; table = 0; chart = 0; total = 0 }
    for ($index = 1; $index -le $Shapes.Count; $index++) {
        $shape = $Shapes.Item($index)
        $result.total++
        if ($shape.Type -eq 6) {
            $children = Get-ShapeCounts $shape.GroupItems
            foreach ($key in @('text', 'image', 'table', 'chart', 'total')) { $result[$key] += $children[$key] }
        } else {
            if ($shape.HasTextFrame -eq -1 -and $shape.TextFrame.HasText -eq -1) { $result.text++ }
            if ($shape.Type -eq 13 -or $shape.Type -eq 11) { $result.image++ }
            if ($shape.HasTable -eq -1) { $result.table++ }
            if ($shape.HasChart -eq -1) { $result.chart++ }
        }
    }
    return $result
}

function Replace-CoverText($Shapes, [string]$OldText, [string]$NewText) {
    $matches = 0
    for ($index = 1; $index -le $Shapes.Count; $index++) {
        $shape = $Shapes.Item($index)
        if ($shape.Type -eq 6) {
            $matches += Replace-CoverText $shape.GroupItems $OldText $NewText
        } elseif ($shape.HasTextFrame -eq -1 -and $shape.TextFrame.HasText -eq -1) {
            $range = $shape.TextFrame.TextRange
            $occurrences = ([regex]::Matches($range.Text, [regex]::Escape($OldText))).Count
            # Replace one exact native range at a time, retaining formatting.
            for ($matchIndex = 0; $matchIndex -lt $occurrences; $matchIndex++) {
                [void]$range.Replace($OldText, $NewText, 0, -1, 0)
            }
            $matches += $occurrences
        }
    }
    return $matches
}

function Assert-BlankSlide($Slide, [int]$Width, [int]$Height) {
    # Unknown decorative objects also block deletion; only empty invisible text placeholders qualify.
    for ($index = 1; $index -le $Slide.Shapes.Count; $index++) {
        $shape = $Slide.Shapes.Item($index)
        if ($shape.Visible -eq 0) { continue }
        if ($shape.Type -ne 14 -or $shape.HasTextFrame -ne -1 -or $shape.TextFrame.HasText -eq -1) {
            throw 'Configured final blank slide contains visible or uncertain content'
        }
        if ($shape.Fill.Visible -ne 0 -or $shape.Line.Visible -ne 0) {
            throw 'Configured final blank slide contains visible placeholder styling'
        }
    }
    # The approved blank slide can retain master decoration and a page number.
    # Keep its native render as evidence; local content above still blocks deletion.
    $blankPng = Join-Path $buildDir 'removed-blank-last.png'
    $Slide.Export($blankPng, 'PNG', $Width, $Height)
}

try {
    $app = New-Object -ComObject PowerPoint.Application
    for ($i = 1; $i -le $app.Presentations.Count; $i++) { $openedBefore += [string]$app.Presentations.Item($i).FullName }
    if ($Mode -eq 'compose') {
        if (Test-Path -LiteralPath $candidatePath) { throw 'Candidate exists; create a new attempt' }
        $deck = Open-PrivatePresentation $job.base_pptx
        if ($deck.Slides.Count -ne $job.expected_base_slides) { throw 'Base slide count changed' }
        $slideSize = '{0},{1}' -f [long][math]::Round($deck.PageSetup.SlideWidth * 12700), [long][math]::Round($deck.PageSetup.SlideHeight * 12700)
        $width = 1600
        $height = [int][math]::Round($width * $deck.PageSetup.SlideHeight / $deck.PageSetup.SlideWidth)
        [void][IO.Directory]::CreateDirectory($basePages)
        $base = @()
        for ($index = 1; $index -le $retainedCount; $index++) {
            $slide = $deck.Slides.Item($index)
            $png = Join-Path $basePages ('slide-{0:D3}.png' -f $index)
            $slide.Export($png, 'PNG', $width, $height)
            $base += @{ slide = $index; sha256 = (Get-Sha256 $png); editable = (Get-ShapeCounts $slide.Shapes) }
        }
        if ($job.drop_blank_last) {
            Assert-BlankSlide $deck.Slides.Item($deck.Slides.Count) $width $height
            $deck.Slides.Item($deck.Slides.Count).Delete()
        }
        # Keep native before-images of the chosen template block for visual comparison.
        $referencePages = Join-Path $buildDir 'reference-pages'
        [void][IO.Directory]::CreateDirectory($referencePages)
        $stage = Open-PrivatePresentation $job.stage_template
        if ($stage.PageSetup.SlideWidth -ne $deck.PageSetup.SlideWidth -or $stage.PageSetup.SlideHeight -ne $deck.PageSetup.SlideHeight) { throw 'Stage and base slide sizes differ' }
        for ($index = [int]$job.block_start; $index -le [int]$job.block_end; $index++) {
            $stage.Slides.Item($index).Export((Join-Path $referencePages ('slide-{0:D3}.png' -f $index)), 'PNG', $width, $height)
        }
        $stage.Close()
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($stage)
        $stage = $null
        $inserted = $deck.Slides.InsertFromFile((Join-Path $buildDir 'stage-edited.pptx'), [int]$job.insert_after, [int]$job.block_start, [int]$job.block_end)
        if ($inserted -ne $blockCount -or $deck.Slides.Count -ne $job.expected_slide_count) { throw 'Unexpected composed slide count' }
        foreach ($replacement in $job.cover_replacements) {
            if ([string]::IsNullOrEmpty($replacement.old) -or $null -eq $replacement.text) { throw 'Invalid cover replacement' }
            $matches = Replace-CoverText $deck.Slides.Item(1).Shapes $replacement.old $replacement.text
            if ($matches -ne 1) { throw 'Cover replacement did not have exactly one match on slide 1' }
        }
        for ($index = 1; $index -le $deck.Slides.Count; $index++) {
            $notes = $deck.Slides.Item($index).NotesPage.Shapes
            for ($shapeIndex = 1; $shapeIndex -le $notes.Count; $shapeIndex++) {
                $shape = $notes.Item($shapeIndex)
                if ($shape.HasTextFrame -eq -1) { $shape.TextFrame.TextRange.Text = '' }
            }
        }
        $deck.SaveAs($candidatePath, 24)
        Save-Json @{ slide_size_emu = $slideSize; width = $width; height = $height; base = $base; inserted = $inserted; blank_last_removed = [bool]$job.drop_blank_last } (Join-Path $buildDir 'composition.json')
    } else {
        $composition = Get-Content -LiteralPath (Join-Path $buildDir 'composition.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $deck = Open-PrivatePresentation $finalPath
        if ($deck.Slides.Count -ne $job.expected_slide_count) { throw 'Final native slide count differs' }
        [void][IO.Directory]::CreateDirectory($pagesDir)
        $pages = @()
        $history = @()
        $historyPassed = $true
        for ($index = 1; $index -le $deck.Slides.Count; $index++) {
            $slide = $deck.Slides.Item($index)
            $png = Join-Path $pagesDir ('slide-{0:D3}.png' -f $index)
            $slide.Export($png, 'PNG', [int]$composition.width, [int]$composition.height)
            $counts = Get-ShapeCounts $slide.Shapes
            $hash = Get-Sha256 $png
            $notes = $slide.NotesPage.Shapes
            for ($shapeIndex = 1; $shapeIndex -le $notes.Count; $shapeIndex++) {
                $shape = $notes.Item($shapeIndex)
                if ($shape.HasTextFrame -eq -1 -and $shape.TextFrame.HasText -eq -1 -and -not [string]::IsNullOrWhiteSpace($shape.TextFrame.TextRange.Text)) { throw 'Speaker notes were not cleared' }
            }
            $pages += @{ slide = $index; sha256 = $hash; editable = $counts }
        }
        $slideCount = $deck.Slides.Count
        $isolatedExports = @()
        foreach ($original in $composition.base) {
            $originalIndex = [int]$original.slide
            $finalIndex = $originalIndex
            if ($originalIndex -gt $job.insert_after) { $finalIndex += $blockCount }
            $final = $pages[$finalIndex - 1]
            $coverException = $originalIndex -eq 1 -and @($job.cover_replacements).Count -gt 0
            $same = $original.sha256 -eq $final.sha256
            if (-not $same -and -not $coverException) {
                # Native text rendering can depend on previously accessed slides.
                # Retry this page first in a fresh read-only presentation; never
                # relax the exact hash requirement or change the PPTX itself.
                $png = Join-Path $pagesDir ('slide-{0:D3}.png' -f $finalIndex)
                Copy-Item -LiteralPath $png -Destination (Join-Path $buildDir ('sequential-slide-{0:D3}.png' -f $finalIndex))
                $deck.Close()
                [void][Runtime.InteropServices.Marshal]::ReleaseComObject($deck)
                $deck = $null
                $deck = Open-PrivatePresentation $finalPath
                $deck.Slides.Item($finalIndex).Export($png, 'PNG', [int]$composition.width, [int]$composition.height)
                $previousHash = $final.sha256
                $final.sha256 = Get-Sha256 $png
                $same = $original.sha256 -eq $final.sha256
                $isolatedExports += @{ slide = $finalIndex; sequential_sha256 = $previousHash; isolated_sha256 = $final.sha256; matches_base = $same }
            }
            if (-not $same -and -not $coverException) { $historyPassed = $false }
            $history += @{ base_slide = $originalIndex; final_slide = $finalIndex; sha256_equal = $same; cover_replacement_exception = $coverException }
        }
        $receipt = @{
            status = $(if ($historyPassed) { 'passed' } else { 'failed' }); native_application = 'Microsoft PowerPoint'
            native_version = $app.Version; reopened_final = $true; slide_count = $slideCount
            isolated_exports = $isolatedExports
            notes_cleared = $true; history_preserved = $historyPassed; history = $history; slides = $pages
            report_sha256 = (Get-Sha256 $finalPath)
        }
        Save-Json $receipt (Join-Path $buildDir 'native-receipt.json')
        if (-not $historyPassed) { throw 'Preserved-page native PNG comparison failed; review private native-receipt.json' }
    }
} catch {
    Save-Json @{ status = 'failed'; mode = $Mode; error = $_.Exception.Message } (Join-Path $buildDir ('native-{0}-failure.json' -f $Mode))
    throw
} finally {
    if ($null -ne $stage) { $stage.Close(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($stage) }
    if ($null -ne $deck) { $deck.Close(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($deck) }
    # Do not Quit(): the COM application may be shared with the user's open decks.
    if ($null -ne $app) {
        $openedAfter = @()
        for ($i = 1; $i -le $app.Presentations.Count; $i++) { $openedAfter += [string]$app.Presentations.Item($i).FullName }
        $missing = @($openedBefore | Where-Object { $_ -notin $openedAfter })
        Save-Json @{ preserved = ($missing.Count -eq 0); opened_before = $openedBefore; missing_after = $missing; quit_called = $false } (Join-Path $buildDir ('window-protection-{0}.json' -f $Mode))
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
        if ($missing.Count -gt 0) { throw 'A previously open presentation is missing after this run' }
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
