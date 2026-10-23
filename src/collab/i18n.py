from __future__ import annotations
import hashlib
import unicodedata
from typing import Any

# The room's own words in each language it speaks: one catalog, a key for each text, the same keys in every
# language, a parameter by its name — never a sentence put together from translated pieces. Names, ids, paths,
# models and commands to run go in as parameters, as they are. A number takes its form by the language's rule
# (one / few / many in Russian). A key missing in one language is a test's failure, not a quiet English line.
LANGS = ("en", "ru")
LANG_NAMES = {"en": "English", "ru": "Русский"}

CATALOG: dict[str, dict[str, Any]] = {
    # The first run: both languages at once, before anything else.
    "lang.choose": {"en": "Выберите язык / Choose language", "ru": "Выберите язык / Choose language"},
    "lang.room": {"en": "Room language: {language}", "ru": "Язык комнаты: {language}"},
    "lang.change.question": {
        "en": "Change this room's language to {language}?",
        "ru": "Язык комнаты: {language}. Применить?",
    },
    "lang.change.apply": {"en": "Apply", "ru": "Применить"},
    "lang.change.cancel": {"en": "Cancel", "ru": "Отмена"},
    "lang.change.also_new": {"en": "Also for new rooms", "ru": "Использовать и для новых комнат"},
    "lang.changed": {
        "en": "The room speaks English now. Agents get it with their next assignment.",
        "ru": "Язык комнаты изменён на русский. Агенты получат указание отвечать по-русски со следующей вводной.",
    },
    "lang.unchanged": {"en": "Language unchanged.", "ru": "Язык не изменён."},
    # Counts.
    "n.agents": {
        "en": {"one": "{n} agent", "other": "{n} agents"},
        "ru": {"one": "{n} агент", "few": "{n} агента", "many": "{n} агентов"},
    },
    # The chat's command menu.
    "cmd.add": {"en": "start a new agent", "ru": "запустить нового агента"},
    "cmd.remove": {"en": "disconnect a participant", "ru": "отключить участника от комнаты"},
    "cmd.mute": {
        "en": "room-wide messages stop; by name still arrive",
        "ru": "общие сообщения не доставляются; по имени — доходят",
    },
    "cmd.unmute": {"en": "deliver to an agent again", "ru": "снова доставлять агенту общие сообщения"},
    "cmd.unlimit": {
        "en": "an agent is back from its usage limit: deliver to it again",
        "ru": "возобновить доставку агенту после окончания лимита",
    },
    "cmd.name": {"en": "rename yourself or an agent", "ru": "переименовать себя или агента"},
    "cmd.view": {"en": "see an agent's screen (read-only)", "ru": "посмотреть экран агента (только чтение)"},
    "cmd.answer": {"en": "respond in an agent terminal over the chat", "ru": "ответить в терминале агента поверх чата"},
    "cmd.open": {"en": "open an agent's window in a new tab", "ru": "открыть окно агента в новой вкладке"},
    "cmd.pause": {"en": "pause the room: agents get no messages", "ru": "приостановить доставку сообщений агентам"},
    "cmd.resume": {"en": "resume the room: waiting messages go out", "ru": "возобновить доставку ожидающих сообщений"},
    "cmd.clear": {"en": "clear the history (moved to an archive)", "ru": "очистить историю комнаты (переносится в архив)"},
    "cmd.help": {"en": "what would you like to do? — tasks and the guide", "ru": "что вы хотите сделать? — задачи и справка"},
    "cmd.mine": {
        "en": "what is for you: unread messages, agents waiting for you",
        "ru": "сообщения и вопросы к вам",
    },
    "cmd.dm": {
        "en": "talk privately with an agent in a panel beside the chat",
        "ru": "личный разговор с агентом в панели рядом с чатом",
    },
    "cmd.restart": {"en": "restart an agent, its conversation continues", "ru": "перезапустить агента, разговор продолжится"},
    "cmd.team": {"en": "the team: see it, or start a ready one", "ru": "команда: посмотреть, изменить или запустить готовую"},
    "cmd.stop": {
        "en": "stop the room: close its windows and agents",
        "ru": "остановить комнату: закрыть её окна и остановить запущенных ею агентов",
    },
    "cmd.exit": {"en": "close this chat panel", "ru": "закрыть этот чат, агенты продолжат работу"},
    "cmd.language": {"en": "the room's language", "ru": "язык комнаты"},
    # The scenarios' picker and preview.
    "team.what": {"en": "What will we do?", "ru": "Что будем делать?"},
    "team.none": {
        "en": "No template — nothing automatic, add agents with /add",
        "ru": "Без шаблона — добавьте агентов через /добавить",
    },
    "team.none.said": {
        "en": "No template: nothing automatic — /add adds an agent, /team picks a ready team later.",
        "ru": "Без шаблона: добавьте агента через /добавить или позже выберите готовую команду через /команда.",
    },
    "team.preview.head": {
        "en": "{title} — {hint}. This is a starting team: change it with /team once it runs.",
        "ru": "{title} — {hint}. Это начальный состав: после запуска его можно изменить через /команда.",
    },
    "team.preview.result": {
        "en": "Result: {output}. Without you: nothing external.",
        "ru": "Итог: {output}. Без вас: ничего внешнего.",
    },
    "team.result.chat": {"en": "in the chat", "ru": "в чате"},
    "team.launch.question": {"en": "Launch this team?", "ru": "Запустить эту команду?"},
    "team.launch": {"en": "Launch the team — {agents}", "ru": "Запустить команду — {agents}"},
    "team.back": {"en": "Back", "ru": "Назад"},
    "team.unavailable": {"en": "Not available: {what}", "ru": "Недоступно: {what}"},
    "team.has_one": {
        "en": "This room has a team already: /team shows it.",
        "ru": "В этой комнате уже есть команда. Посмотреть состав: /команда.",
    },
    "team.starting": {"en": "Starting the {title} team…", "ru": "Запускаю команду «{title}»…"},
    "team.view.head": {
        "en": "Team: {title} · result: {output} · without you: {without}",
        "ru": "Команда: {title} · итог: {output} · без вас: {without}",
    },
    "team.view.none": {"en": "No scenario", "ru": "Без сценария"},
    "team.view.nothing": {"en": "nothing external", "ru": "ничего внешнего"},
    "team.view.empty": {
        "en": "No agents yet: /team picks a ready team, /add adds one agent.",
        "ru": "Агентов пока нет. Выберите готовую команду через /команда или добавьте агента через /добавить.",
    },
    "team.started": {"en": "Team started: {started} of {total}.", "ru": "Команда запущена: {started} из {total}."},
    "team.started.lead": {"en": " Lead — {lead}.", "ru": " Ведущий — {lead}."},
    "team.started.nolead": {
        "en": " No lead started: /add one, or start the team again.",
        "ru": " Ведущий не запустился: добавьте его через /добавить или запустите команду снова.",
    },
    "state.starting": {"en": "Starting", "ru": "Запускается"},
    "state.getting": {"en": "Getting its assignment", "ru": "Получает назначение"},
    "state.acknowledged": {"en": "Assignment acknowledged", "ru": "Назначение подтверждено"},
    "state.replied": {"en": "Replied to its assignment — read it", "ru": "Агент ответил на вводную — прочитайте ответ"},
    # The guide (/help): each line by its English words' key.
    "help.cc3cb7ce9b": {"en": "Choose a recipient", "ru": "Выбрать получателя"},
    "help.f7ec8b2413": {
        "en": "Open the command menu; ↑/↓ to choose, Enter to select",
        "ru": "Открыть меню команд; ↑/↓ — выбор, Enter — выполнить",
    },
    "help.4b0fd81d82": {"en": "Go back or close the menu", "ru": "Назад или закрыть меню"},
    "help.096c43bb67": {
        "en": "Send a message; multiline pastes stay in one message",
        "ru": "Отправить сообщение; вставка из нескольких строк — одним сообщением",
    },
    "help.2c4259714f": {
        "en": "Earlier messages, one at a time; Esc — back to the newest",
        "ru": "Прежние сообщения по одному; Esc — к последним",
    },
    "help.edda33e098": {
        "en": "An agent needs you — /answer opens its terminal",
        "ru": "Агент ждёт вашего действия — /ответить откроет его терминал",
    },
    "help.ad15d1c0f8": {"en": "Rename yourself or an agent", "ru": "Переименовать себя или агента"},
    "help.105f29f801": {"en": "Add an agent · disconnect a participant", "ru": "Добавить агента · отключить участника"},
    "help.ccdbc7ccb5": {
        "en": "See an agent's screen · open its window in a new tab",
        "ru": "Экран агента · его окно в новой вкладке",
    },
    "help.a1ac7cf483": {"en": "Show this guide", "ru": "Показать эту справку"},
    "help.e9137ff5be": {"en": "Choose a recipient; shown by the input", "ru": "Выбрать получателя; он показан у строки ввода"},
    "help.c58fb8dff4": {
        "en": "New line inside a message (or end the line with \\)",
        "ru": "Новая строка внутри сообщения (или \\ в конце строки)",
    },
    "help.86018ecc92": {
        "en": "Clear the whole draft, even a long pasted one",
        "ru": "Стереть весь черновик, даже длинную вставку",
    },
    "help.b33880db0e": {
        "en": "Earlier messages: one at a time, or a page; Esc — back to the newest",
        "ru": "Прежние сообщения: по одному или страницей; Esc — к последним",
    },
    "help.0bc491285d": {
        "en": "Send to someone without changing the chosen recipient",
        "ru": "Написать кому-то, не меняя выбранного получателя",
    },
    "help.d7eee0ba1f": {
        "en": "An agent needs your input — use /answer, then Ctrl+] to return",
        "ru": "Агенту нужен ваш ответ — /ответить, затем Ctrl+] назад",
    },
    "help.c1ffaa1441": {
        "en": "Green: ready · Yellow: working · Orange: muted or limit · Red !: act in its window",
        "ru": "Зелёный: свободен · Жёлтый: работает · Оранжевый: заглушён или лимит · Красный !: нужен ответ в его окне",
    },
    "help.50af86a220": {
        "en": "× muted · ◷ limit reached · ? no recent data · … connecting",
        "ru": "× заглушён · ◷ лимит · ? нет свежих данных · … подключается",
    },
    "help.85d38c9be9": {
        "en": "Green with a spinner and a number: ready for messages, background tasks still run",
        "ru": "Зелёный с вращающимся значком и числом: принимает сообщения, фоновые задачи ещё выполняются",
    },
    "help.9dfa9c133f": {
        "en": "More agents wrap to another row; none are hidden",
        "ru": "Если агенты не помещаются, их статусы переносятся на следующую строку; никто не скрыт",
    },
    "help.6f3283e0e3": {
        "en": "Start a new agent: choose its kind, its folder, its name",
        "ru": "Запустить нового агента: программа, папка, имя",
    },
    "help.d40ae8935e": {
        "en": "See the team and where each agent is with its assignment; in a new room, start a ready team",
        "ru": "Состав и состояние назначения каждого агента; в новой комнате — запустить готовую команду",
    },
    "help.e8644cfcd3": {
        "en": "Disconnect a participant; the agent keeps running in its window",
        "ru": "Отключить участника; агент продолжает работать в своём окне",
    },
    "help.04405a24f7": {
        "en": "Stop / resume room-wide messages to an agent; messages to it by name still arrive",
        "ru": "Остановить / возобновить общие сообщения агенту; сообщения по имени доходят",
    },
    "help.b474bc9da6": {
        "en": "An agent is back from its usage limit: the room delivers to it again",
        "ru": "Возобновить доставку агенту после окончания лимита",
    },
    "help.abb8b22aa6": {
        "en": "Rename yourself or an agent: choose whom, type the new name",
        "ru": "Переименовать себя или агента: выбрать кого, ввести новое имя",
    },
    "help.33f4ba4960": {
        "en": "See an agent's screen, read-only; Esc — back to chat",
        "ru": "Экран агента, только чтение; Esc — назад в чат",
    },
    "help.344cb72375": {
        "en": "Respond in an agent terminal over the chat; Ctrl+] returns",
        "ru": "Ответить в терминале агента поверх чата; Ctrl+] — назад",
    },
    "help.26a7880074": {
        "en": "Open an agent's window in a new terminal tab, to answer it",
        "ru": "Открыть окно агента в новой вкладке терминала, чтобы ответить ему",
    },
    "help.09e3feee8b": {
        "en": "Restart an agent the room started; its conversation continues (new rules too)",
        "ru": "Перезапустить агента комнаты; разговор продолжится (и с новыми правилами)",
    },
    "help.91954ad89f": {
        "en": "What is for you: agents waiting for you, then unread messages to you and answers to yours",
        "ru": "Агенты, которым нужно ваше действие, затем непрочитанные сообщения и ответы вам",
    },
    "help.8520174043": {
        "en": "Talk with one agent in a panel beside the chat: Ctrl+O there and back, Tab tabs in it, Ctrl+] close",
        "ru": "Разговор с одним агентом в панели рядом с чатом: Ctrl+O туда и обратно, Tab — вкладки, Ctrl+] — закрыть",
    },
    "help.95f5ecec5f": {
        "en": "Pause the room: agents get no messages until /resume",
        "ru": "Пауза: агенты не получают сообщений до /продолжить",
    },
    "help.bf237a3c84": {
        "en": "Clear the history for everyone; old messages move to an archive",
        "ru": "Очистить историю для всех; старые сообщения уходят в архив",
    },
    "help.b1c83d1813": {
        "en": "Stop the room: its windows and the agents it started close; history stays",
        "ru": "Остановить комнату: закрываются её окна и запущенные ею агенты; история остаётся",
    },
    "help.6a4472ea0e": {
        "en": "Close this chat panel (asks first); agents keep running",
        "ru": "Закрыть этот чат после подтверждения; агенты продолжат работу",
    },
    "help.2639bda3b4": {"en": "The room's language: Russian or English", "ru": "Язык комнаты: русский или английский"},
    "help.679993f49d": {"en": "Red status", "ru": "Красный статус"},
    "help.b55b1117dd": {"en": "Status colours", "ru": "Цвета статуса"},
    "help.6400bc3301": {"en": "Status marks", "ru": "Значки статуса"},
    "help.667bf0636e": {"en": "Status layout", "ru": "Строка статусов"},
    "help.c9337cd13e": {"en": "@name text", "ru": "@имя текст"},
    "help.5642921e1d": {"en": "Add an agent", "ru": "Добавить агента"},
    "help.5f1009f1ff": {
        "en": "Choose an agent type, project folder, and name.",
        "ru": "Выберите программу, папку проекта и имя.",
    },
    "help.5eef887242": {
        "en": "The room checks the connection before showing the agent as connected.",
        "ru": "Комната проверит связь и только потом покажет агента подключённым.",
    },
    "help.fe44aa3b1e": {"en": "See what an agent is doing", "ru": "Посмотреть, что делает агент"},
    "help.723ae40430": {"en": "Choose an agent to see its live screen.", "ru": "Выберите агента, чтобы увидеть его экран."},
    "help.213df32fcd": {"en": "This is read-only. Press Esc to return to the chat.", "ru": "Только чтение. Esc — назад в чат."},
    "help.0097af42ee": {"en": "View an agent", "ru": "Посмотреть агента"},
    "help.aa1e92b5c5": {"en": "Open an agent's window in a new tab", "ru": "Открыть окно агента в новой вкладке"},
    "help.b579907bf4": {
        "en": "Choose an agent: its command goes into your clipboard.",
        "ru": "Выберите агента: команда попадёт в буфер обмена.",
    },
    "help.b0dae6b200": {
        "en": "Open a new terminal tab (+), paste with Cmd+V and press Enter.",
        "ru": "Откройте новую вкладку терминала (+), вставьте Cmd+V и нажмите Enter.",
    },
    "help.f7207047c3": {"en": "Open an agent terminal", "ru": "Открыть терминал агента"},
    "help.bb39d21b7d": {"en": "Answer an agent over the chat", "ru": "Ответить агенту поверх чата"},
    "help.b859a4c7a8": {
        "en": "Choose an agent to open its terminal over this chat.",
        "ru": "Выберите агента, чтобы открыть его терминал поверх чата.",
    },
    "help.6e893f15df": {
        "en": "Respond in that window. Press Ctrl+] to return to the chat.",
        "ru": "Ответьте в этом окне. Ctrl+] — назад в чат.",
    },
    "help.ee11988fb4": {"en": "Answer an agent", "ru": "Ответить агенту"},
    "help.69d1258962": {"en": "Pause or resume the room", "ru": "Приостановить или возобновить доставку сообщений"},
    "help.c55cb683e8": {
        "en": "Pause holds delivery to agents and their outgoing messages.",
        "ru": "Пауза задерживает доставку агентам и их исходящие сообщения.",
    },
    "help.4a4dbc7ca9": {
        "en": "A turn already running may finish. Resume releases waiting messages.",
        "ru": "Уже начатая работа может завершиться. После возобновления доставки ожидающие сообщения будут отправлены.",
    },
    "help.e48c7298c4": {"en": "Clear conversation history", "ru": "Очистить историю разговора"},
    "help.d343ad2c96": {
        "en": "Messages move to the room's local archive. Confirm before clearing.",
        "ru": "Сообщения уходят в местный архив комнаты. Перед очисткой — подтверждение.",
    },
    "help.c1783d24d8": {
        "en": "This does not erase an agent's own session history.",
        "ru": "Собственную историю сессии агента это не стирает.",
    },
    "help.53b5158bc7": {"en": "Clear history", "ru": "Очистить историю"},
    "help.15c3fa417b": {"en": "Stop the room", "ru": "Остановить комнату"},
    "help.40373b140a": {
        "en": "Stop asks which room windows will close and preserves history.",
        "ru": "Перед остановкой вы увидите, какие окна закроются. История сохранится.",
    },
    "help.a468e04ed2": {"en": "Stop room", "ru": "Остановить комнату"},
    "help.b823bb9b42": {"en": "All keys", "ru": "Все клавиши"},
    "help.f620876eb7": {
        "en": "The full guide: recipients, multiline input, clearing a draft, history, status indicators,",
        "ru": "Полная справка: получатели, многострочный ввод, стирание черновика, история, значки статуса",
    },
    "help.35c9b64cc8": {"en": "and all commands.", "ru": "и все команды."},
    "help.931e90b732": {"en": "Room guide · {me}", "ru": "Справка комнаты · {me}"},
    "help.1254a7fd3e": {"en": "Pause the room", "ru": "Приостановить доставку"},
    "help.60dd26ce4d": {"en": "Resume the room", "ru": "Возобновить доставку"},
    "help.7b52603bb2": {"en": "What would you like to do?", "ru": "Что вы хотите сделать?"},
    "help.eb72290348": {
        "en": "You can open the guide any time: type /help.",
        "ru": "Справку можно открыть в любой момент: /помощь.",
    },
    "help.88dfd48ffe": {"en": "/dm name \u00b7 /dm close", "ru": "/личное имя · /личное закрыть"},
    # /open: the command in the clipboard, or not.
    "open.copied": {
        "en": "The command is already in your clipboard — you don't need to copy anything.\n  1. Open a new terminal tab (+).\n  2. Paste with Cmd+V and press Enter: {label}'s window opens there.\n  3. Close that tab when done: {label} keeps running, the room stays here.\nIf pasting doesn't work, copy this command yourself:\n\n{line}  {command}\n",
        "ru": "Команда уже в буфере обмена — копировать ничего не нужно.\n  1. Откройте новую вкладку терминала (+).\n  2. Вставьте Cmd+V и нажмите Enter: там откроется окно агента {label}.\n  3. Закончив, закройте вкладку: {label} продолжит работу, комната останется здесь.\nЕсли вставка не сработала, скопируйте команду сами:\n\n{line}  {command}\n",
    },
    "open.copy_failed": {
        "en": "Could not copy automatically — copy this command yourself:\n\n{line}  {command}\n\n  1. Open a new terminal tab (+).\n  2. Paste the command and press Enter: {label}'s window opens there.\n  3. Close that tab when done: {label} keeps running, the room stays here.\n",
        "ru": "Скопировать автоматически не удалось — скопируйте команду вручную:\n\n{line}  {command}\n\n  1. Откройте новую вкладку терминала (+).\n  2. Вставьте команду и нажмите Enter: там откроется окно агента {label}.\n  3. Закончив, закройте вкладку: {label} продолжит работу, комната останется здесь.\n",
    },
    # Scenario titles and hints.
    "scenario.development.title": {"en": "Development", "ru": "Разработка"},
    "scenario.development.hint": {
        "en": "Split the work, check the changes, get code",
        "ru": "Разделить работу, проверить изменения, получить код",
    },
    "scenario.research.title": {"en": "Research", "ru": "Исследование"},
    "scenario.research.hint": {
        "en": "Gather facts, check them, get a conclusion with sources",
        "ru": "Собрать сведения, проверить факты, получить вывод с источниками",
    },
    "scenario.text.title": {"en": "Text", "ru": "Текст"},
    "scenario.text.hint": {"en": "Write a text and have it checked", "ru": "Подготовить текст и проверить его"},
    "scenario.decision.title": {"en": "Decision", "ru": "Решение"},
    "scenario.decision.hint": {
        "en": "Compare the options, see the arguments and where they disagree",
        "ru": "Сравнить варианты и увидеть доводы и разногласия",
    },
    "scenario.long.title": {"en": "Long work", "ru": "Долгая работа"},
    "scenario.long.hint": {
        "en": "Run a project and keep its state between your visits",
        "ru": "Вести проект и сохранять его состояние между посещениями",
    },
    "scenario.assistant.title": {"en": "One assistant", "ru": "Один помощник"},
    "scenario.assistant.hint": {"en": "Work with one agent directly", "ru": "Работать напрямую с одним агентом"},
    # Roles, memory, whom one reports to — as shown.
    "role.lead": {"en": "lead", "ru": "ведущий"},
    "role.worker": {"en": "worker", "ru": "исполнитель"},
    "role.reviewer": {"en": "reviewer", "ru": "проверяющий"},
    "role.none": {"en": "no role", "ru": "без роли"},
    "model.default": {"en": "default model", "ru": "модель по умолчанию"},
    "memory.keep": {"en": "keep", "ru": "не трогать автоматически"},
    "memory.compact": {"en": "compact past {k}k", "ru": "сжимать после {k} тыс."},
    "memory.clear": {"en": "clear past {k}k", "ru": "очищать после {k} тыс."},
    "reports.to": {"en": "reports to {whom}", "ru": "отвечает: {whom}"},
    "whom.person": {"en": "the person", "ru": "вам"},
    # The assignment an agent gets.
    "brief.head": {"en": "Your assignment in this room", "ru": "Ваше назначение в этой комнате"},
    "brief.version": {"en": " (version {v})", "ru": " (версия {v})"},
    "brief.role": {"en": "- Role: {role} — {what}.", "ru": "- Роль: {role} — {what}."},
    "brief.role.none": {"en": "- Role: none set.", "ru": "- Роль: не задана."},
    "brief.topic": {"en": "- Topic: {topic}.", "ru": "- Тема: {topic}."},
    "brief.report": {"en": "- Report to: {whom}.", "ru": "- Кому отвечаете: {whom}."},
    "brief.lead": {
        "en": "- Lead: {lead} — gathers the result and the questions for the person.",
        "ru": "- Ведущий: {lead} — собирает итог и вопросы к пользователю.",
    },
    "brief.you_lead": {
        "en": "- You are the room's lead: the person hears from you; ask them what only they can decide.",
        "ru": "- Вы — ведущий комнаты: собирайте итог для пользователя и обращайтесь к нему с вопросами, которые может решить только он.",
    },
    "brief.goal": {"en": "- Goal: {goal} You answer for reaching it.", "ru": "- Цель: {goal} Вы отвечаете за её достижение."},
    "brief.team": {
        "en": "- Your team: {team}. Give each its part, have the result checked, gather it — do not do their parts yourself.",
        "ru": "- Ваша команда: {team}. Распределите работу, организуйте проверку и соберите итог. Не выполняйте чужие части работы сами.",
    },
    "brief.result": {
        "en": "- Result file: {output}, in the project folder. Keep the result there, not in messages.",
        "ru": "- Итоговый файл: {output}, в папке проекта. Сохраняйте результат в этом файле, а не в сообщениях.",
    },
    "brief.without.some": {
        "en": "- Without the person you may: {what}. Anything else external — ask first.",
        "ru": "- Без пользователя можно: {what}. Перед другими действиями вне комнаты спросите разрешение.",
    },
    "brief.without.none": {
        "en": "- Without the person: nothing external — ask before publishing, writing outside the room or spending.",
        "ru": "- Перед публикацией, отправкой сообщений вне комнаты или расходованием денег спросите разрешение пользователя.",
    },
    "brief.language": {"en": "- Answer in English.", "ru": "- Отвечайте по-русски."},
    "brief.not_task": {
        "en": "This is your assignment, not a task: wait for one from {whom}.",
        "ru": "Это назначение, а не задача. Кто даст задачу: {whom}.",
    },
    "brief.foot": {
        "en": "These are the room's settings; they change no permissions of your tools. After /compact or /clear they come again.",
        "ru": "Это настройки комнаты; права ваших инструментов они не меняют. После /compact или /clear они придут снова.",
    },
    "whom.person.from": {"en": "the person", "ru": "пользователь"},
    "whom.person.to": {"en": "the person", "ru": "человеку"},
    "agent.word": {"en": "agent", "ru": "агент"},
    "role.lead.what": {
        "en": "you split the work, gather the result and the questions for the person",
        "ru": "вы распределяете работу, собираете итог и вопросы к пользователю",
    },
    "role.worker.what": {
        "en": "you do your part of the work and hand it to whom you report to",
        "ru": "вы делаете свою часть работы и сдаёте её тому, кому отвечаете",
    },
    "role.reviewer.what": {
        "en": "you look for mistakes in the result and say what to fix; you do not redo the work",
        "ru": "вы ищете ошибки в результате и говорите, что исправить; работу заново не делаете",
    },
    "without.merge": {"en": "merge into the main branch", "ru": "вливать в основную ветку"},
    "without.publish": {"en": "publish", "ru": "публиковать"},
    "without.send": {"en": "write to anyone outside the room", "ru": "писать кому-либо вне комнаты"},
    "without.spend": {"en": "spend money", "ru": "тратить деньги"},
}

