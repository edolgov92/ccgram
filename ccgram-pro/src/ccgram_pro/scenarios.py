"""Scenario launcher — one-tap, pre-baked prompts forwarded to the agent.

A 🎬 button rides the Stop-summary action row (just before ⚙️ Settings). Tapping
it opens a small menu of "scenarios": canned, multi-line prompts the user would
otherwise paste by hand. Picking one (a) edits the menu message into a short
"Scenario triggered" record kept in the chat history and (b) forwards the full
prompt to the bound agent exactly as a normal user turn — bypassing the batcher,
via the original (pre-wrap) forward captured by :mod:`input_pipeline.intercept`
— then starts the live progress bubble.

Scenarios shipping today:

- **Self-review** (every session): a deep self-review checklist over the last,
  unpushed changes.
- **Commit & push** (git repos): review, write a message, push the branch.
- **Feature branch + push** (git repos): create a feature branch, commit the
  changes, and push it.
- **Sync main branch** (git repos): switch to the repo's default branch
  (``develop`` for the humanprogram backend, ``main``/``master`` elsewhere — read
  from the remote) and fast-forward pull; refuses to clobber uncommitted work.
- **PR auto-fixer** (humanprogram backend/app only): drives the repo's
  ``var/pr-check.sh`` loop to address CI + Cursor-bot feedback until the PR is
  green. It needs one input — the PR number — collected through a free-text
  reply (mirrors the voice-edit flow: a high-priority ``MessageHandler`` in
  group −12 consumes the next message in that topic before ccgram's text
  handler can forward it).
- **Manual testing** (humanprogram backend/app only): the pre-release manual
  test run on the VPS — environment preflight (dev DB / servers / headless
  Chrome), API-first cases with browser evidence, a Russian HTML report built
  by the repo harness and published as a claude.ai artifact whose link is
  guaranteed to reach Telegram (``tldr.extract_artifact_links``).
- **All — branch → PR → auto-fix** (humanprogram backend/app only): the full
  pipeline — create a feature branch, commit, push, open a PR with ``gh``, then
  drive the PR auto-fixer on that new PR until green. No PR number to supply —
  the agent uses the one it just created.

The PR-fixer (and the all-in-one flow) is gated on the session's git ``origin`` remote resolving to a
known humanprogram repository, so the ``REPO`` env (``backend`` →
``primer_server``, ``frontend`` → ``hyper_school_dashboard``) is unambiguous and
the user only has to supply the number.

**Full-stack sessions** (composite projects, ``WindowSidecar.project_repos``)
span several repos: eligibility checks run over every repo, git/review/testing
prompts get a lead-in naming them all, the PR-fixer asks for one PR number per
repo (``1234 567``, ``-`` for none) and the all-in-one flow opens a PR in each
repo with changes and drives them all to green.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from . import state

logger = structlog.get_logger()

_CB_PREFIX = "ccgrampro:scn:"

# Layer-local user_data key holding the pending PR-number context:
# {chat_id, thread_id, window_id, repo, prompt_msg_id, user_id}.
AWAITING_PR_NUMBER = "_ccgrampro_awaiting_pr_number"

# Single-shot install guard.
_installed = False

# git origin remote ``owner/repo`` segment → REPO env value pr-check.sh expects.
# Matching the full ``humanprogram/<repo>`` segment (works for SSH, HTTPS, and
# host-alias remote URLs) gates the PR auto-fixer to EXACTLY these two repos —
# any other repository (no match → None) hides the option entirely, and a repo
# that merely shares a name under a different owner won't false-positive.
_REPO_BY_REMOTE: tuple[tuple[str, str], ...] = (
    ("humanprogram/primer_server", "backend"),
    ("humanprogram/hyper_school_dashboard", "frontend"),
)


# ── scenario prompts ──────────────────────────────────────────────────────────

_SELF_REVIEW_PROMPT = """\
Нужно провести тщательное и глубокое code review последних реализованных тобой изменений, которые еще не были запушены или были запушены только что.

Необходимо убедиться, что в реализации нет проблем или пробелов и что она полностью соответствует требованиям. Ничего не должно быть упущено. Код должен быть профессиональным, готовым к production, соответствовать best practices и текущим правилам проекта.

Запускать workflows не нужно. Просто еще раз внимательно прочитай свои изменения и проведи тщательное селф-ревью.

Проверь:

- Главный вопрос: вообще все ли правильно реализовано с высоты взгляда - правильно согласно рекварементам и правильно в архитектуре? Иначе мы просто будем фиксить микро-изьяны изначально неверной реализации. Если есть какие-то глобальные проблемы, то на этом этапе лучше остановиться и обсудить с мной.
- Полностью ли решение соответствует требованиям?
- Устраняет ли оно корневую причину проблемы, а не только ее симптом?
- Реализовано ли исправление на правильном архитектурном уровне?
- Достаточно ли решение простое и не переусложнено ли оно?
- Обработаны ли edge cases?
- Корректно ли обрабатываются ошибки?
- Обновлены ли types, contracts, JSDocs или Typedocs?
- Обновлены ли все затронутые call sites?
- Являются ли тесты содержательными и действительно полезными? Только для Backend.
- Безопасна ли реализация?
- Приемлема ли производительность?
- Сохранена ли backward compatibility там, где это необходимо?
- Не осталось ли TODOs, debug logs, dead code или временных артефактов?
- Уверены ли мы, что изменения не привели к regressions?

Если ты обнаружишь какие-либо проблемы, исправь их перед завершением работы."""

# ``__PR__`` / ``__REPO__`` are substituted via str.replace (no brace escaping,
# the body contains literal JSON braces).
_PR_FIXER_TEMPLATE = """\
Мы переходим в режим обработки code review feedback и подготовки PR #__PR__ к merge: нужно довести все проверки до зеленого состояния. Для этого необходимо использовать следующий скрипт:

Script: /root/projects/humanprogram/backend/var/pr-check.sh
Env:    REPO=__REPO__   (required — используй именно это значение)
        OWNER=humanprogram   (default)
        POLL_SECS=30   (default)

