param([Parameter(Mandatory=$true)][string]$PayloadPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$data = Get-Content -LiteralPath $PayloadPath -Raw -Encoding UTF8 | ConvertFrom-Json
$workbookPath = [IO.Path]::GetFullPath($data.output)
$pdfPath = [IO.Path]::GetFullPath($data.summary_pdf)
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class InvoicePdfExcelOwner {
  [DllImport("user32.dll")]
  public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
}
'@
$priorExcelIds = @(Get-Process EXCEL -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$excel = $null; $book = $null; $main = $null; $detail = $null; $owned = $false
try {
    $excel = New-Object -ComObject Excel.Application
    [uint32]$excelPid = 0
    [InvoicePdfExcelOwner]::GetWindowThreadProcessId([IntPtr]$excel.Hwnd, [ref]$excelPid) | Out-Null
    $owned = $excelPid -notin $priorExcelIds
    if (-not $owned) { throw 'Excel PDF instance is not isolated.' }
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    $excel.AutomationSecurity = 3
    $excel.AskToUpdateLinks = $false
    $book = $excel.Workbooks.Open($workbookPath, 0, $true)
    if (-not $book.ReadOnly) { throw 'Expected a read-only workbook.' }
    $main = $book.Worksheets.Item(1)
    $detail = $book.Worksheets.Item(2)
    $excel.CalculateFull()
    $detailRow = $data.items.Count + 2
    if ([math]::Round([double]$main.Range('H35').Value2 * 100) -ne $data.total_cents -or
        [math]::Round([double]$detail.Range("H$detailRow").Value2 * 100) -ne $data.total_cents) {
        throw 'Native Excel totals differ from the shared report snapshot.'
    }
    foreach ($row in $data.snapshot.rows) {
        if ([math]::Round([double]$main.Range("H$($row.row)").Value2 * 100) -ne $row.amount_cents) {
            throw 'Native Excel category total differs from the shared report snapshot.'
        }
    }
    $setup = $main.PageSetup
    $setup.PrintArea = '$A$1:$M$36'
    $setup.PaperSize = 9
    $setup.Orientation = 1
    $setup.Zoom = $false
    $setup.FitToPagesWide = 1
    $setup.FitToPagesTall = 1
    $setup.LeftHeader = ''; $setup.CenterHeader = ''; $setup.RightHeader = ''
    $setup.LeftFooter = ''; $setup.CenterFooter = ''; $setup.RightFooter = ''
    $main.ExportAsFixedFormat(0, $pdfPath, 0, $true, $false)
} finally {
    if ($null -ne $book) { $book.Close($false) }
    if ($owned -and $null -ne $excel) { $excel.Quit() }
    foreach ($comObject in @($detail, $main, $book, $excel)) {
        if ($null -ne $comObject) { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($comObject) | Out-Null }
    }
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}
