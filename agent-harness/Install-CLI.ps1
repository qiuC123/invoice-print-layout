param([string]$Python='python',[string]$Url='http://127.0.0.1:8765',[string]$WorkspaceId='',[switch]$SkipInstall,[switch]$SkipAgentSkills)
$ErrorActionPreference='Stop'
$venv=Join-Path $PSScriptRoot '.venv'
$cliPython=Join-Path $venv 'Scripts\python.exe'
if(-not(Test-Path -LiteralPath $cliPython)){& $Python -m venv --system-site-packages $venv;if($LASTEXITCODE){throw 'CLI venv creation failed.'}}
if(-not $SkipInstall){& $cliPython -m pip install -e $PSScriptRoot;if($LASTEXITCODE){throw 'CLI installation failed.'}}
$configure=@'
import json,os,sys,uuid
from pathlib import Path
from cli_anything.invoice_assistant.utils.client import Client,local_url
url=local_url(sys.argv[1]); expected=sys.argv[2]
client=Client({'url':url,'workspace_id':expected})
runtime=client.request('/api/runtime')
if runtime.get('app_id') not in {'invoice-print-layout.invoice-assistant','invoice-print-layout.workbench'} or runtime.get('ready') is not True:raise SystemExit('Not a ready native invoice service.')
actual=runtime.get('workspace_id')
if not actual or (expected and expected!=actual):raise SystemExit('Native workspace identity mismatch.')
folder=Path(os.environ['LOCALAPPDATA'])/'InvoiceAssistantCLI';folder.mkdir(parents=True,exist_ok=True)
file=folder/'config.json';config=json.loads(file.read_text(encoding='utf-8-sig')) if file.exists() else {}
config.update(url=url,workspace_id=actual)
temporary=folder/('.config-'+uuid.uuid4().hex+'.tmp');temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(file)
print('Native API identity pinned; no model settings or credentials stored.')
'@
$configure | & $cliPython -X utf8 -B - $Url $WorkspaceId
if($LASTEXITCODE){throw 'Native invoice service identity/configuration failed.'}
$binDir=Join-Path $env:USERPROFILE '.local\bin'
New-Item -ItemType Directory -Path $binDir -Force | Out-Null
foreach($name in @('invoice-assistant','cli-anything-invoice-assistant')){
 foreach($suffix in @('.exe','-script.py')){
  $source=Join-Path $venv ('Scripts\'+$name+$suffix)
  if(Test-Path -LiteralPath $source){Copy-Item -LiteralPath $source -Destination (Join-Path $binDir ($name+$suffix)) -Force}
 }
}
$userPath=[string][Environment]::GetEnvironmentVariable('Path','User')
if(@($userPath -split ';' | Where-Object {$_.TrimEnd('\') -ieq $binDir.TrimEnd('\')}).Count -eq 0){[Environment]::SetEnvironmentVariable('Path',($userPath.TrimEnd(';')+';'+$binDir),'User')}
if(-not $SkipAgentSkills){
 $skillSource=Join-Path (Split-Path $PSScriptRoot -Parent) 'skills\cli-anything-invoice-assistant\SKILL.md'
 foreach($skillDir in @((Join-Path $env:USERPROFILE '.agents\skills\cli-anything-invoice-assistant'),(Join-Path $env:LOCALAPPDATA 'hermes\skills\cli-anything-invoice-assistant'))){
  New-Item -ItemType Directory -Path $skillDir -Force | Out-Null
  Copy-Item -LiteralPath $skillSource -Destination (Join-Path $skillDir 'SKILL.md') -Force
 }
}
& (Join-Path $binDir 'invoice-assistant.exe') --json doctor
if($LASTEXITCODE){throw 'Installed CLI doctor failed.'}
Write-Output ('Installed: '+(Join-Path $binDir 'invoice-assistant.exe'))