Commands:
  REPO=__REPO__ /root/projects/humanprogram/backend/var/pr-check.sh status __PR__
      Polls until no check is in_progress (30s intervals), then prints JSON:
        {
          rollup,
          checks: [{name, status, conclusion, url}],
          cursor_comments: [{id, path, line, url, title, severity, description, locations[]}]
        }
      Only unresolved cursor-bot threads. If cursor check failed with no
      comments yet, waits 30s once more before returning.

  REPO=__REPO__ /root/projects/humanprogram/backend/var/pr-check.sh reply __PR__ <comment-id> "<text>"
      Posts a threaded reply. comment-id = `id` from cursor_comments.

  REPO=__REPO__ /root/projects/humanprogram/backend/var/pr-check.sh resolve __PR__ <comment-id>
      Marks the thread containing that comment as resolved.

  REPO=__REPO__ /root/projects/humanprogram/backend/var/pr-check.sh reply-resolve __PR__ <comment-id> "<text>"
      Reply, then resolve, in one call.

Status JSON goes to stdout; progress messages go to stderr.

Не нужно ограничивать длину вывода скрипта или время его выполнения. Скрипт может работать некоторое время — review от Cursor Bot может занимать от 5 до 20 минут — пока статус PR не будет готов к проверке. Так же, если есть мерж конфликты, то они не будут видны в output скрипта, проверяй это отдельно.

Если все готово или возникла проблема, требующая моего внимания, просто заверши ход итоговым сообщением (или вопросом ко мне) — на сервере нет звука, я увижу его в Telegram и отвечу.

Однако, если какая-либо проверка завершилась с ошибкой или появились комментарии от Cursor Bot, ты должен автоматически проанализировать проблему и исправить ее:

- Если падает Typecheck, запусти локально `pnpm typecheck` и исправь ошибки.
- Если падают Unit tests, запусти локально `pnpm test` и исправь ошибки.
- Если появились комментарии от Cursor Bot, тщательно проанализируй их, чтобы определить, являются ли они реальными проблемами или false positives. Не делай поспешных выводов — удели достаточно времени анализу.

  - Если это реальные проблемы, исправь их тщательно и профессионально. Убедись, что ничего не сломано и каждая проблема действительно устранена. Нам нужен production-ready код.

    Нужно осторожно определять, является ли комментарий реальной проблемой, чтобы случайно не отклониться слишком далеко от первоначальных требований. Если проблема критическая, требует обсуждения или существенной переработки, лучше остановить цикл и спросить меня. Я не хочу после завершения всех итераций обнаружить, что какая-либо функциональность была удалена или значительно переработана без моего согласования.

    После исправления выполни команду `reply-resolve`, чтобы оставить комментарий о том, что проблема устранена, и закрыть соответствующий thread. Затем сделай commit и push изменений. Используй профессиональный commit message и не указывай Claude в качестве collaborator в commit message.

    После этого снова запусти `pr-check.sh`, дождись завершения проверок и проверь статус PR после внесенных изменений. В начале работы скрипт делает короткую задержку в 5 секунд, чтобы GitHub успел обработать commit и запустить pipelines.

  - Если комментарий является false positive, выполни команду `reply-resolve`, оставь объяснение, почему это false positive, и закрой соответствующий thread. Если реальных проблем, требующих исправления, нет, создавать commit не нужно.

Продолжай этот процесс, пока все проблемы не будут устранены, все checks не станут зелеными и PR не будет полностью готов к merge.

После каждой итерации выводи короткий, но заметный заголовок с кратким описанием прогресса и выполненных действий.

Действуй осторожно, чтобы ничего не сломать. Нам нужна профессиональная и production-ready реализация, которая соответствует best practices и текущим правилам проекта. Не используй быстрые исправления или хаки — реализация должна быть качественной и корректной.

Еще одно правило — не допускать бесконечного цикла. Выполни не более 20 итераций. Обычно Cursor Bot оставляет от 1 до 5 комментариев после каждого запуска и не публикует все комментарии сразу, поэтому несколько итераций — это нормально. Однако после 20-й итерации остановись, чтобы избежать бесконечного цикла.

Не выполняй merge PR самостоятельно. Твоя единственная цель — довести PR до состояния, в котором все checks зеленые и он готов к merge."""


def _pr_fixer_prompt(pr: str, repo: str) -> str:
    return _PR_FIXER_TEMPLATE.replace("__PR__", pr).replace("__REPO__", repo)


_COMMIT_PUSH_PROMPT = """\
Please commit the current changes and push them.

- First review what actually changed (git status + git diff). Stage only the files that belong in this change — do NOT blindly `git add -A`. Leave out anything that looks unintended or unrelated (build artifacts, local scratch/config, editor files, files outside the scope of recent work); if you spot such a file, leave it unstaged and tell me about it.
- Write a clear, meaningful commit message describing the INTENT of the change (not a file list), following this project's existing commit-message conventions (use Conventional Commits if that's the style here).
- Do NOT add a `Co-Authored-By` trailer and do NOT mention Claude, AI, or this assistant anywhere in the commit message.
- Then push to the current branch's upstream (set the upstream if it doesn't exist yet).
- If there is nothing staged to commit but there are unpushed commits, just push them.
- If there is genuinely nothing to commit or push, say so. If anything is ambiguous or risky (unrelated changes mixed together, a force-push would be needed, detached HEAD, conflicts), STOP and ask me instead of guessing.

When done, report the exact commit message you used and the push result (branch + remote)."""


_SYNC_MAIN_PROMPT = """\
Please switch to the repository's default (main) branch and pull the latest changes.

