#!/usr/bin/env python3
"""Screen A shares and email one combined screening and analysis report."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STRATEGY = "balanced_alpha"
MAX_RESULTS = 5


def validated_candidates(result: dict) -> list[dict]:
    """Refuse to interpret a failed or stale source as a valid selection."""
    count = result.get("snapshot_count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise ValueError("选股快照为空或不可用，无法产生可靠候选。")
    degradations = result.get("degradation") or []
    if any("stale" in str(item).lower() or "陈旧" in str(item) for item in degradations):
        raise ValueError("选股快照使用了陈旧缓存，本次不发送候选名单。")
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_RESULTS:
        raise ValueError("选股结果格式不符合预期。")
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not re.fullmatch(r"\d{6}", str(candidate.get("code", ""))):
            raise ValueError("选股结果包含无效的 A 股代码。")
        code = str(candidate["code"])
        if code in seen:
            raise ValueError("选股结果包含重复代码。")
        seen.add(code)
    return candidates


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).replace("|", "\\|")[:300]


def has_candidate_reports(details: str, candidates: list[dict]) -> bool:
    """Require a real stock section for each candidate, not a mention in prose."""
    return all(
        re.search(r"^#{2,3}\s+[^\n]*\(" + re.escape(str(candidate["code"])) + r"\)", details, re.MULTILINE)
        for candidate in candidates
    )


def screening_summary(result: dict, candidates: list[dict], day: str) -> str:
    lines = [
        f"# {day} A 股自动选股与分析", "",
        "策略：均衡多因子（balanced_alpha）；候选不超过 5 只。",
        "该策略按估值、流动性、动量和稳定性等条件筛选；其现有规则包含股价 3–220 元的范围。",
        f"快照股票数：{result['snapshot_count']}；数据源：{clean_text(result.get('snapshot_source')) or '未说明'}。",
        f"排序方式：{'AI 辅助排序' if result.get('llm_ranked') else '因子排序（AI 未参与排序或已降级）'}。",
        "", "## 候选名单", "",
    ]
    if not candidates:
        lines.append("本次没有符合条件的候选股票。")
    else:
        lines.extend(["| 代码 | 名称 | 评分 | 入选依据 |", "|---|---|---:|---|"])
        for c in candidates:
            score = c.get("score")
            lines.append(f"| {c['code']} | {clean_text(c.get('name'))} | {clean_text(score) if score is not None else '未提供'} | {clean_text(c.get('reason')) or '请查看个股报告'} |")
    if result.get("warnings") or result.get("source_errors") or result.get("degradation"):
        lines.extend(["", "数据提示：本次部分数据源或处理步骤存在警告或降级，请结合原始数据核验。"])
    lines.extend(["", "结果用于研究，不保证未来表现。", ""])
    return "\n".join(lines)


def main() -> int:
    from src.config import get_config
    from src.core.trading_calendar import get_trading_dates
    from src.services.screening_service import ScreeningService
    from src.notification_sender.email_sender import EmailSender

    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    force = os.getenv("SCREENING_FORCE_RUN", "false").lower() == "true"
    if not force:
        trading_dates = get_trading_dates("cn", day, day)
        if trading_dates is None:
            raise RuntimeError("无法确认 A 股交易日，本次不运行自动选股。")
        if len(trading_dates) == 0:
            print("A 股非交易日，跳过自动选股。")
            return 0
    os.chdir(ROOT)
    config = get_config()
    result = ScreeningService(config).screen(strategy=STRATEGY, market="cn", max_results=MAX_RESULTS)
    candidates = validated_candidates(result)
    summary = screening_summary(result, candidates, day.isoformat())
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    full_report = summary
    if candidates:
        # A fresh runner and explicit path prevent an old report from being emailed.
        daily_report = reports / f"report_{day:%Y%m%d}.md"
        daily_report.unlink(missing_ok=True)
        codes = ",".join(str(c["code"]) for c in candidates)
        command = [sys.executable, "main.py", "--stocks", codes, "--no-market-review", "--no-notify"]
        if force:
            command.append("--force-run")
        subprocess.run(command, cwd=ROOT, check=True)
        if not daily_report.is_file() or not daily_report.read_text(encoding="utf-8").strip():
            raise RuntimeError("个股分析没有生成报告，本次不发送不完整邮件。")
        details = daily_report.read_text(encoding="utf-8")
        if not has_candidate_reports(details, candidates):
            raise RuntimeError("部分候选股缺少分析报告，本次不发送不完整邮件。")
        full_report += "\n---\n\n" + details
        daily_report.unlink()
    report_path = reports / f"screening_{day:%Y%m%d}.md"
    report_path.write_text(full_report, encoding="utf-8")
    sender = EmailSender(config)
    if not sender.send_to_email(full_report, subject=f"A股自动选股与分析报告 - {day.isoformat()}"):
        raise RuntimeError("选股邮件发送失败，请检查邮件配置与 Actions 日志。")
    print(f"选股报告已发送；候选数：{len(candidates)}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
