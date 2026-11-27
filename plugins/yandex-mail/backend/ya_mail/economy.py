"""Select a local mail-only Codex profile from the installed client's current catalog."""

import json
import shutil
import subprocess

from .accounts import Problem


def select(catalog):
    eligible = [
        m
        for m in catalog["models"]
        if m.get("visibility", "list") == "list" and m.get("supported_in_api", True)
    ]

    def rank(m):
        slug = m["slug"].lower()
        if slug.endswith("-nano"):
            return 0
        if slug.endswith(("-luna", "-mini")):
            return 1
        return 2

    for m in sorted(eligible, key=lambda m: (rank(m), m.get("priority", 999))):
        levels = {v["effort"] for v in m.get("supported_reasoning_levels", [])}
        for effort in ("none", "minimal", "low"):
            if effort in levels and rank(m) < 2:
                return m["slug"], effort
    raise Problem(
        "В каталоге Codex нет доступной базовой модели с минимальным режимом рассуждений."
    )


def launch(prompt, show):
    codex = shutil.which("codex")
    if not codex:
        raise Problem("Для экономичного запуска нужен установленный Codex CLI.")
    result = subprocess.run(
        [codex, "debug", "models"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise Problem("Codex не смог прочитать каталог доступных моделей.")
    model, effort = select(json.loads(result.stdout))
    if show:
        print(json.dumps({"model": model, "reasoning_effort": effort}))
        return 0
    if not prompt:
        raise Problem("Укажите поручение для чтения почты или --show-model.")
    context = (
        "Используй Ya-Ma для чтения почты. Начни с заголовков. Открывай только нужные письма. "
        "Не отправляй и не удаляй письма в этом режиме. Поручение пользователя:\n"
        + prompt
    )
    return subprocess.run(
        [
            codex,
            "exec",
            "--skip-git-repo-check",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-",
        ],
        check=False,
        input=context.encode("utf-8"),
    ).returncode