- First determine the default branch from the REMOTE — it is NOT always "main". Read it from `git remote show origin` (the "HEAD branch:" line) or `git symbolic-ref refs/remotes/origin/HEAD`. For this project's humanprogram backend the default branch is `develop`; for most repos it's `main` or `master`. Use whatever the remote actually reports.
- Before switching, check `git status`. If the working tree is dirty — staged, unstaged, OR untracked changes that matter — STOP and tell me. Do NOT stash, discard, reset, or force anything; I will decide what to do with the in-progress work.
- If the working tree is clean, check out the default branch and pull with fast-forward only (`git pull --ff-only`). If a fast-forward is not possible, STOP and tell me rather than merging or rebasing.
- If you are inside a git worktree and the default branch is already checked out in another worktree, STOP and tell me instead of forcing it.

When done, report which branch you are now on and what was pulled (e.g. "develop — fast-forwarded 12 commits" or "main — already up to date")."""


_FEATURE_BRANCH_PROMPT = """\
Please create a new feature branch for the current changes, then commit and push it.

- First review what actually changed (git status + git diff) so you understand the change and can pick a good branch name.
- Create a NEW branch off the current branch with a clear, conventional name that matches this repo's existing branch style (e.g. `feature/<short-kebab-summary>`). If you are already on a feature branch that has unrelated in-progress work, STOP and ask me before branching.
- Stage only the files that belong in this change — do NOT blindly `git add -A`. Leave out anything unintended or unrelated (build artifacts, local scratch/config, editor files, files outside the scope of recent work); if you spot such a file, leave it unstaged and tell me about it.
- Write a clear, meaningful commit message describing the INTENT of the change (not a file list), following this project's existing commit-message conventions (use Conventional Commits if that's the style here). Do NOT add a `Co-Authored-By` trailer and do NOT mention Claude, AI, or this assistant anywhere in the commit message.
- Push the new branch and set its upstream (`git push -u origin <branch>`).
- If there is genuinely nothing to commit, say so. If anything is ambiguous or risky (unrelated changes mixed together, detached HEAD, conflicts), STOP and ask me instead of guessing.

When done, report the branch name, the exact commit message you used, and the push result (branch + remote)."""


# Manual testing on the VPS (humanprogram backend/app only). The report is
# published as a claude.ai artifact; the summarizer guarantees the link lands
# in Telegram even if the TL;DR omitted it (see tldr.extract_artifact_links).
_MANUAL_TESTING_PROMPT = """\
# Ручное тестирование

Мы переходим к ручному тестированию на локальной среде этого сервера перед выкаткой на прод. Задача: убедиться, что проверены все основные кейсы и edge-cases, и оставить отчёт, после которого не остаётся сомнений, что можно катить.

Фича может быть только бэкендная, только фронтовая или сквозная. Разберись по диффу, какие слои затронуты, и тестируй те, что затронуты.

## Прежде всего

**Ничего не начинай, пока пул реквесты не зелёные.** Бот на PR может выдать замечания, мы будем фиксить, всё поменяется, и тестирование придётся повторять. Проверь статусы всех связанных PR и только потом начинай.

**Прочитай доки по ручному тестированию в репозиториях, они уже написаны:**

- `/root/projects/humanprogram/backend/docs/manual-testing.md` - метод, факты про API и аутентификацию, снимок/восстановление состояния, что нельзя проверить локально
- `/root/projects/humanprogram/app/docs/manual-testing-ui.md` - браузер, хранилище, оверлеи, селекторы, контролы, которые скрыты по правилу, а не сломаны

**Используй готовый харнесс, не пиши его заново:**

- `backend/test/manual/lib/` - `record()` и фильтр `ONLY=`, снимок базы с проверкой восстановления, HTTP и авторизация, выпуск magic-ссылок
- `backend/test/manual/report/build.js` - сборка самодостаточного HTML-отчёта
- `app/test/manual/lib/` - запуск Chrome, ожидание по маркеру, клики, гашение оверлеев, чтение сессии из localStorage, проверка композера
- шаблоны прогона: `test/manual/runs/template.run.js` в обоих репозиториях

Скопируй шаблон **рядом с ним же**, в `test/manual/runs/`, и правь копию. Там `.gitignore` пропускает только сам шаблон, поэтому скрипт прогона не попадёт в коммит, но относительные пути до библиотек резолвятся. Копировать в `/tmp` не надо - оттуда `require('../lib/...')` не найдётся.

## Среда на этом сервере (VPS)

- Репозитории: бэкенд `/root/projects/humanprogram/backend` (`primer_server`), фронт `/root/projects/humanprogram/app` (это `hyper_school_dashboard` - в доках и харнессе он называется `dashboard`). Из-за этого дефолт `MT_DASHBOARD_ENV=../dashboard/.env` здесь не резолвится - передавай явно `MT_DASHBOARD_ENV=/root/projects/humanprogram/app/.env`
- Бэкенд `localhost:3000` (он же админка; `MT_API_BASE=http://localhost:3000/v1`), фронт `localhost:5173` (`MT_APP_BASE=http://localhost:5173`). Ничего из этого не запущено постоянно - поднимаешь сам: бэкенд `npm run start:dev` из корня backend, фронт `pnpm dev` из корня app. Если что-то уже висит на этих портах - убей и подними заново со свежим кодом. После правок бэкенда перезапускай его. Логи серверов сохраняй в папку evidence
- База: Docker MySQL 8.4 на `127.0.0.1:3306`, пользователь `root` без пароля, схема `primer_development` (бэкенд берёт её по умолчанию, если в `.env` не задано иное). Redis на `127.0.0.1:6379` уже запущен
- Перед стартом проверь, что миграции накачены (`npm run db:migrate`): сервер поднимется и с непринятой миграцией, но будет валиться на запросах
- **Прод-реплика (`hp-db`) - только для чтения и НЕ для тестирования.** Никаких мутаций через неё, никакой подмены dev-базы прод-данными. Тестируем только в `primer_development`
- База и данные dev-среды в твоём распоряжении, готовь любые фикстуры