# The chat's commands: each by its English name and its Russian one, both always understood. The first is the
# one the room works with; the second is shown in a Russian room's menu and help.
COMMANDS: list[tuple[str, str]] = [
    ("/add", "/добавить"),
    ("/remove", "/отключить"),
    ("/mute", "/заглушить"),
    ("/unmute", "/разглушить"),
    ("/unlimit", "/снятьлимит"),
    ("/name", "/имя"),
    ("/view", "/экран"),
    ("/answer", "/ответить"),
    ("/open", "/открыть"),
    ("/pause", "/пауза"),
    ("/resume", "/продолжить"),
    ("/clear", "/очистить"),
    ("/help", "/помощь"),
    ("/mine", "/мне"),
    ("/dm", "/личное"),
    ("/restart", "/перезапустить"),
    ("/team", "/команда"),
    ("/stop", "/остановить"),
    ("/exit", "/выход"),
    ("/quit", "/выйти"),
    ("/language", "/язык"),
]
SUBCOMMANDS: dict[str, dict[str, str]] = {"/dm": {"закрыть": "close"}, "/team": {"сохранить": "save"}}
_TO_EN = {unicodedata.normalize("NFC", ru).casefold(): en for en, ru in COMMANDS} | {en: en for en, _ in COMMANDS}


