$rows = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|cmd|conhost|WindowsTerminal' }
foreach ($r in $rows) {
  $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $r.ParentProcessId) -ErrorAction SilentlyContinue
  $pname = if ($p) { $p.Name + '#' + $p.ProcessId } else { 'none' }
  "{0} pid={1} ppid={2}({3}) started={4:HH:mm:ss} cmd={5}" -f $r.Name, $r.ProcessId, $r.ParentProcessId, $pname, $r.CreationDate, ($r.CommandLine -replace '\s+',' ')
}
"--- listeners ---"
Get-NetTCPConnection -LocalPort 28087 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { "listen pid=" + $_.OwningProcess }
"--- files ---"
Get-ChildItem E:\Project_AI\ai_chajian\workbuddy-trae-proxy\wt-proxy-28087.* -ErrorAction SilentlyContinue | ForEach-Object { $_.Name + ' ' + $_.Length + ' ' + $_.LastWriteTime }