**Префлайт - обязателен, до любых действий.** Проверь и запиши в отчёт: контейнер MySQL запущен, схема `primer_development` существует и в ней есть данные (`SHOW DATABASES LIKE 'primer_development'`, `SELECT COUNT(*) FROM users`), миграции накачены, у бэкенда есть `.env` (или он стартует на дефолтах), у фронта в `.env` `VITE_API_BASE_URL=http://localhost:3000/v1` и basic-auth совпадает с `api_users` в dev-базе, оба сервера отвечают. **Если dev-базы, данных или тестовых аккаунтов нет - остановись и доложи, что именно отсутствует и что нужно для подготовки (дамп локальной базы, `.env`), ничего не выдумывай и не подменяй.**

**Аккаунты** (dev-база - копия моей локальной; проверь их наличие запросом перед прогоном). На почте `eugene@humanprogram.com` висят ДВА аккаунта - `4` и `41`. Логин отдаёт список и штампует введённый адрес на обе записи, так что по email их не различить; `signIn` это знает и требует явный выбор: `signIn(email, code, { userId: 4 })`. Без `userId` он бросит исключение со списком - это не поломка, а защита от молчаливого входа не в тот аккаунт.

- `4` - мой основной аккаунт (роль `admin`, но это студент с админ-правами: есть сквады, история чата, объявления, родители). Годится почти для всего
- `72` - его родитель (Valentina Dolgova)
- `1196722` → `1196723` (Test Parent23 → Test Student23) - пара для перехода «родитель → ребёнок». У аккаунта 4 эта кнопка НЕ показывается, потому что у него своя почта, и это правильное поведение продукта. У этой пары не пройден онбординг: проставь `onboardingCompletedAt` перед прогоном и сними после
- Создаёшь новых пользователей - обязательно проставь им онбординг в базе, иначе после входа откроется онбординг, а не дашборд

**Comet Chat.** Дев-аккаунт превысил лимит в 100 юзеров, поэтому ошибки и тост с 402 ожидаемы, особенно для новых пользователей. Игнорируй их, но следи, чтобы тост не попадал в скриншоты отчёта (`dismissToasts()` в харнессе).

**Пароль ко всем локальным аккаунтам, если где-то спросят: `1234`.** Еще один тестовый код входа `3791`, работает только для аккаунтов с `isTester = 1` или `isDemoNpc = 1`.

**Браузер.** На сервере нет дисплея, поэтому запускай Chrome только headless: `MT_HEADLESS=1` и `MT_CHROME=/root/.cache/puppeteer/chrome/linux-150.0.7871.115/chrome-linux64/chrome` (если версия сменилась - посмотри `ls /root/.cache/puppeteer/chrome/`). Скриншоты в headless снимаются нормально; в доке есть оговорка, что headless рендерит чуть иначе - если это влияет на вывод, отметь в отчёте.

## Как проводить прогон

Метод подробно описан в `backend/docs/manual-testing.md`. Главное:

- **Доказывай изменение состояния, а не внешний вид.** Прочитал состояние, подействовал, прочитал снова, сравнил. Скриншот подтверждает историю, но редко является доказательством сам по себе
- **Сначала API-слой, браузер только там, где нужен человеческий взгляд** или где поведение вообще не доходит до нашего сервера. API-кейс отрабатывает за секунду, браузерный за минуты
- **У каждого основного кейса должна быть контрольная группа.** «Подавляется для админа» ничего не значит, пока та же фикстура и те же клики в обычной сессии не дают противоположный результат
- **Упавший кейс - это твоя фикстура, пока не доказано обратное.** Если основной кейс и его контрольная группа падают ОДИНАКОВО, дело почти наверняка в фикстуре, а не в продукте
- **Оформи прогон блоками с фильтром `ONLY=`**, чтобы перезапускать упавший кейс, не гоняя те, что двигают деньги или создают счета у провайдера
- **Сними снимок всего, что трогаешь, восстанови и ПРОВЕРЬ запросом.** Восстанавливай все поля, которые менял, а не только то, о чём думал

## Хранение артефактов

- Всё - скриншоты, сырые JSON, скрипты, логи - в папку `/root/projects/humanprogram/.mt-evidence/<дата>-<тема>/`, вне `/tmp` и вне сессионного scratchpad. Передай её обоим харнессам через `MT_EVIDENCE`, тогда бэкендная и фронтовая половины соберутся в один отчёт
- Результат каждого кейса пиши в файл сразу после прогона, а не в конце. Харнесс уже так делает
- Перед новым прогоном удали отчёты старше недели, если такие лежат рядом
- После сдачи отчёта удали рабочую папку целиком. Долговременная копия - это опубликованный артефакт

## Отчёт

- **На русском языке**, самодостаточный (скриншоты внутри файла)
- Собери его из файлов кейсов через `backend/test/manual/report/build.js`, чтобы его можно было пересобрать, ничего не перепроверяя
- Русский язык задаётся в конфиге прогона, а не в репозитории: в репозиториях всё по-английски, потому что команда интернациональная. Передай в meta-конфиг блок `labels` с русскими подписями (пример есть в `backend/docs/manual-testing.md`), а тексты самих кейсов пиши по-русски прямо в конфиге. Ничего переводить в коде не надо
- Сначала summary: сколько основных кейсов, сколько регрессий, сколько edge-cases, сколько упало
- Дальше по каждому кейсу: что проверяли, что получили с конкретными числами (строки в базе до и после, HTTP-статус, разобранный клейм), и там, где это видно в UI - скриншот с коротким объяснением, что на нём и почему это доказывает, что кейс пройден
- **Отдельным блоком: что осталось непроверенным и почему.** Что подменялось в базе вместо ожидания по времени, что проверено только чтением кода, какие внешние настройки надо подтвердить на проде перед выкаткой
- **Опубликуй отчёт артефактом (инструмент Artifact) сразу, как он готов, и ОБЯЗАТЕЛЬНО вставь ссылку на артефакт в TL;DR-блок финального сообщения** - в Telegram приходит только короткая версия, и ссылка должна быть в ней. Если артефакт опубликовать не удалось - скажи об этом прямо и приложи путь к HTML-отчёту

