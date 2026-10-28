param([Parameter(Mandatory=$true)][string]$Payload,
      [Parameter(Mandatory=$true)][string]$OutputFile)
$ErrorActionPreference = 'Stop'
$rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider(3072)
$rsa.PersistKeyInCsp = $false
try {
    $data = [IO.File]::ReadAllBytes($Payload)
    $signature = $rsa.SignData($data, 'SHA256')
    $fixture = @{
        envelope = @{
            payload = [Convert]::ToBase64String($data)
            signature = [Convert]::ToBase64String($signature)
        }
        modulus = [Convert]::ToBase64String($rsa.ExportParameters($false).Modulus)
    }
    [IO.File]::WriteAllText($OutputFile, ($fixture | ConvertTo-Json -Depth 4 -Compress))
} finally { $rsa.Dispose() }
