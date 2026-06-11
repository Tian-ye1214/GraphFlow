# build-pipeline.ps1 —— 用 gf 从零搭一条 输入→LLM→自动处理→输出 流水线并运行
# 用法（模型配置需已存在，gf model add 先建）:
#   .\build-pipeline.ps1 -Seed C:\data\种子.jsonl -Model 通义
#   .\build-pipeline.ps1 -Seed .\种子.jsonl -Model 通义 -Server http://127.0.0.1:8000 -User alice -Wf 我的流水线
param(
    [Parameter(Mandatory)] [string]$Seed,        # jsonl/csv/xlsx，需含 q 列
    [Parameter(Mandatory)] [string]$Model,       # 已存在的模型配置名或 ID
    [string]$Server = "http://127.0.0.1:8000",
    [string]$User = "alice",
    [string]$Wf = "示例流水线"
)
$ErrorActionPreference = "Stop"
# 脚本位于 <仓库>\.claude\skills\gf-cli\scripts\，backend 在 <仓库>\backend
Push-Location (Resolve-Path "$PSScriptRoot\..\..\..\..\backend")

function gf {
    uv run gf @args
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "gf $($args -join ' ') 失败（退出码 $LASTEXITCODE）" }
}

$SeedPath = Resolve-Path $Seed
gf login $User --server $Server
gf data up "$SeedPath"
$ds = [IO.Path]::GetFileNameWithoutExtension($SeedPath)

gf wf add $Wf
gf use $Wf
gf node add input
gf node set input_1 dataset=$ds
gf node add llm
gf node set llm_synth_1 model=$Model "prompt=回答:{{q}}" out=answer conc=4
gf node add auto
gf op add auto_process_1 dedup answer
gf node add output
gf link input_1 llm_synth_1
gf link llm_synth_1 auto_process_1
gf link auto_process_1 output_1

gf show
gf run -f
Pop-Location