Если по ходу выяснится, что какой-то устойчивый факт про среду или про приложение не описан в доках репозитория, а ты потратил на него цикл отладки - скажи мне об этом в конце. Такие вещи надо дописывать в доки, чтобы следующий прогон был быстрее."""


# Prepended to the PR-fixer body to make the all-in-one flow. ``<PR>`` in the
# fixer body is the number ``gh pr create`` returns in STEP 2.
_FULL_FLOW_PREAMBLE = """\
Нужно довести текущие изменения до зеленого, готового к merge pull request. Выполни все шаги ниже по порядку; останавливайся только если что-то действительно неоднозначно или рискованно (и тогда спроси меня).

STEP 1 — Feature-ветка, commit и push:
- Посмотри, что реально изменилось (git status + git diff). Создай НОВУЮ ветку от текущей с понятным именем в стиле этого репозитория (например `feature/<короткое-описание-через-дефис>`).
- Добавляй в staging только файлы, относящиеся к этому изменению — НЕ делай слепой `git add -A`; всё постороннее (артефакты сборки, локальные конфиги, файлы редактора) оставь незастейдженным и скажи мне о таких файлах.
- Сделай commit с понятным сообщением, описывающим СУТЬ изменения, по конвенциям этого проекта. НЕ добавляй трейлер `Co-Authored-By` и НЕ упоминай Claude, AI или этого ассистента нигде.
- Запушь ветку и установи upstream (`git push -u origin <branch>`).

STEP 2 — Открой pull request:
- Создай PR через `gh pr create` против ветки по умолчанию репозитория (для humanprogram backend это `develop`; проверь через `git remote show origin`). Понятный заголовок (суть изменения) и краткое описание (что изменилось, зачем и как проверить). НЕ упоминай Claude или AI в PR.
- Запомни НОМЕР PR, который вернёт `gh pr create` — во всех командах ниже `<PR>` — это он.

STEP 3 — Прогоняй PR auto-fixer по только что открытому PR, пока все проверки не станут зелёными и PR не будет готов к merge:

"""


def _full_flow_prompt(repo: str) -> str:
    """Branch → commit → push → open PR → drive the PR auto-fixer (humanprogram)."""
    fixer = _PR_FIXER_TEMPLATE.replace("__REPO__", repo).replace("__PR__", "<PR>")
    return _FULL_FLOW_PREAMBLE + fixer


def _targets_listing(targets: list[tuple[str, str, str]]) -> str:
    """``- <role>: <path> — REPO=<env>, PR #<n>`` per repo (``<n>`` may be ``<PR>``)."""
    return "\n".join(
        f"- {_REPO_ROLE.get(repo, repo)}: {path} — REPO={repo}, PR #{pr}"
        for path, repo, pr in targets
    )


def _pr_fixer_prompt_multi(targets: list[tuple[str, str, str]]) -> str:
    """PR auto-fixer across several repos: ``targets`` = [(path, REPO env, pr)]."""
    generic = _PR_FIXER_TEMPLATE.replace("__REPO__", "<REPO репозитория>").replace(
        "__PR__", "<PR репозитория>"
    )
    return (
        "Это full-stack сессия: нужно довести до merge PR в НЕСКОЛЬКИХ "
        "репозиториях одновременно:\n"
        f"{_targets_listing(targets)}\n\n"
        "Прогони цикл ниже для КАЖДОГО из них: команды скрипта выполняй из "
        "корня соответствующего репозитория, подставляя его значения REPO и "
        "номера PR вместо плейсхолдеров. Все перечисленные PR должны стать "
        "зелёными; в заголовках прогресса указывай, о каком репозитории речь.\n\n"
        + generic
    )


def _full_flow_prompt_multi(repos: list[tuple[str, str]]) -> str:
    """Full flow across several repos: ``repos`` = [(path, REPO env)]."""
    targets = [(path, repo, "<PR>") for path, repo in repos]
    listing = "\n".join(
        f"- {_REPO_ROLE.get(repo, repo)}: {path} (REPO={repo})" for path, repo in repos
    )
    generic = _PR_FIXER_TEMPLATE.replace("__REPO__", "<REPO репозитория>").replace(
        "__PR__", "<PR>"
    )
    return (
        "Это full-stack сессия: изменения могут быть в НЕСКОЛЬКИХ репозиториях:\n"
        f"{listing}\n\n"
        + _FULL_FLOW_PREAMBLE.replace(
            "STEP 1 — Feature-ветка, commit и push:",
            "STEP 1 — Feature-ветка, commit и push (в КАЖДОМ репозитории, где есть "
            "изменения; ветки называй одинаково, чтобы их было легко связать):",
        )
        .replace(
            "STEP 2 — Открой pull request:",
            "STEP 2 — Открой pull request в КАЖДОМ репозитории с изменениями (у "
            "каждого своя ветка по умолчанию: backend → `develop`, frontend → `main`; "
            "проверь через `git remote show origin`). В описании каждого PR сошлись "
            "на парный PR другого репозитория:",
        )
        .replace(
            "STEP 3 — Прогоняй PR auto-fixer по только что открытому PR, пока все "
            "проверки не станут зелёными и PR не будет готов к merge:",
            "STEP 3 — Прогоняй PR auto-fixer по КАЖДОМУ открытому PR (из корня "
            "соответствующего репозитория, с его REPO и номером), пока все проверки "
            "не станут зелёными и оба PR не будут готовы к merge:",
        )
        + f"Целевые репозитории:\n{_targets_listing(targets)}\n\n"
        + generic
    )


# ── callback codec (window_id is the trailing, colon-safe field) ───────────────


def _encode(action: str, window_id: str) -> str:
    return f"{_CB_PREFIX}{action}:{window_id}"


def _decode(data: str) -> tuple[str, str] | None:
    """Return (action, window_id) or None. window_id may contain ``:``."""
    if not data.startswith(_CB_PREFIX):
        return None
    action, _, window_id = data[len(_CB_PREFIX) :].partition(":")
    if (
        action not in ("menu", "sr", "cp", "sm", "fb", "fa", "pr", "mt", "x")
        or not window_id
    ):
        return None
    return action, window_id


