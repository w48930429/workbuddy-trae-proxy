Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-14)} -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'python' } |
  Select-Object -First 20 |
  ForEach-Object { "{0} {1} {2} :: {3}" -f $_.TimeCreated, $_.Id, $_.ProviderName, ($_.Message -replace '\s+', ' ').Substring(0, [Math]::Min(240, $_.Message.Length)) }
"--- done ---"
