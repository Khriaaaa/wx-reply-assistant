#!/usr/bin/env python3
"""国内主流大模型厂商的 OpenAI 兼容接口预置表

面板首次打开时用这份表把「接口地址」替用户填好 —— 用户只需要粘一个 API Key 进来。
每条都带 apply（去哪申请 Key）和 src（核对来源），端点用探针实测过：
无 Key 请求应回 401/403（说明域名与路径存在），回 000/404/410 的都不收。

维护约定：
- base_url 只留「填进 OpenAI SDK 就能用」的那一段，末尾不带 /
- models 只写能在官方文档里核到的模型名（示例用）；核不到的留空，
  让用户点面板上的「拉取模型列表」从自己账号里取 —— 别凭记忆编
- VERIFIED_AT 是这轮核对的日期，改了哪条就把日期一起更新
"""

VERIFIED_AT = "2026-10-06"

# id 一旦发布别再改：面板保存时会把 id 写进配置注释里
PRESETS = [
    {
        "id": "deepseek",
        "name": "DeepSeek 深度求索",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "apply": "https://platform.deepseek.com/api_keys",
        "src": "https://api-docs.deepseek.com/",
        "note": "文档里的 base_url 是 https://api.deepseek.com，带不带 /v1 都能通",
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.7-max", "qwen3.7-plus", "qwen3.6-flash"],
        "apply": "https://bailian.console.aliyun.com/",
        "src": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        "note": "百炼在推专属域名 https://{业务空间ID}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1，"
                "性能更稳，老域名仍可用",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.3", "glm-5.3-flash", "glm-5.2"],
        "apply": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        "src": "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
        "note": "GLM-5.3 是纯思考模型，出字比 flash 慢一截",
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.6"],
        "apply": "https://platform.kimi.com/console/api-keys",
        "src": "https://platform.moonshot.cn/docs/api/chat",
        "note": "K3 默认就带思考（reasoning_effort 默认 max）",
    },
    {
        "id": "ark",
        "name": "火山方舟（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-2-1-pro-260628", "doubao-seed-2-0-lite-260428"],
        "apply": "https://console.volcengine.com/ark",
        "src": "https://www.volcengine.com/docs/82379/1554521",
        "note": "模型 ID 带版本日期，且必须先在该模型卡片上点「开通」；也可以填自定义推理接入点 ep-xxxxxx",
    },
    {
        "id": "qianfan",
        "name": "百度千帆（文心）",
        "base_url": "https://qianfan.baidubce.com/v2",
        "models": ["ernie-5.1", "ernie-5.1-preview"],
        "apply": "https://console.bce.baidu.com/qianfan",
        "src": "https://cloud.baidu.com/doc/qianfan/s/Hmh4suq26",
        "note": "走 v2 新接口，Key 形如 bce-v3/ALTAK-xxxx/xxxx（不是老版 OAuth 的 access_token）",
    },
    {
        "id": "spark",
        "name": "讯飞星火",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "models": ["spark-x", "4.0Ultra", "generalv3.5"],
        "apply": "https://console.xfyun.cn/",
        "src": "https://www.xfyun.cn/doc/spark/X1http.html",
        "note": "Key 填控制台里的 APIPassword（不是 AppID/APIKey）；spark-x 现在指向 X2",
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "base_url": "https://api.minimaxi.com/v1",
        "models": ["MiniMax-M3", "MiniMax-M2.7"],
        "apply": "https://platform.minimaxi.com/",
        "src": "https://platform.minimaxi.com/document",
        "note": "国内站是 api.minimaxi.com，海外站 api.minimax.io；模型名请以拉取结果为准",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [],
        "apply": "https://cloud.siliconflow.cn/account/ak",
        "src": "https://docs.siliconflow.cn/",
        "note": "一家聚合站，模型名形如 deepseek-ai/DeepSeek-V3 —— 点「拉取模型列表」看你能用的那批",
    },
    {
        "id": "stepfun",
        "name": "阶跃星辰 StepFun",
        "base_url": "https://api.stepfun.com/v1",
        "models": ["step-5-preview", "step-3.7-flash", "step-3.5-flash"],
        "apply": "https://platform.stepfun.com/",
        "src": "https://platform.stepfun.com/docs/zh/guides/developer/openai",
        "note": "国际版是 api.stepfun.ai，别混用",
    },
    {
        "id": "hunyuan",
        "name": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "models": ["hunyuan-2.0-thinking-20251109", "hunyuan-2.0-instruct-20251111", "hunyuan-turbos"],
        "apply": "https://console.cloud.tencent.com/hunyuan/api-key",
        "src": "https://cloud.tencent.com/document/product/1729",
        "note": "腾讯云要开通混元服务并创建 API Key，Key 是 sk- 开头",
    },
    {
        "id": "sensenova",
        "name": "商汤日日新 SenseNova",
        "base_url": "https://api.sensenova.cn/compatible-mode/v1",
        "models": [],
        "apply": "https://console.sensecore.cn/",
        "src": "https://platform.sensenova.cn/doc",
        "note": "模型名点「拉取模型列表」取",
    },
    {
        "id": "longcat",
        "name": "美团 LongCat",
        "base_url": "https://api.longcat.chat/openai/v1",
        "models": ["LongCat-Flash-Chat"],
        "apply": "https://longcat.chat/platform/",
        "src": "https://longcat.chat/platform/docs",
        "note": "路径里带 openai/，别漏",
    },
    {
        "id": "antling",
        "name": "蚂蚁百灵 Ling",
        "base_url": "https://api.ant-ling.com/v1",
        "models": [],
        "apply": "https://ling.tbox.cn/",
        "src": "https://alipaytbox.yuque.com/sxs0ba/ling/openai_compatible",
        "note": "模型名点「拉取模型列表」取",
    },
    {
        "id": "local",
        "name": "本地部署（Ollama / vLLM / LM Studio）",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": [],
        "apply": "",
        "src": "",
        "note": "Ollama 默认 11434 / vLLM 默认 8000；本地不校验 Key，随便填一个非空值即可",
    },
    {
        "id": "custom",
        "name": "自定义（任何 OpenAI 兼容接口）",
        "base_url": "",
        "models": [],
        "apply": "",
        "src": "",
        "note": "自己填地址，形如 https://your-host/v1 —— 只要兼容 /chat/completions",
    },
]


def by_id(pid):
    return next((p for p in PRESETS if p["id"] == pid), None)


def for_panel():
    """给面板的副本：去掉内部字段，补上核对日期"""
    out = []
    for p in PRESETS:
        q = dict(p)
        q["presets_verified_at"] = VERIFIED_AT
        out.append(q)
    return out