def scenarios_button_for_window(window_id: str) -> Any:
    """The 🎬 action-row button that opens the scenarios menu."""
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton

    return InlineKeyboardButton("🎬", callback_data=_encode("menu", window_id))


# ── repo / eligibility ─────────────────────────────────────────────────────────


async def _run_git(repo_path: str, *args: str) -> str | None:
    """Run ``git -C <repo_path> <args>`` and return trimmed stdout, or None.

    Bounded by a 5s timeout (a wedged git must never hang a menu open).
    """
    # Lazy: only needed on this path.
    import contextlib

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            repo_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError, ValueError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except TimeoutError, OSError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace").strip() or None


async def _git_remote_url(repo_path: str) -> str | None:
    return await _run_git(repo_path, "remote", "get-url", "origin")


def _session_repo_paths(window_id: str) -> list[str]:
    """Git repos the session spans: the composite list from the sidecar, else
    the single resolved working directory (workspace-aware)."""
    sidecar = state.load(window_id)
    if sidecar is not None and sidecar.project_repos:
        return list(sidecar.project_repos)
    repo_path = state.resolve_repo(window_id)
    return [repo_path] if repo_path else []


# A session spanning this many repos (or more) is a full-stack session.
_COMPOSITE_MIN_REPOS = 2


def _is_composite(window_id: str) -> bool:
    return len(_session_repo_paths(window_id)) >= _COMPOSITE_MIN_REPOS


async def _is_git_repo(window_id: str) -> bool:
    """True when EVERY directory the session spans is inside a git work tree."""
    paths = _session_repo_paths(window_id)
    if not paths:
        return False
    for repo_path in paths:
        if await _run_git(repo_path, "rev-parse", "--is-inside-work-tree") != "true":
            return False
    return True


async def _detect_pr_repos(window_id: str) -> list[tuple[str, str]]:
    """``[(repo_path, REPO env)]`` for every session repo that is a known
    humanprogram repository (keyed off the git ``origin`` remote, so worktrees
    and clones qualify too). Empty when none qualifies."""
    targets: list[tuple[str, str]] = []
    for repo_path in _session_repo_paths(window_id):
        url = await _git_remote_url(repo_path)
        if not url:
            continue
        for needle, repo in _REPO_BY_REMOTE:
            if needle in url:
                targets.append((repo_path, repo))
                break
    return targets


async def _detect_pr_repo(window_id: str) -> str | None:
    """The first eligible ``REPO`` env value, or None — the menu-gating check."""
    targets = await _detect_pr_repos(window_id)
    return targets[0][1] if targets else None


_REPO_ROLE = {"backend": "backend", "frontend": "frontend"}


def _scope_preamble(window_id: str, *, ru: bool) -> str:
    """For full-stack sessions, a lead-in naming every repo the task spans.

    Empty for ordinary single-repo sessions, so their prompts are unchanged.
    """
    paths = _session_repo_paths(window_id)
    if len(paths) < _COMPOSITE_MIN_REPOS:
        return ""
    listing = "\n".join(f"- {p}" for p in paths)
    if ru:
        return (
            "Это full-stack сессия: она охватывает НЕСКОЛЬКО репозиториев:\n"
            f"{listing}\n"
            "Выполни всё, что описано ниже, для КАЖДОГО репозитория, в котором "
            "есть изменения (git-команды запускай из корня соответствующего "
            "репозитория или через `git -C <path>`; у каждого репозитория своя "
            "ветка по умолчанию).\n\n"
        )
    return (
        "This is a full-stack session spanning SEVERAL repositories:\n"
        f"{listing}\n"
        "Apply everything below to EACH repository that has changes (run git "
        "from the repo's root or via `git -C <path>`; each repo has its own "
        "default branch).\n\n"
    )


# ── shared forward ─────────────────────────────────────────────────────────────


async def _forward_scenario(
    *,
    window_id: str,
    user_id: int,
    thread_id: int,
    prompt: str,
    anchor: Any,
    bot: Any,
) -> None:
    """Forward *prompt* to the agent as a normal turn, then start the bubble.

    Routes through the ORIGINAL (pre-batch-wrap) forward captured by
    :mod:`input_pipeline.intercept`, so a scenario fires immediately as a turn
    instead of being appended to the batch.
    """
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.telegram_client import PTBTelegramClient

    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .input_pipeline import intercept

    original = intercept._ORIGINAL_FORWARD_MESSAGE
    if original is None:
        logger.warning("scenario forward skipped — original forward not wired")
        return
    client = PTBTelegramClient(bot)
    try:
        await original(window_id, user_id, thread_id, prompt, client, anchor)
    except Exception:  # noqa: BLE001 -- never let a scenario crash the handler
        logger.exception("scenario forward failed for %s", window_id)
        return

    # Live "⚙️ Working on your request…" bubble (mirrors the batch-flush path).
    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .output_pipeline import progress_bubble

    fallback_chat_id = getattr(getattr(anchor, "chat", None), "id", None)
    await progress_bubble.begin_for_turn(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        bot=bot,
        fallback_chat_id=fallback_chat_id,
    )


async def _edit_to_note(message: Any, text: str) -> None:
    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    with contextlib.suppress(TelegramError):
        await message.edit_text(text=text, reply_markup=None)


# ── callbacks ───────────────────────────────────────────────────────────────────


async def handle_scenarios_callback(update: Any, context: Any) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    try:
        await _dispatch(update, context)
    except Exception:  # noqa: BLE001 -- log, then stop the handler chain below
        logger.exception("scenarios callback failed")
    finally:
        raise ApplicationHandlerStop


