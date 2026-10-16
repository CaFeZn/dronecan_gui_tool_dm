<#
.SYNOPSIS
    把当前 HEAD 的源码同步进 WSL 的 Linux 文件系统，可重复执行。

.DESCRIPTION
    直接在 `/mnt/d` 上跑也能起来，但 9p 文件系统很慢（import 大量 .py 时尤其明显），
    而且**部分环境里 /mnt 下的源码读出来不是原文件内容**（安全软件、文件过滤驱动等）。

    所以这里不"复制文件"，而是 `git archive` 打成一个 .tar，再让 **WSL 里的 tar 自己
    解包**：内容以二进制原样穿过管道/文件，最终由 Linux 侧落盘，绕开这些差异。

.PARAMETER Distro
    WSL 发行版名，默认 Ubuntu。

.PARAMETER Target
    WSL 里的目标目录，默认 /home/dm/dgt。
#>
param(
    [string]$Distro = 'Ubuntu',
    [string]$Target = '/home/dm/dgt'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$tar = Join-Path $repo 'artifacts\src.tar'
$sdkSo = '/mnt/d/Codes/mess/dm-device-sdk/C&C++/lib/v2.1.0/linux/x86_64/libdmcan.so'

Write-Host "[sync] 导出 $tar …"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tar) | Out-Null
git -C $repo archive --format=tar -o $tar HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive 失败（$LASTEXITCODE）" }

Write-Host "[sync] 在 WSL 里解包到 $Target …"
wsl -d $Distro -u root -e bash -c "rm -rf $Target && mkdir -p $Target/dlls && tar -xf /mnt/d/Codes/mess/dronecan-gui-tool-dm/artifacts/src.tar -C $Target && cp '$sdkSo' $Target/dlls/ && echo '[sync] 解包完成'"
if ($LASTEXITCODE -ne 0) { throw "WSL 解包失败（$LASTEXITCODE）" }

wsl -d $Distro -u root -e bash -c "head -1 $Target/setup.py; ls $Target/dlls/"
Write-Host "[sync] 完成（源码为明文、libdmcan.so 就位）"
