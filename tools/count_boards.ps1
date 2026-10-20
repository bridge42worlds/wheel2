# Сколько плат ESP32-S3 (USB VID 303A) видит Windows, их MAC и COM-порты.
#   powershell -NoProfile -File tools/count_boards.ps1
# Вывод одной строкой: "<число плат> <COM-порты через пробел> | <MAC через пробел>".
# MAC берётся из имени USB-устройства: у встроенного USB-JTAG оно равно MAC платы.
$dev = Get-PnpDevice -PresentOnly | Where-Object {
  $_.InstanceId -like 'USB\VID_303A&PID_*' -and $_.InstanceId -notlike '*&MI_*'
}
$macs = $dev | ForEach-Object { ($_.InstanceId -split '\\')[-1].ToLower() }
$ports = Get-PnpDevice -PresentOnly | Where-Object {
  $_.InstanceId -like 'USB\VID_303A&PID_*&MI_00*' -and $_.FriendlyName -match 'COM\d+'
} | ForEach-Object { if ($_.FriendlyName -match '(COM\d+)') { $Matches[1] } }
"$(@($dev).Count) $($ports -join ' ') | $($macs -join ' ')"