async def _dispatch(update: Any, context: Any) -> None:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.callback_helpers import get_thread_id, user_owns_window

    query = update.callback_query
    if query is None or not query.data:
        return
    decoded = _decode(query.data)
    if decoded is None:
        await query.answer("Invalid", show_alert=True)
        return
    action, window_id = decoded

    user = update.effective_user
    user_id = user.id if user else 0
    if not user_owns_window(user_id, window_id):
        await query.answer("Not your session", show_alert=True)
        return

    if action == "x":
        await _cancel(query, context)
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await query.answer("No topic context", show_alert=True)
        return
    if query.message is None:
        await query.answer("This card expired — reopen the menu.", show_alert=True)
        return

    await _route_topic_action(query, action, window_id, user_id, thread_id, context)


async def _route_topic_action(
    query: Any,
    action: str,
    window_id: str,
    user_id: int,
    thread_id: int,
    context: Any,
) -> None:
    """Dispatch a validated, topic-scoped scenario action to its handler."""
    if action == "menu":
        await _open_menu(query, window_id)
    elif action == "sr":
        await _run_self_review(query, window_id, user_id, thread_id, context)
    elif action == "cp":
        await _run_commit_push(query, window_id, user_id, thread_id, context)
    elif action == "sm":
        await _run_sync_main(query, window_id, user_id, thread_id, context)
    elif action == "fb":
        await _run_feature_branch(query, window_id, user_id, thread_id, context)
    elif action == "fa":
        await _run_full_flow(query, window_id, user_id, thread_id, context)
    elif action == "pr":
        await _ask_pr_number(query, window_id, user_id, thread_id, context)
    elif action == "mt":
        await _run_manual_testing(query, window_id, user_id, thread_id, context)


async def _open_menu(query: Any, window_id: str) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    rows = [
        [InlineKeyboardButton("🔎 Self-review", callback_data=_encode("sr", window_id))]
    ]
    if await _is_git_repo(window_id):
        rows.append(
            [
                InlineKeyboardButton(
                    "💾 Commit & push", callback_data=_encode("cp", window_id)
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "🌿 Feature branch + push",
                    callback_data=_encode("fb", window_id),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 Sync main branch", callback_data=_encode("sm", window_id)
                )
            ]
        )
    if await _detect_pr_repo(window_id) is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    "🤖 PR auto-fixer", callback_data=_encode("pr", window_id)
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "🚀 Branch → PR → auto-fix",
                    callback_data=_encode("fa", window_id),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "🧪 Manual testing", callback_data=_encode("mt", window_id)
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("✖ Cancel", callback_data=_encode("x", window_id))]
    )
    await query.answer()
    msg = query.message
    if msg is None:
        return
    thread_id = getattr(msg, "message_thread_id", None)
    with contextlib.suppress(TelegramError):
        await msg.reply_text(
            text="🎬 Scenarios — pick one to run:",
            reply_markup=InlineKeyboardMarkup(rows),
            message_thread_id=thread_id,
        )


async def _run_self_review(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    note = (
        "🔎 Scenario triggered: Self-review\n"
        "Deep self-review of the last unpushed changes — fixing any issues found."
    )
    await _edit_to_note(query.message, note)
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=_scope_preamble(window_id, ru=True) + _SELF_REVIEW_PROMPT,
        anchor=query.message,
        bot=context.bot,
    )
    await query.answer("Self-review started")


async def _run_commit_push(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    note = (
        "💾 Scenario triggered: Commit & push\n"
        "Claude is reviewing the changes, writing a commit message, and pushing."
    )
    await _edit_to_note(query.message, note)
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=_scope_preamble(window_id, ru=False) + _COMMIT_PUSH_PROMPT,
        anchor=query.message,
        bot=context.bot,
    )
    await query.answer("Commit & push started")


async def _run_sync_main(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    note = (
        "🔄 Scenario triggered: Sync main branch\n"
        "Switching to the repo's default branch (develop/main) and pulling the "
        "latest — will stop and ask if there are uncommitted changes."
    )
    await _edit_to_note(query.message, note)
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=_scope_preamble(window_id, ru=False) + _SYNC_MAIN_PROMPT,
        anchor=query.message,
        bot=context.bot,
    )
    await query.answer("Sync started")


async def _run_feature_branch(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    note = (
        "🌿 Scenario triggered: Feature branch + push\n"
        "Creating a feature branch, committing the changes, and pushing it."
    )
    await _edit_to_note(query.message, note)
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=_scope_preamble(window_id, ru=False) + _FEATURE_BRANCH_PROMPT,
        anchor=query.message,
        bot=context.bot,
    )
    await query.answer("Feature branch + push started")


async def _run_full_flow(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    targets = await _detect_pr_repos(window_id)
    if not targets:
        await query.answer("Not a humanprogram backend/app repo", show_alert=True)
        return
    note = (
        "🚀 Scenario triggered: Branch → PR → auto-fix\n"
        "Feature branch + commit + push, opening a PR, then driving the PR "
        "auto-fixer until all checks are green."
    )
    await _edit_to_note(query.message, note)
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=(
            _full_flow_prompt_multi(targets)
            if len(targets) > 1
            else _full_flow_prompt(targets[0][1])
        ),
        anchor=query.message,
        bot=context.bot,
    )
    await query.answer("Full flow started")


async def _run_manual_testing(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    repo = await _detect_pr_repo(window_id)
    if repo is None:
        await query.answer("Not a humanprogram backend/app repo", show_alert=True)
        return
    note = (
        "🧪 Scenario triggered: Manual testing\n"
        "Preflight of the local VPS environment, API + headless-browser run "
        "with evidence, then a Russian HTML report published as an artifact."
    )
    await _edit_to_note(query.message, note)
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=_scope_preamble(window_id, ru=True) + _MANUAL_TESTING_PROMPT,
        anchor=query.message,
        bot=context.bot,
    )
    await query.answer("Manual testing started")


