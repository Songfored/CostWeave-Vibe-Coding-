"""Built-in model facts used as the resettable v0.3 catalog baseline.

Prices and hard limits come from vendor documentation. Capability scores are
CostWeave routing estimates, not vendor benchmarks. No provider API is called.
"""

from __future__ import annotations

from .domain import WorkerProfile


SNAPSHOT_DATE = "2026-07-18"

SOURCES = {
    "openai": "https://developers.openai.com/api/docs/models",
    "anthropic": "https://platform.claude.com/docs/en/about-claude/models/overview",
    "google": "https://ai.google.dev/gemini-api/docs/pricing",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing",
    "xai": "https://docs.x.ai/developers/pricing",
    "mistral": "https://docs.mistral.ai/models/model-cards/mistral-small-4-0-26-03",
    "alibaba": "https://help.aliyun.com/en/model-studio/model-pricing",
}


def _model(
    model_id: str,
    name: str,
    provider: str,
    specialty: str,
    capabilities: dict[str, float],
    *,
    tier: str,
    reasoning: float,
    speed: float,
    reliability: float,
    context: int,
    max_output: int,
    input_price: float,
    output_price: float,
    cached_price: float | None = None,
    modalities: tuple[str, ...] = ("text",),
    tools: tuple[str, ...] = ("function_calling", "structured_output"),
    strengths: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    local: bool = False,
    currency: str = "USD",
    preview: bool = False,
    routable: bool = True,
) -> WorkerProfile:
    def items(value: tuple[str, ...] | str) -> list[str]:
        return [value] if isinstance(value, str) else list(value)

    return WorkerProfile(
        id=model_id,
        model_id=model_id,
        name=name,
        provider=provider,
        specialty=specialty,
        capabilities=capabilities,
        cost_per_task=0.0,
        latency_factor=max(0.35, 1.55 - speed),
        reliability=reliability,
        local=local,
        tier=tier,
        reasoning=reasoning,
        speed=speed,
        context_window=context,
        max_output_tokens=max_output,
        input_price_per_mtok=input_price,
        cached_input_price_per_mtok=cached_price,
        output_price_per_mtok=output_price,
        pricing_currency=currency,
        modalities=items(modalities),
        tools=items(tools),
        strengths=items(strengths),
        limitations=items(limitations),
        source_url=SOURCES.get(provider, ""),
        verified_at=SNAPSHOT_DATE,
        preview=preview,
        routable=routable,
    )


BASE = {
    "analysis": .72, "structure": .72, "planning": .70, "coding": .65,
    "testing": .63, "research": .65, "data": .65, "math": .64,
    "writing": .72, "translation": .72, "vision": .58, "risk": .68,
    "validation": .70, "synthesis": .72, "safety": .65, "creative": .70,
    "tool_use": .68, "long_context": .62,
}


def _caps(**overrides: float) -> dict[str, float]:
    return {**BASE, **overrides}


