#!/usr/bin/env python3
"""Screen A shares and email one combined screening and analysis report."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STRATEGY = "balanced_alpha"
MAX_RESULTS = 5


def validated_candidates(result: dict, market: str = "cn") -> list[dict]:
    """Refuse to interpret a failed or stale source as a valid selection."""
    count = result.get("snapshot_count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise ValueError("选股快照为空或不可用，无法产生可靠候选。")
    if market == "us" and count < 30:
        raise ValueError(f"美股有效行情仅 {count} 只，覆盖不足，无法可靠选股。")
    degradations = result.get("degradation") or []
    if any("stale" in str(item).lower() or "陈旧" in str(item) for item in degradations):
        raise ValueError("选股快照使用了陈旧缓存，本次不发送候选名单。")
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_RESULTS:
        raise ValueError("选股结果格式不符合预期。")
    seen = set()
    for candidate in candidates:
        pattern = r"\d{6}" if market == "cn" else r"[A-Z]{1,5}(?:[.-][A-Z])?"
        if not isinstance(candidate, dict) or not re.fullmatch(pattern, str(candidate.get("code", ""))):
            raise ValueError("选股结果包含无效的股票代码。")
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


def screening_summary(result: dict, candidates: list[dict], day: str, market: str = "cn", excluded: list[str] | None = None) -> str:
    if market == "us":
        lines = [
            f"# {day} 美股自动选股与分析", "",
            "日期为美东最近已收盘交易日，报告生成时间以北京时间为准。",
            "策略：美股稳健多因子（us_balanced）；候选不超过 5 只。",
            "主要依据流动性、动量及稳定性筛选：股价不低于 5 美元、成交额不低于 2000 万美元、市值不低于 20 亿美元，当日涨跌幅在 -8% 至 8% 之间。",
            "选股范围优先标普 500 成分股，来源不可用时退回内置约 50 支大盘股，不代表全部美股；初筛未采集 PE/PB，未使用估值硬过滤或估值因子。",
            f"有效快照股票数：{result['snapshot_count']}；数据源：{clean_text(result.get('snapshot_source')) or '未说明'}。",
            f"排序方式：{'AI 辅助排序' if result.get('llm_ranked') else '因子排序（AI 未参与排序或已降级）'}。",
            "", "## 候选名单", "",
        ]
        if "/default:" in str(result.get("snapshot_source", "")):
            lines.extend(["注意：本次使用内置大盘股备用池，并非标普 500 全量成分股。", ""])
        if excluded:
            lines.extend(["兼容性限制：以下分类股代码暂不支持个股分析，已从邮件中剔除：" + ", ".join(excluded), ""])
    else:
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


def select_us_session(now: datetime, dates, session_close, force: bool) -> date | None:
    """Choose a completed NY session; normal scheduled runs skip holidays."""
    if now.tzinfo is None:
        raise ValueError("current time must be timezone-aware")
    today = now.astimezone(ZoneInfo("America/New_York")).date()
    if dates is None:
        raise RuntimeError("无法确认美股交易日，本次不运行选股。")
    sessions = sorted(d.date() if isinstance(d, datetime) else d for d in dates)
    if not force and today not in sessions:
        return None
    completed = []
    for session in sessions:
        close = session_close(session)
        if close.tzinfo is None:
            raise RuntimeError("交易日历未提供带时区的收盘时间。")
        if now.astimezone(timezone.utc) >= close.astimezone(timezone.utc):
            completed.append(session)
    if not completed:
        raise RuntimeError("找不到已收盘的美股交易日。")
    if not force and completed[-1] != today:
        raise RuntimeError("美股当日尚未收盘，不运行定时选股。")
    return completed[-1]


def supported_us_candidates(candidates: list[dict]) -> tuple[list[dict], list[str]]:
    """Do not conflate Yahoo class shares with unsupported analysis tickers."""
    supported = [candidate for candidate in candidates if re.fullmatch(r"[A-Z]{1,5}", str(candidate["code"]))]
    excluded = [str(candidate["code"]) for candidate in candidates if candidate not in supported]
    if candidates and not supported:
        raise ValueError("候选均为当前单股分析不支持的分类股代码，未发送邮件。")
    return supported, excluded


def main() -> int:
    from src.config import get_config
    from src.core.trading_calendar import get_trading_dates
    from src.services.screening_service import ScreeningService
    from src.notification_sender.email_sender import EmailSender

    market = os.getenv("SCREENING_MARKET", "cn").strip().lower()
    if market not in ("cn", "us"):
        raise ValueError("不支持的选股市场。")
    local_day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    day = local_day
    force = os.getenv("SCREENING_FORCE_RUN", "false").lower() == "true"
    if market == "us":
        import exchange_calendars as xcals

        now = datetime.now(timezone.utc)
        ny_day = now.astimezone(ZoneInfo("America/New_York")).date()
        dates = get_trading_dates("us", ny_day - timedelta(days=14), ny_day)
        cal = xcals.get_calendar("XNYS")
        day = select_us_session(now, dates, cal.session_close, force)
        if day is None:
            print("美股非交易日，跳过自动选股。")
            return 0
        os.environ["SCREENING_US_TARGET_DATE"] = day.isoformat()
    elif not force:
        trading_dates = get_trading_dates("cn", day, day)
        if trading_dates is None:
            raise RuntimeError("无法确认 A 股交易日，本次不运行自动选股。")
        if len(trading_dates) == 0:
            print("A 股非交易日，跳过自动选股。")
            return 0
    os.chdir(ROOT)
    config = get_config()
    strategy = STRATEGY if market == "cn" else "us_balanced"
    result = ScreeningService(config).screen(strategy=strategy, market=market, max_results=MAX_RESULTS)
    candidates = validated_candidates(result, market)
    excluded = []
    if market == "us":
        candidates, excluded = supported_us_candidates(candidates)
    summary = screening_summary(result, candidates, day.isoformat(), market, excluded)
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    full_report = summary
    if candidates:
        # A fresh runner and explicit path prevent an old report from being emailed.
        daily_report = reports / f"report_{local_day:%Y%m%d}.md"
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
    report_path = reports / (f"screening_us_{day:%Y%m%d}.md" if market == "us" else f"screening_{day:%Y%m%d}.md")
    report_path.write_text(full_report, encoding="utf-8")
    sender = EmailSender(config)
    market_name = "A股" if market == "cn" else "美股"
    if not sender.send_to_email(full_report, subject=f"{market_name}自动选股与分析报告 - {day.isoformat()}"):
        raise RuntimeError("选股邮件发送失败，请检查邮件配置与 Actions 日志。")
    print(f"选股报告已发送；候选数：{len(candidates)}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