def plural(lang: str, n: int) -> str:
    """The form of a word for a number: one / few / many in Russian (21 агент, 22 агента, 25 агентов, 11–14 —
    агентов), one / other in English."""
    if lang != "ru":
        return "one" if n == 1 else "other"
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def lang_of(value: object) -> str:
    return value if isinstance(value, str) and value in LANGS else "en"


def words_in(key: str, lang: str = "en", **params: Any) -> str:
    """The text for a key in a language, its parameters put in whole. A count named `n` takes its form."""
    entry = CATALOG[key][lang_of(lang)]
    if isinstance(entry, dict):
        entry = entry[plural(lang_of(lang), int(params.get("n", 0)))]
    return str(entry).format(**params) if params else str(entry)


def command(line: str) -> str:
    """A line typed in the chat with its command in English, whichever name was typed: the command's token only,
    by Unicode NFC and without case — the rest (names, text) as it is. Its subcommand too («/личное закрыть»).
    Not a command — the line as it came."""
    if not line.startswith("/") or line.startswith("//"):
        return line
    token, sep, rest = line.partition(" ")
    en = _TO_EN.get(unicodedata.normalize("NFC", token).casefold())
    if en is None:
        return line
    if rest:
        sub, sep2, tail = rest.partition(" ")
        sub_en = SUBCOMMANDS.get(en, {}).get(unicodedata.normalize("NFC", sub).casefold())
        if sub_en:
            rest = sub_en + sep2 + tail
    return en + sep + rest


def shown_command(en: str, lang: str) -> str:
    """A command as a room in this language shows it: Russian in a Russian room, English otherwise."""
    return next((ru for e, ru in COMMANDS if e == en), en) if lang_of(lang) == "ru" else en


def help_words(en: str, lang: str, **params: Any) -> str:
    """A line of the guide in the room's language, by its English words (catalog key help.<hash>)."""
    return words_in("help." + hashlib.sha1(en.encode()).hexdigest()[:10], lang, **params)


def shown_commands(text: str, lang: str) -> str:
    """Each command named in a short label («/add · /remove») as a room in this language shows it."""
    return " ".join(shown_command(w, lang) if w.startswith("/") else w for w in text.split(" "))
