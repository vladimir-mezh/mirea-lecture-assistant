"""The AI on duty: called only when the app's own check of a pair finds a problem.

The app checks itself for free five and twenty minutes into every online pair.
Only when something is wrong does it start an AI agent the person already uses
(Codex or Claude Code), without a window, with one task: find out what is wrong
through the MCP tools, repair what those tools can repair, and report.

The agent gets no shell or file access from us: Codex runs in its read-only
sandbox and Claude Code may use only this app's MCP tools. The report it files
is scrubbed by the app before it goes anywhere.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

REPOSITORY = "vladimir-mezh/mirea-lecture-assistant"
TIMEOUT_SECONDS = 10 * 60
MCP_NAME = "mirea-lecture-assistant"

PROMPT = """Ты — ИИ-дежурный приложения MIREA Lecture Assistant на компьютере студента.
Приложение само заходит на онлайн-пары МИРЭА и отмечает посещаемость по QR. Его проверка
нашла проблему на текущей паре. Твоя задача — разобраться и починить через инструменты MCP
сервера mirea-lecture-assistant. Других инструментов не используй.

1. Вызови check_health. Если verdict равен "ok" — ответь «OK» и сразу закончи: проблема
   ушла сама, ничего больше не делай.
2. Если в network.verdict "vpn_suspected" — чинить приложение бесполезно: вызови
   get_vpn_help и перескажи пользователю коротко и простыми словами, что сделать с VPN.
   Если "offline" — интернета нет, скажи об этом.
3. Иначе бери проблемы из problems по порядку и для каждой вызывай repair с одним действием
   из её actions (начиная с первого). После каждого действия вызывай wait_and_check
   (60–90 секунд), пока проблема не уйдёт. Не больше пяти действий за раз.
   Если нужно, посмотри get_recent_problems.
4. В конце обязательно вызови report_fix: summary — 3–6 строк по-русски (что было, что ты
   сделал, помогло ли), fixed — true, если check_health в итоге "ok".

Никогда не проси и не трогай пароли, коды из писем, QR-коды и сообщения в чате лекции.
Не делай ничего сверх этого. Ответ пользователю — коротко, без технических подробностей."""


@dataclass(frozen=True)
class Agent:
    key: str  # the ai_clients key of the same program
    name: str
    executable: str

    def command(self) -> list[str]:
        if self.key == "codex":
            # "-": the task comes on stdin, untouched by cmd.exe's quoting.
            return [self.executable, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"]
        return [
            self.executable,
            "-p",
            "--allowedTools",
            f"mcp__{MCP_NAME}",
            "--output-format",
            "text",
        ]


def _find(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    # npm puts global commands here; it is not always on PATH for GUI programs.
    appdata = os.environ.get("APPDATA")
    if appdata:
        for suffix in (".cmd", ".exe"):
            candidate = Path(appdata) / "npm" / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def available() -> list[Agent]:
    """Agents that can run without a window: Codex CLI and Claude Code."""
    agents = []
    codex = _find("codex")
    if codex:
        agents.append(Agent("codex", "Codex", codex))
    claude = _find("claude")
    if claude:
        agents.append(Agent("claude-code", "Claude Code", claude))
    return agents


def run(agent: Agent, work_dir: Path, log_dir: Path, *, context: str = "") -> int:
    """Run the agent once (blocking; call from a worker thread). Returns its exit code."""
    work_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output = log_dir / f"ai-duty-{stamp}.log"
    task = PROMPT + (f"\n\nЧто заметило приложение: {context}" if context else "")
    log.info("ai_duty_started agent=%s", agent.key)
    try:
        with output.open("w", encoding="utf-8") as sink:
            result = subprocess.run(
                agent.command(),
                input=task,
                text=True,
                encoding="utf-8",
                stdout=sink,
                stderr=subprocess.STDOUT,
                cwd=work_dir,
                timeout=TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except subprocess.TimeoutExpired:
        log.warning("ai_duty_timeout agent=%s", agent.key)
        return -1
    except OSError:
        log.warning("ai_duty_start_failed agent=%s", agent.key, exc_info=True)
        return -2
    log.info("ai_duty_finished agent=%s code=%s", agent.key, result.returncode)
    return result.returncode


def issue_url(title: str, body: str) -> str:
    from urllib.parse import urlencode

    query = urlencode({"title": title, "body": body[:5000], "labels": "ai-duty"})
    return f"https://github.com/{REPOSITORY}/issues/new?{query}"


def post_issue(title: str, body: str) -> str | None:
    """File the report with the person's own GitHub CLI, if it is signed in."""
    gh = shutil.which("gh")
    if not gh:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        signed_in = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=flags,
        )
        if signed_in.returncode != 0:
            return None
        created = subprocess.run(
            [gh, "issue", "create", "--repo", REPOSITORY, "--title", title, "--body-file", "-"],
            input=body,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if created.returncode != 0:
        log.warning("ai_duty_report_post_failed")
        return None
    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    return url if url.startswith("https://github.com/") else None
