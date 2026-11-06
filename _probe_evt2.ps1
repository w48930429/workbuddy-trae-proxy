$s = Get-Date -Date "2026-10-30 06:30:00"
$e = Get-Date -Date "2026-10-30 07:20:00"
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$s; EndTime=$e} -ErrorAction SilentlyContinue |
  Sort-Object TimeCreated |
  ForEach-Object { "{0:HH:mm:ss} {1,-6} {2,-32} {3}" -f $_.TimeCreated, $_.Id, $_.ProviderName, (($_.Message -replace '\s+',' ')) }
"--- powercfg lastwake ---"
powercfg /lastwake
"--- boot ---"
(Get-CimInstance Win32_OperatingSystem).LastBootUpTime
"--- uptime/sleep events 24h ---"
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; StartTime=(Get-Date).AddHours(-24)} -ErrorAction SilentlyContinue |
  ForEach-Object { "{0:MM-dd HH:mm:ss} {1} {2}" -f $_.TimeCreated, $_.Id, (($_.Message -replace '\s+',' ')) }
