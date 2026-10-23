"""Windows-only native regression: an isolated re-export must not approve changed history."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Requires Windows PowerPoint')
def test_isolated_native_retry_still_rejects_changed_history(tmp_path):
    shell = shutil.which('powershell.exe')
    if not shell:
        pytest.skip('Windows PowerShell unavailable')
    build = tmp_path / 'build'
    output = tmp_path / 'output'
    build.mkdir()
    output.mkdir()
    # Only synthetic text, with no user deck or desktop interaction.
    setup = tmp_path / 'setup.ps1'
    setup.write_text(r'''
param([string]$Root)
$ErrorActionPreference = 'Stop'
try { $app = New-Object -ComObject PowerPoint.Application } catch { exit 77 }
$deck = $null
try {
    $deck = $app.Presentations.Add(0)
    $deck.PageSetup.SlideWidth = 720
    $deck.PageSetup.SlideHeight = 405
    $slide = $deck.Slides.Add(1, 12)
    $shape = $slide.Shapes.AddTextbox(1, 30, 30, 600, 100)
    $shape.TextFrame.TextRange.Text = 'Original synthetic history'
    $slide.Export((Join-Path $Root 'base.png'), 'PNG', 1600, 900)
    $shape.TextFrame.TextRange.Text = 'Changed synthetic history'
    for ($i = 2; $i -le 5; $i++) { [void]$deck.Slides.Add($i, 12) }
    foreach ($s in $deck.Slides) {
        foreach ($n in $s.NotesPage.Shapes) {
            if ($n.HasTextFrame -eq -1) { $n.TextFrame.TextRange.Text = '' }
        }
    }
    $deck.SaveAs((Join-Path $Root 'output/report.pptx'), 24)
} finally {
    if ($null -ne $deck) { $deck.Close(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($deck) }
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
''', encoding='utf-8')
    created = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(setup), '-Root', str(tmp_path)], capture_output=True, timeout=60)
    if created.returncode == 77:
        pytest.skip('PowerPoint COM unavailable')
    assert created.returncode == 0, created.stderr.decode(errors='replace')
    composition = {'width': 1600, 'height': 900, 'base': [{'slide': 1, 'sha256': hashlib.sha256((tmp_path / 'base.png').read_bytes()).hexdigest()}]}
    (build / 'composition.json').write_text(json.dumps(composition), encoding='utf-8')
    job = {'run_dir': str(tmp_path), 'expected_base_slides': 1, 'expected_slide_count': 5, 'drop_blank_last': False, 'block_start': 1, 'block_end': 4, 'insert_after': 1, 'cover_replacements': []}
    job_path = build / 'job.json'
    job_path.write_text(json.dumps(job), encoding='utf-8')
    verifier = Path(__file__).parents[1] / 'src/invoice_print_layout/construction_report_native.ps1'
    checked = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(verifier), '-JobPath', str(job_path), '-Mode', 'verify'], capture_output=True, timeout=60)
    assert checked.returncode != 0
    receipt = json.loads((build / 'native-receipt.json').read_text(encoding='utf-8-sig'))
    assert receipt['status'] == 'failed'
    assert receipt['history_preserved'] is False
    assert receipt['isolated_exports'][0]['slide'] == 1
    assert receipt['isolated_exports'][0]['matches_base'] is False
    assert receipt['history'][0]['sha256_equal'] is False
    assert [p['slide'] for p in receipt['slides']] == [1, 2, 3, 4, 5]
    assert (build / 'sequential-slide-001.png').exists()
    protection = json.loads((build / 'window-protection-verify.json').read_text(encoding='utf-8-sig'))
    assert protection['preserved'] is True
    assert protection['quit_called'] is False
