param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskName = 'InvoiceAssistantBackground'
$executable = Join-Path $env:SystemRoot 'System32\wscript.exe'
$arguments = '//B //Nologo "' + (Join-Path $PSScriptRoot 'assistant-startup.vbs') + '"'
$description = 'Invoice Print Layout background launcher: ' + (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing -and ($existing.Description -ne $description -or @($existing.Actions).Count -ne 1 -or $existing.Actions[0].Execute -ne $executable -or $existing.Actions[0].Arguments -ne $arguments)) {
    throw 'The existing InvoiceAssistantBackground task belongs to another program or checkout; it was not changed.'
}
if ($Remove) {
    if ($existing) { Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false }
    return
}
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$([Security.SecurityElement]::Escape($description))</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Repetition><Interval>PT10M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><Enabled>true</Enabled><UserId>$user</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="CurrentUser"><UserId>$user</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT5M</ExecutionTimeLimit></Settings>
  <Actions Context="CurrentUser"><Exec><Command>$([Security.SecurityElement]::Escape($executable))</Command><Arguments>$([Security.SecurityElement]::Escape($arguments))</Arguments><WorkingDirectory>$([Security.SecurityElement]::Escape((Resolve-Path (Join-Path $PSScriptRoot '..')).Path))</WorkingDirectory></Exec></Actions>
</Task>
"@
Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Xml $xml -Force | Out-Null