MODELS: tuple[WorkerProfile, ...] = (
    _model(
        "local-rule-core", "本地规则核心", "local", "确定性分类、Schema 与 DAG 检查",
        _caps(analysis=.58, structure=.99, planning=.52, coding=.28, research=.20,
              validation=.98, risk=.74, synthesis=.35, tool_use=.20),
        tier="deterministic", reasoning=.28, speed=.99, reliability=.99,
        context=64_000, max_output=8_000, input_price=0, output_price=0,
        tools=("structured_output",), strengths=("零 API 成本", "确定性校验", "本地运行"),
        limitations=("不能生成可靠领域结论", "不具备实时知识"), local=True,
    ),
    _model(
        "gpt-oss-20b-local", "GPT-OSS 20B（本地）", "openai", "低延迟本地推理与结构化任务",
        _caps(analysis=.76, structure=.85, planning=.72, coding=.76, testing=.72,
              math=.72, validation=.78, tool_use=.82, vision=.10),
        tier="local", reasoning=.72, speed=.82, reliability=.82,
        context=131_072, max_output=131_072, input_price=0, output_price=0,
        modalities=("text",), tools=("function_calling", "structured_output", "code_execution"),
        strengths=("Apache 2.0", "可配置推理强度", "适合本地与专用部署"),
        limitations=("API 价格为零不代表本地算力免费", "不支持视觉"), local=True,
    ),
    _model(
        "gpt-5.6-luna", "GPT-5.6 Luna", "openai", "低成本高吞吐通用任务",
        _caps(analysis=.88, planning=.84, coding=.86, testing=.84, research=.82,
              data=.84, math=.84, validation=.86, synthesis=.86, vision=.86, tool_use=.91),
        tier="economy", reasoning=.86, speed=.88, reliability=.91,
        context=1_050_000, max_output=128_000, input_price=1, output_price=6,
        modalities=("text", "image"), tools=("function_calling", "structured_output", "web_search", "file_search", "computer_use"),
        strengths=("高吞吐", "长上下文", "工具覆盖完整"), limitations=("复杂任务能力低于 Sol"),
    ),
    _model(
        "gpt-5.6-terra", "GPT-5.6 Terra", "openai", "能力与成本平衡的复杂工作",
        _caps(analysis=.94, planning=.93, coding=.94, testing=.92, research=.90,
              data=.91, math=.92, validation=.92, synthesis=.92, risk=.90, vision=.91, tool_use=.95),
        tier="balanced", reasoning=.94, speed=.72, reliability=.95,
        context=1_050_000, max_output=128_000, input_price=2.5, output_price=15,
        modalities=("text", "image"), tools=("function_calling", "structured_output", "web_search", "file_search", "computer_use"),
        strengths=("复杂推理", "编码", "代理工具调用"), limitations=("成本高于轻量模型"),
    ),
    _model(
        "gpt-5.6-sol", "GPT-5.6 Sol", "openai", "高难度专业推理与编码",
        _caps(analysis=.98, planning=.98, coding=.98, testing=.96, research=.94,
              data=.95, math=.97, validation=.96, synthesis=.96, risk=.95, safety=.94,
              vision=.94, tool_use=.98, long_context=.97),
        tier="frontier", reasoning=.99, speed=.52, reliability=.97,
        context=1_050_000, max_output=128_000, input_price=5, output_price=30,
        modalities=("text", "image"), tools=("function_calling", "structured_output", "web_search", "file_search", "computer_use"),
        strengths=("最高级复杂工作", "推理与编码", "超长上下文"), limitations=("价格较高", "速度偏慢"),
    ),
    _model(
        "claude-haiku-4.5", "Claude Haiku 4.5", "anthropic", "快速分类、写作和批量处理",
        _caps(analysis=.84, planning=.78, coding=.82, writing=.87, translation=.87,
              validation=.82, synthesis=.84, vision=.83, tool_use=.84),
        tier="economy", reasoning=.80, speed=.96, reliability=.90,
        context=200_000, max_output=64_000, input_price=1, output_price=5,
        modalities=("text", "image"), strengths=("低延迟", "近前沿轻量能力", "自然表达"),
        limitations=("上下文短于 Claude 高阶型号",),
    ),
    _model(
        "claude-sonnet-5", "Claude Sonnet 5", "anthropic", "快速的代理编码、分析与内容任务",
        _caps(analysis=.95, planning=.94, coding=.97, testing=.94, research=.92,
              writing=.95, synthesis=.94, risk=.91, validation=.93, vision=.92, tool_use=.96),
        tier="balanced", reasoning=.95, speed=.82, reliability=.96,
        context=1_000_000, max_output=128_000, input_price=3, output_price=15,
        modalities=("text", "image"), strengths=("速度与智能平衡", "代理编码", "长上下文"),
        limitations=("官方曾有阶段性优惠，目录使用长期标价"),
    ),
    _model(
        "claude-opus-4.8", "Claude Opus 4.8", "anthropic", "复杂代理编码与企业级工作",
        _caps(analysis=.98, planning=.98, coding=.99, testing=.97, research=.95,
              writing=.96, synthesis=.97, risk=.96, validation=.97, vision=.94, tool_use=.98),
        tier="frontier", reasoning=.99, speed=.55, reliability=.98,
        context=1_000_000, max_output=128_000, input_price=5, output_price=25,
        modalities=("text", "image"), strengths=("复杂代理编码", "长周期任务", "稳健分析"),
        limitations=("速度中等", "价格较高"),
    ),
    _model(
        "claude-fable-5", "Claude Fable 5", "anthropic", "最高能力长周期智能体",
        _caps(analysis=.995, planning=.995, coding=.99, testing=.98, research=.98,
              writing=.98, synthesis=.99, risk=.98, validation=.98, vision=.96, tool_use=.99),
        tier="premium", reasoning=.995, speed=.34, reliability=.985,
        context=1_000_000, max_output=128_000, input_price=10, output_price=50,
        modalities=("text", "image"), strengths=("长周期智能体", "最高可用能力", "复杂企业任务"),
        limitations=("价格最高", "延迟较高"),
    ),
    _model(
        "gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", "google", "高吞吐多模态与简单代理任务",
        _caps(analysis=.81, planning=.75, coding=.78, research=.79, data=.80,
              translation=.88, vision=.86, tool_use=.84, long_context=.88),
        tier="economy", reasoning=.77, speed=.98, reliability=.88,
        context=1_000_000, max_output=65_536, input_price=.25, output_price=1.5,
        modalities=("text", "image", "audio", "video"), tools=("function_calling", "structured_output", "web_search", "code_execution", "url_context"),
        strengths=("极低成本", "多模态", "高吞吐"), limitations=("不适合最高难度推理"),
    ),
    _model(
        "gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview", "google", "多模态理解、代理与长上下文",
        _caps(analysis=.96, planning=.95, coding=.95, testing=.92, research=.96,
              data=.95, math=.95, vision=.98, tool_use=.97, long_context=.98),
        tier="frontier", reasoning=.97, speed=.60, reliability=.91,
        context=1_000_000, max_output=65_536, input_price=2, output_price=12,
        modalities=("text", "image", "audio", "video"), tools=("function_calling", "structured_output", "web_search", "code_execution", "url_context", "file_search"),
        strengths=("原生多模态", "搜索与落地", "超长上下文"), limitations=("Preview 版本稳定性风险",), preview=True,
    ),
    _model(
        "deepseek-v4-flash", "DeepSeek V4 Flash", "deepseek", "极低成本推理、编码与高并发",
        _caps(analysis=.88, planning=.84, coding=.91, testing=.86, math=.91,
              data=.86, validation=.84, tool_use=.88, vision=.20),
        tier="economy", reasoning=.90, speed=.93, reliability=.87,
        context=1_000_000, max_output=384_000, input_price=.14, cached_price=.0028, output_price=.28,
        strengths=("极低价格", "编码与推理", "超长输出"), limitations=("文本为主", "目录价格采用缓存未命中输入价"),
    ),
    _model(
        "deepseek-v4-pro", "DeepSeek V4 Pro", "deepseek", "高性价比复杂推理与编码",
        _caps(analysis=.94, planning=.92, coding=.96, testing=.92, math=.96,
              data=.92, validation=.91, risk=.89, tool_use=.93, vision=.20),
        tier="balanced", reasoning=.97, speed=.72, reliability=.91,
        context=1_000_000, max_output=384_000, input_price=.435, cached_price=.003625, output_price=.87,
        strengths=("高性价比深度推理", "编码", "长上下文"), limitations=("文本为主",),
    ),
    _model(
        "grok-4.5", "Grok 4.5", "xai", "编码、代理任务与知识工作",
        _caps(analysis=.94, planning=.93, coding=.96, testing=.92, research=.91,
              writing=.91, tool_use=.95, risk=.88, validation=.90),
        tier="frontier", reasoning=.96, speed=.66, reliability=.91,
        context=500_000, max_output=128_000, input_price=2, cached_price=.5, output_price=6,
        strengths=("编码", "代理工作", "知识任务"), limitations=("长上下文触发更高价格，目录采用短上下文价"),
    ),
    _model(
        "mistral-small-4", "Mistral Small 4", "mistral", "混合指令、推理、编码和文档任务",
        _caps(analysis=.85, planning=.81, coding=.88, testing=.83, writing=.84,
              vision=.86, validation=.82, tool_use=.89),
        tier="economy", reasoning=.86, speed=.91, reliability=.87,
        context=256_000, max_output=64_000, input_price=.15, output_price=.6,
        modalities=("text", "image"), strengths=("低成本", "指令/推理/编码混合", "工具丰富"),
        limitations=("能力低于大型前沿模型",),
    ),
    _model(
        "qwen3.7-max", "Qwen 3.7 Max", "alibaba", "中文、多模态、思考与工具调用",
        _caps(analysis=.94, planning=.92, coding=.94, research=.90, writing=.94,
              translation=.95, vision=.94, tool_use=.95),
        tier="balanced", reasoning=.95, speed=.70, reliability=.91,
        context=1_000_000, max_output=131_072, input_price=12, output_price=36,
        currency="CNY", modalities=("text", "image"),
        strengths=("中文任务", "思考/非思考双模式", "内置工具"),
        limitations=("原始价格为人民币，未配置汇率前仅展示不参与美元预算路由",), routable=False,
    ),
)


def catalog_metadata() -> dict:
    return {
        "snapshot_date": SNAPSHOT_DATE,
        "pricing_unit": "per 1M tokens; currency is explicit per model",
        "capability_notice": "能力分数是 CostWeave v0.3 的可编辑路由先验，不是厂商基准成绩。",
        "execution_notice": "当前仍为离线模拟；目录用于决策演示，不会发起真实 API 调用。",
        "sources": SOURCES,
    }
