---
name: gf-model
description: Use when 用 gf 命令管理 GraphFlow 的模型配置——列出/新建/修改/删除模型，或真实发一条请求测试模型连通性；涉及 base_url / model_name / api_key / provider(openai|azure) / 采样默认参数 时
---

# gf-model —— 模型配置

前置：先 `gf login`。模型是用户级资源，与当前工作流无关。

| 命令 | 说明 |
|---|---|
| `gf model ls` | ID、名、模型ID、base_url、provider、api_version、`key:已配置/未配置`（**永不显示明文 key**） |
| `gf model add <名> --url <base_url> --model <模型ID> [--key <api_key>] [--provider openai\|azure] [--api-version <版本>]` | 新建。`--url`/`--model` 必填；`--key` 可省（无鉴权网关）；`--provider` 默认 openai |
| `gf model set <名\|ID> key=value …` | 改字段。键：`name=` `model=`(model_name) `url=`(base_url) `key=`(api_key) `provider=` `api_version=`(别名 `version=`) + 采样 `temp=` `top_p=` `max_tokens=`（进 default_params）。**未给的字段保留原值；`key=` 不给则不改密钥** |
| `gf model rm <名\|ID>` | 删除 |
| `gf model test <名\|ID>` | 真实发一条测试请求；连通打印「连通正常」，失败打印错误并退出码 1 |

示例：

```powershell
gf model add 通义 --url https://dashscope.aliyuncs.com/compatible-mode/v1 --model qwen-plus --key sk-xxx
gf model set 通义 temp=0 max_tokens=2048
gf model test 通义
```

⚠️ 资源指代：`<名|ID>` 纯数字按 ID，否则按名精确匹配，重名报错列候选 ID（见 gf-cli 跨域坑）。