async def _ask_pr_number(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    targets = await _detect_pr_repos(window_id)
    if not targets:
        await query.answer("Not a humanprogram backend/app repo", show_alert=True)
        return
    repo = targets[0][1]
    msg = query.message
    if msg is None:
        return
    if context.user_data is not None:
        context.user_data[AWAITING_PR_NUMBER] = {
            "chat_id": msg.chat.id,
            "thread_id": thread_id,
            "window_id": window_id,
            "repo": repo,
            "targets": [list(target) for target in targets],
            "prompt_msg_id": msg.message_id,
            "user_id": user_id,
        }
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖ Cancel", callback_data=_encode("x", window_id))]]
    )
    if len(targets) > 1:
        roles = " ".join(_REPO_ROLE.get(r, r) for _p, r in targets)
        text = (
            f"🤖 PR auto-fixer (full-stack: {roles}) — reply with the PR numbers "
            f"in that order, e.g. `1234 567`; use `-` for a repo without a PR."
        )
    else:
        text = f"🤖 PR auto-fixer ({repo}) — reply with the PR number (e.g. 1234)."
    with contextlib.suppress(TelegramError):
        await msg.edit_text(text=text, reply_markup=keyboard)
    await query.answer("Send the PR number")


async def _cancel(query: Any, context: Any) -> None:
    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    if context.user_data is not None:
        context.user_data.pop(AWAITING_PR_NUMBER, None)
    if query.message is not None:
        with contextlib.suppress(TelegramError):
            await query.message.delete()
    await query.answer("Cancelled")


async def consume_pr_number_reply(update: Any, context: Any) -> None:
    """Group −12 text handler: consume a PR number when the PR flow is armed.

    Pure pass-through (returns without stopping) when no PR number is awaited,
    so normal messages reach ccgram's text handler untouched.
    """
    pend = context.user_data.get(AWAITING_PR_NUMBER) if context.user_data else None
    if not pend:
        return
    message = update.message
    if message is None or not (message.text and message.text.strip()):
        return

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.callback_helpers import get_thread_id

    if pend.get("thread_id", 0) != (get_thread_id(update) or 0):
        return  # armed in a different topic — leave it, pass through

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    bot = context.bot
    targets = [tuple(target) for target in pend.get("targets") or []]
    parsed = _parse_pr_reply(message.text, len(targets) if len(targets) > 1 else 1)
    if parsed is None:
        # Invalid — re-prompt, keep the flow armed, drop the stray reply.
        await _reprompt_invalid(bot, pend)
        with contextlib.suppress(TelegramError):
            await message.delete()
        raise ApplicationHandlerStop

    repo = pend["repo"]
    window_id = pend["window_id"]
    user_id = pend.get("user_id", 0)
    thread_id = pend["thread_id"]
    if context.user_data is not None:
        context.user_data.pop(AWAITING_PR_NUMBER, None)

    if len(targets) > 1:
        chosen = [
            (path, env, pr)
            for (path, env), pr in zip(targets, parsed, strict=True)
            if pr is not None
        ]
        summary = ", ".join(f"#{pr} ({env})" for _p, env, pr in chosen)
        prompt = _pr_fixer_prompt_multi(chosen)
    else:
        pr = parsed[0] or ""
        summary = f"#{pr} ({repo})"
        prompt = _pr_fixer_prompt(pr, repo)

    note = (
        "🤖 Scenario triggered: PR auto-fixer\n"
        f"Driving PR {summary} to green — addressing checks & Cursor "
        "feedback (≤20 iterations)."
    )
    with contextlib.suppress(TelegramError):
        await bot.edit_message_text(
            chat_id=pend["chat_id"], message_id=pend["prompt_msg_id"], text=note
        )
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=prompt,
        anchor=message,
        bot=bot,
    )
    # Keep the chat clean — drop the user's bare-number message.
    with contextlib.suppress(TelegramError):
        await message.delete()
    raise ApplicationHandlerStop


def _parse_pr_reply(text: str, expected: int) -> list[str | None] | None:
    """Parse the PR-number reply.

    Single repo: one number (a leading ``#`` is tolerated). Full-stack: one
    token per repo in order, each a number or ``-`` (no PR for that repo), with
    at least one real number. ``None`` when the reply doesn't fit.
    """
    tokens = [tok.lstrip("#") for tok in text.split()]
    if expected <= 1:
        if len(tokens) != 1 or not tokens[0].isdigit():
            return None
        return [tokens[0]]
    if len(tokens) != expected:
        return None
    parsed: list[str | None] = []
    for tok in tokens:
        if tok == "-":
            parsed.append(None)
        elif tok.isdigit():
            parsed.append(tok)
        else:
            return None
    return parsed if any(p is not None for p in parsed) else None


async def _reprompt_invalid(bot: Any, pend: dict[str, Any]) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✖ Cancel", callback_data=_encode("x", pend["window_id"])
                )
            ]
        ]
    )
    with contextlib.suppress(TelegramError):
        await bot.edit_message_text(
            chat_id=pend["chat_id"],
            message_id=pend["prompt_msg_id"],
            text=(
                "🤖 PR auto-fixer — that doesn't look like PR numbers. Reply with "
                "one number per repo in order (e.g. `1234 567`, `-` for none)."
                if len(pend.get("targets") or []) > 1
                else "🤖 PR auto-fixer — that doesn't look like a PR number. "
                "Reply with just the number, e.g. 1234."
            ),
            reply_markup=keyboard,
        )


# ── install ─────────────────────────────────────────────────────────────────────


def install_scenarios(application: Any) -> None:
    """Register the scenarios callback + PR-number text handler on *application*."""
    global _installed
    if _installed:
        return
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    # group=-10: run before ccgram's catch-all CallbackQueryHandler (group 0),
    # alongside the layer's other -10 handlers (each pattern-gated).
    application.add_handler(
        CallbackQueryHandler(handle_scenarios_callback, pattern=r"^ccgrampro:scn:"),
        group=-10,
    )
    # group=-12: consume a PR-number reply before the voice-edit (-11) and core
    # text (0) handlers. No-op pass-through when no PR number is awaited.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, consume_pr_number_reply),
        group=-12,
    )
    _installed = True
    logger.info(
        "ccgram-pro scenarios installed — self-review + commit/push + sync-main "
        "+ PR auto-fixer + manual testing"
    )


def _reset_for_testing() -> None:
    global _installed
    _installed = False
